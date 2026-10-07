"""Owned conversations, immutable context revisions and message acceptance."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app import auth, db
from app.assistant import diagnostics, prompts


class ChatError(ValueError):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


def _uuid(value: Any) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ChatError("invalid_id") from exc


def _profile(cur: Any, profile_id: Any, principal: auth.Principal, privacy: str) -> dict[str, Any]:
    if type(profile_id) is not int or profile_id < 1 or privacy not in {"local_only", "configured"}:
        raise ChatError("invalid_settings")
    cur.execute("SELECT * FROM public.assistant_provider_profiles WHERE profile_id = %s AND active", (profile_id,))
    profile = cur.fetchone()
    if profile is None or not principal.allows(profile["min_role"]):
        raise ChatError("profile_not_available", 404)
    if privacy == "local_only" and profile["network_scope"] != "internal":
        raise ChatError("local_only_requires_internal_profile")
    return profile


def _context_for_run(cur: Any, pi_id: Any, scenario_id: Any, run_id: Any) -> dict[str, Any]:
    if (not isinstance(pi_id, str) or not pi_id.strip() or not isinstance(scenario_id, str)
            or not scenario_id.strip() or type(run_id) is not int or run_id < 1):
        raise ChatError("invalid_run_context")
    cur.execute(
        "SELECT g.generation_id, s.snapshot_id FROM public.assistant_dataset_generations g "
        "JOIN public.assistant_input_snapshots s ON s.generation_id = g.generation_id AND s.run_id = %s "
        "WHERE g.active AND g.pi_id = %s AND g.scenario_id = %s",
        (run_id, pi_id, scenario_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ChatError("run_snapshot_not_found", 404)
    return {"scope": "planning", "generation_id": row["generation_id"],
            "snapshot_id": row["snapshot_id"], "pi_id": pi_id,
            "scenario_id": scenario_id, "run_id": run_id}


def _joined(cur: Any, conversation_id: UUID, owner: str, *, lock: bool = False) -> dict[str, Any]:
    cur.execute("SELECT * FROM public.assistant_conversations WHERE conversation_id = %s "
                "AND owner_key = %s" + (" FOR UPDATE" if lock else ""), (conversation_id, owner))
    conversation = cur.fetchone()
    if conversation is None:
        raise ChatError("conversation_not_found", 404)
    cur.execute("SELECT * FROM public.assistant_context_revisions WHERE conversation_id = %s "
                "ORDER BY revision DESC LIMIT 1", (conversation_id,))
    context = cur.fetchone()
    return {**conversation, **context}


def _public(cur: Any, row: dict[str, Any]) -> dict[str, Any]:
    newer: bool | None = None
    if row["scope"] == "planning":
        cur.execute("SELECT EXISTS(SELECT 1 FROM public.assistant_dataset_generations g "
                    "WHERE g.schema_name = (SELECT schema_name FROM public.assistant_dataset_generations "
                    "WHERE generation_id = %s) AND g.active AND g.generation_id <> %s) "
                    "OR EXISTS(SELECT 1 FROM public.assistant_input_snapshots s "
                    "WHERE s.generation_id = %s AND s.run_id > %s) AS newer",
                    (row["generation_id"], row["generation_id"], row["generation_id"], row["run_id"]))
        newer = bool(cur.fetchone()["newer"])
    return {"conversation_id": str(row["conversation_id"]), "title": row["title"],
            "scope": row["scope"], "context_revision": row["revision"],
            "provider_profile_id": row["profile_id"], "privacy_mode": row["privacy_mode"],
            "pi_id": row["pi_id"], "scenario_id": row["scenario_id"], "run_id": row["run_id"],
            "newer_run_available": newer}


def create(principal: auth.Principal, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("scope") not in {"knowledge", "planning"}:
        raise ChatError("invalid_conversation")
    expected = {"scope", "provider_profile_id", "privacy_mode"}
    if raw["scope"] == "planning":
        expected |= {"pi_id", "scenario_id", "run_id"}
    if set(raw) != expected:
        raise ChatError("invalid_conversation_fields")
    conversation_id = uuid4()
    with db.transaction(operation="assistant_conversation_create") as cur:
        _profile(cur, raw["provider_profile_id"], principal, raw["privacy_mode"])
        context = ({"scope": "knowledge", "generation_id": None, "snapshot_id": None,
                    "pi_id": None, "scenario_id": None, "run_id": None}
                   if raw["scope"] == "knowledge" else
                   _context_for_run(cur, raw["pi_id"], raw["scenario_id"], raw["run_id"]))
        cur.execute("INSERT INTO public.assistant_conversations "
                    "(conversation_id, owner_key, profile_id, privacy_mode) VALUES (%s, %s, %s, %s)",
                    (conversation_id, principal.owner_key, raw["provider_profile_id"], raw["privacy_mode"]))
        cur.execute("INSERT INTO public.assistant_context_revisions "
                    "(conversation_id, revision, scope, generation_id, snapshot_id, pi_id, scenario_id, run_id) "
                    "VALUES (%s, 1, %s, %s, %s, %s, %s, %s)",
                    (conversation_id, *(context[key] for key in
                      ("scope", "generation_id", "snapshot_id", "pi_id", "scenario_id", "run_id"))))
        return _public(cur, _joined(cur, conversation_id, principal.owner_key))


def get(principal: auth.Principal, conversation_id: Any) -> dict[str, Any]:
    with db.connection(read_only=True) as conn, conn.cursor() as cur:
        return _public(cur, _joined(cur, _uuid(conversation_id), principal.owner_key))


def list_conversations(principal: auth.Principal, cursor: str | None = None) -> dict[str, Any]:
    with db.connection(read_only=True) as conn, conn.cursor() as cur:
        where = ""
        params: list[Any] = [principal.owner_key]
        if cursor:
            cur.execute("SELECT created_at, conversation_id FROM public.assistant_conversations "
                        "WHERE owner_key = %s AND conversation_id = %s", (principal.owner_key, _uuid(cursor)))
            anchor = cur.fetchone()
            if anchor is None:
                raise ChatError("invalid_cursor")
            where = "AND (created_at, conversation_id) < (%s, %s)"
            params += [anchor["created_at"], anchor["conversation_id"]]
        cur.execute("SELECT conversation_id FROM public.assistant_conversations "
                    "WHERE owner_key = %s " + where + " ORDER BY created_at DESC, conversation_id DESC LIMIT 51", params)
        ids = [row["conversation_id"] for row in cur.fetchall()]
        items = [_public(cur, _joined(cur, item, principal.owner_key)) for item in ids[:50]]
        return {"items": items, "next_cursor": str(ids[49]) if len(ids) > 50 else None}


def settings(principal: auth.Principal, conversation_id: Any, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"provider_profile_id", "privacy_mode"}:
        raise ChatError("invalid_settings")
    cid = _uuid(conversation_id)
    with db.transaction(operation="assistant_conversation_settings") as cur:
        row = _joined(cur, cid, principal.owner_key, lock=True)
        _no_active_job(cur, cid)
        _profile(cur, raw["provider_profile_id"], principal, raw["privacy_mode"])
        cur.execute("UPDATE public.assistant_conversations SET profile_id = %s, privacy_mode = %s, "
                    "updated_at = now() WHERE conversation_id = %s",
                    (raw["provider_profile_id"], raw["privacy_mode"], cid))
        row.update(profile_id=raw["provider_profile_id"], privacy_mode=raw["privacy_mode"])
        return _public(cur, row)


TITLE_MAX = 120


def rename(principal: auth.Principal, conversation_id: Any, raw: Any) -> dict[str, Any]:
    """Пользователь называет свой чат сам; название не влияет на контекст и ответы."""
    if not isinstance(raw, dict) or set(raw) != {"title"}:
        raise ChatError("invalid_title")
    title = raw["title"]
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX:
        raise ChatError("invalid_title")
    cid = _uuid(conversation_id)
    with db.transaction(operation="assistant_conversation_rename") as cur:
        row = _joined(cur, cid, principal.owner_key, lock=True)
        cur.execute("UPDATE public.assistant_conversations SET title = %s, updated_at = now() "
                    "WHERE conversation_id = %s", (title.strip(), cid))
        row["title"] = title.strip()
        return _public(cur, row)


def delete(principal: auth.Principal, conversation_id: Any) -> dict[str, Any]:
    """Удаляет свой чат со всей историей, доказательствами и рекомендациями. Чат с активным заданием не удаляется."""
    cid = _uuid(conversation_id)
    with db.transaction(operation="assistant_conversation_delete") as cur:
        _joined(cur, cid, principal.owner_key, lock=True)
        _no_active_job(cur, cid)
        for table in ("assistant_recommendations", "assistant_evidence", "assistant_scenario_results",
                      "assistant_messages", "assistant_jobs", "assistant_context_revisions"):
            cur.execute(f"DELETE FROM public.{table} WHERE conversation_id = %s", (cid,))  # noqa: S608 - имена из списка выше
        cur.execute("DELETE FROM public.assistant_conversations WHERE conversation_id = %s", (cid,))
    return {"deleted": True, "conversation_id": str(cid)}


def _no_active_job(cur: Any, cid: UUID) -> None:
    cur.execute("SELECT 1 FROM public.assistant_jobs WHERE conversation_id = %s "
                "AND status IN ('queued', 'running')", (cid,))
    if cur.fetchone() is not None:
        raise ChatError("conversation_busy", 409)


def bind_context(principal: auth.Principal, conversation_id: Any, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"pi_id", "scenario_id", "run_id", "expected_context_revision"}:
        raise ChatError("invalid_context")
    cid = _uuid(conversation_id)
    with db.transaction(operation="assistant_context_bind") as cur:
        row = _joined(cur, cid, principal.owner_key, lock=True)
        if type(raw["expected_context_revision"]) is not int or raw["expected_context_revision"] != row["revision"]:
            raise ChatError("context_revision_conflict", 409)
        _no_active_job(cur, cid)
        context = _context_for_run(cur, raw["pi_id"], raw["scenario_id"], raw["run_id"])
        revision = row["revision"] + 1
        cur.execute("INSERT INTO public.assistant_context_revisions "
                    "(conversation_id, revision, scope, generation_id, snapshot_id, pi_id, scenario_id, run_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (cid, revision, *(context[key] for key in
                      ("scope", "generation_id", "snapshot_id", "pi_id", "scenario_id", "run_id"))))
        cur.execute("UPDATE public.assistant_conversations SET updated_at = now() WHERE conversation_id = %s", (cid,))
        return _public(cur, {**row, **context, "revision": revision})


def list_messages(principal: auth.Principal, conversation_id: Any, cursor: str | None = None) -> dict[str, Any]:
    cid = _uuid(conversation_id)
    try:
        after = int(cursor) if cursor else 0
    except ValueError as exc:
        raise ChatError("invalid_cursor") from exc
    if after < 0:
        raise ChatError("invalid_cursor")
    with db.connection(read_only=True) as conn, conn.cursor() as cur:
        _joined(cur, cid, principal.owner_key)
        cur.execute("SELECT message_id, sequence_no, role, content, context_revision, payload "
                    "FROM public.assistant_messages WHERE conversation_id = %s AND sequence_no > %s "
                    "ORDER BY sequence_no LIMIT 51", (cid, after))
        rows = cur.fetchall()
        items = [{"message_id": str(row["message_id"]), "role": row["role"],
                  "text": row["content"], "context_revision": row["context_revision"],
                  **({"answer": diagnostics.public_payload(row["payload"], principal)}
                     if row["role"] == "assistant" and row["payload"] else {})}
                 for row in rows[:50]]
        return {"items": items, "next_cursor": str(rows[49]["sequence_no"]) if len(rows) > 50 else None}


def send_message(principal: auth.Principal, conversation_id: Any, raw: Any,
                 idempotency_key: str | None) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"text", "expected_context_revision", "expected_last_message_id"}:
        raise ChatError("invalid_message")
    text = raw["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
        raise ChatError("invalid_message_text")
    if type(raw["expected_context_revision"]) is not int or raw["expected_context_revision"] < 1:
        raise ChatError("invalid_context_revision")
    if raw["expected_last_message_id"] is not None:
        _uuid(raw["expected_last_message_id"])
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise ChatError("idempotency_key_required")
    try:
        deadline_seconds = int(os.environ.get("PI_PLANNER_ASSISTANT_MESSAGE_DEADLINE_SECONDS", "150"))
    except ValueError as exc:
        raise ChatError("invalid_message_deadline") from exc
    if not 10 <= deadline_seconds <= 300:
        raise ChatError("invalid_message_deadline")
    cid = _uuid(conversation_id)
    digest = hashlib.sha256(json.dumps({"conversation_id": str(cid), **raw},
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # Resolve both required prompt versions before the message is accepted.
    default = prompts.get("default")
    personal = prompts.get("user", principal)
    with db.transaction(operation="assistant_message_send") as cur:
        cur.execute("SELECT pg_advisory_xact_lock(90340212, hashtext(%s))",
                    (principal.owner_key + ":" + idempotency_key,))
        cur.execute("SELECT job_id, request_sha256, status FROM public.assistant_jobs "
                    "WHERE owner_key = %s AND idempotency_key = %s", (principal.owner_key, idempotency_key))
        existing = cur.fetchone()
        if existing is not None:
            if existing["request_sha256"] != digest:
                raise ChatError("idempotency_conflict", 409)
            return {"job_id": str(existing["job_id"]), "status": existing["status"]}
        row = _joined(cur, cid, principal.owner_key, lock=True)
        if row["revision"] != raw["expected_context_revision"]:
            raise ChatError("context_revision_conflict", 409)
        _no_active_job(cur, cid)
        _profile(cur, row["profile_id"], principal, row["privacy_mode"])
        cur.execute("SELECT revision_id FROM public.assistant_kb_revisions WHERE active")
        kb_row = cur.fetchone()
        kb_revision = str(kb_row["revision_id"]) if kb_row else None
        cur.execute("SELECT message_id, sequence_no FROM public.assistant_messages "
                    "WHERE conversation_id = %s ORDER BY sequence_no DESC LIMIT 1", (cid,))
        last = cur.fetchone()
        if (str(last["message_id"]) if last else None) != raw["expected_last_message_id"]:
            raise ChatError("last_message_conflict", 409)
        seq = last["sequence_no"] + 1 if last else 1
        message_id, job_id = uuid4(), uuid4()
        cur.execute("INSERT INTO public.assistant_messages "
                    "(message_id, conversation_id, sequence_no, context_revision, role, content, "
                    "default_prompt_id, user_prompt_id, profile_id, payload) "
                    "VALUES (%s, %s, %s, %s, 'user', %s, %s, %s, %s, %s)",
                    (message_id, cid, seq, row["revision"], text.strip(),
                     default["prompt_id"], personal["prompt_id"], row["profile_id"],
                     Jsonb({"kb_revision": kb_revision})))
        payload = {"message_id": str(message_id), "context_revision": row["revision"],
                   "generation_id": str(row["generation_id"]) if row["generation_id"] else None,
                   "snapshot_id": str(row["snapshot_id"]) if row["snapshot_id"] else None,
                   "kb_revision": kb_revision,
                   "privacy_mode": row["privacy_mode"],
                   "principal": {"name": principal.name, "role": principal.role,
                                 "source": principal.source, "user_id": principal.user_id}}
        cur.execute("INSERT INTO public.assistant_jobs "
                    "(job_id, owner_key, conversation_id, kind, status, idempotency_key, request_sha256, "
                    "input_payload, deadline_at) "
                    "VALUES (%s, %s, %s, 'message', 'queued', %s, %s, %s, "
                    "now() + (%s * interval '1 second'))",
                    (job_id, principal.owner_key, cid, idempotency_key, digest,
                     Jsonb(payload), deadline_seconds))
        cur.execute("UPDATE public.assistant_conversations SET updated_at = now(), "
                    "title = CASE WHEN title = '' THEN %s ELSE title END "
                    "WHERE conversation_id = %s", (text.strip()[:80], cid))
        return {"job_id": str(job_id), "status": "queued"}


def get_job(principal: auth.Principal, job_id: Any) -> dict[str, Any]:
    row = db.query_one("SELECT job_id, status, result_payload, error_payload FROM public.assistant_jobs "
                       "WHERE job_id = %s AND owner_key = %s", (_uuid(job_id), principal.owner_key))
    if row is None:
        raise ChatError("job_not_found", 404)
    return {"job_id": str(row["job_id"]), "status": row["status"],
            "result": diagnostics.public_payload(row["result_payload"], principal),
            "error": diagnostics.job_error(row["error_payload"], principal, str(row["job_id"]))}


def cancel_job(principal: auth.Principal, job_id: Any) -> dict[str, Any]:
    jid = _uuid(job_id)
    with db.transaction(operation="assistant_job_cancel") as cur:
        cur.execute("UPDATE public.assistant_jobs SET status = 'cancelled', updated_at = now(), "
                    "lease_until = NULL WHERE job_id = %s AND owner_key = %s "
                    "AND status IN ('queued', 'running') RETURNING job_id, status, result_payload, error_payload",
                    (jid, principal.owner_key))
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT job_id, status, result_payload, error_payload FROM public.assistant_jobs "
                        "WHERE job_id = %s AND owner_key = %s", (jid, principal.owner_key))
            row = cur.fetchone()
        if row is None:
            raise ChatError("job_not_found", 404)
        return {"job_id": str(row["job_id"]), "status": row["status"],
                "result": diagnostics.public_payload(row["result_payload"], principal),
            "error": diagnostics.job_error(row["error_payload"], principal, str(row["job_id"]))}
