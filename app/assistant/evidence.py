"""Evidence records bound to an answer and readable only by its owner."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app import auth, db
from app.assistant import facts, orchestrator, retrieval, snapshots

MAX_MODEL_FACT_CHARS = 18000


def prepare(snapshot_id: UUID | None, question: str,
            focus: tuple[str, str] | None,
            found: retrieval.SearchResult,
            document_context: str,
            scenario: dict[str, Any] | None = None,
            comparison: dict[str, Any] | None = None,
            operation: str | None = None) -> tuple[list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    context = ""
    if snapshot_id is not None:
        if operation == "planning_actions":
            payload = facts.get_improvement_plan(snapshot_id)
        elif comparison is not None:
            payload = comparison
        else:
            _inputs, plan, _baseline, _options = snapshots.load(snapshot_id)
            codes = {row.kpi_code for row in plan.kpis}
            mentioned = orchestrator.metric_mentions(question, codes)
            if focus and focus[0] == "task":
                payload = facts.get_task_trace(snapshot_id, focus[1])
            elif focus and focus[0] == "team":
                payload = facts.get_team(snapshot_id, focus[1])
            elif len(mentioned) == 1:
                payload = facts.get_metric(snapshot_id, mentioned[0])
            else:
                payload = facts.get_overview(snapshot_id)
        evidence_id = uuid4()
        records.append({"evidence_id": evidence_id, "source_type": "snapshot",
                        "source_ref": str(snapshot_id), "payload": payload})
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(text) > MAX_MODEL_FACT_CHARS:
            context = (f"[evidence:{evidence_id}] Полный факт сохранён отдельно; "
                       "объём данных превышает контекст модели. Уточните задачу, команду или KPI.")
        else:
            context = f"[evidence:{evidence_id}] {text}"
    if scenario is not None:
        evidence_id = uuid4()
        records.append({"evidence_id": evidence_id, "source_type": "scenario",
                        "source_ref": scenario["scenario_result_id"],
                        "payload": scenario["result"]})
        scenario_text = json.dumps(scenario["result"], ensure_ascii=False, separators=(",", ":"))
        if len(context) + len(scenario_text) <= MAX_MODEL_FACT_CHARS:
            context += f"\n[evidence:{evidence_id}] Сценарный расчёт: {scenario_text}"
        else:
            context += "\nСценарный результат сохранён, но слишком велик для ответа. Уточните меру или команду."
    for hit in found.hits:
        if f"[kb:{hit.chunk_id}]" not in document_context:
            continue
        records.append({"evidence_id": uuid4(), "source_type": "document",
                        "source_ref": str(hit.chunk_id),
                        "payload": {"revision": str(found.revision), "chunk_id": str(hit.chunk_id),
                                    "document_id": str(hit.document_id), "path": hit.path,
                                    "heading": hit.heading, "version": hit.version,
                                    "content": hit.content, "metadata": hit.metadata}})
    return records, context


def check_citations(answer: dict[str, Any], records: list[dict[str, Any]]) -> None:
    allowed = {str(record["evidence_id"]) for record in records}
    allowed_kb = {record["source_ref"] for record in records
                  if record["source_type"] == "document"}
    content = answer["summary"] + "\n" + answer["explanation"]
    cited = set(re.findall(r"\[evidence:([^\]]+)\]", content))
    cited_kb = set(re.findall(r"\[kb:([^\]]+)\]", content))
    if not cited <= allowed or not cited_kb <= allowed_kb:
        raise ValueError("unverified_evidence_reference")


def render_fact_refs(answer: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """Insert numeric values from saved evidence, never from model-provided values."""
    by_id = {str(record["evidence_id"]): record for record in records}
    rendered: list[str] = []
    for ref in answer.get("fact_refs", []):
        item = by_id.get(ref["evidence_id"])
        field = ref["field"]
        if (item is None or item["source_type"] == "document" or len(field) > 120
                or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_0-9]+)*", field)):
            raise ValueError("invalid_fact_reference")
        value: Any = item["payload"]
        for part in field.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                raise ValueError("invalid_fact_reference")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("non_numeric_fact_reference")
        try:
            numeric = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("non_numeric_fact_reference") from exc
        if not numeric.is_finite():
            raise ValueError("non_numeric_fact_reference")
        rendered.append(f"{field} = {value} [evidence:{ref['evidence_id']}]")
    allowed_entities: set[str] = set()

    def entities(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"task_id", "team_id", "initiative_id", "entity_id"} and isinstance(item, str):
                    allowed_entities.add(item)
                elif isinstance(item, (dict, list)):
                    entities(item)
        elif isinstance(value, list):
            for item in value:
                entities(item)

    for record in records:
        if record["source_type"] == "snapshot":
            entities(record["payload"])
    for entity in re.findall(r"\[entity:([^\]]+)\]", answer["summary"] + "\n" + answer["explanation"]):
        if entity not in allowed_entities:
            raise ValueError("unverified_entity_reference")
    bare = re.sub(r"\[(?:evidence|kb):[^\]]+\]", "", answer["summary"] + "\n" + answer["explanation"])
    # Numerals in prose are forbidden; all numeric claims are rendered above.
    # Validate the entire model prose BEFORE appending server values. A model
    # must not bypass validation by writing the rendering marker itself.
    prose = re.sub(r"\[entity:[^\]]+\]", "объект", bare)
    # Номера пунктов Markdown-списка («1.», «2)») — разметка, а не числовое утверждение.
    prose = re.sub(r"(?m)^[ \t]*\d{1,2}[.)][ \t]", "- ", prose)
    if re.search(r"\d", prose):
        raise ValueError("untyped_numeric_claim")
    if rendered:
        answer["explanation"] += ("\n\nПроверенные значения: " + "; ".join(rendered))


def save(cur: Any, *, conversation_id: UUID, message_id: UUID,
         records: list[dict[str, Any]]) -> None:
    for record in records:
        cur.execute("INSERT INTO public.assistant_evidence "
                    "(evidence_id, conversation_id, message_id, source_type, source_ref, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (record["evidence_id"], conversation_id, message_id,
                     record["source_type"], record["source_ref"], Jsonb(record["payload"])))


def get(principal: auth.Principal, evidence_id: str) -> dict[str, Any]:
    from app.assistant.conversations import ChatError

    try:
        identifier = UUID(evidence_id)
    except (ValueError, TypeError) as exc:
        raise ChatError("evidence_not_found", 404) from exc
    row = db.query_one("SELECT e.evidence_id, e.source_type, e.source_ref, e.payload, e.created_at "
                       "FROM public.assistant_evidence e "
                       "JOIN public.assistant_conversations c USING (conversation_id) "
                       "WHERE e.evidence_id = %s AND c.owner_key = %s",
                       (identifier, principal.owner_key))
    if row is None:
        raise ChatError("evidence_not_found", 404)
    return {"evidence_id": str(row["evidence_id"]), "source_type": row["source_type"],
            "source_ref": row["source_ref"], "payload": row["payload"],
            "created_at": row["created_at"].isoformat()}


def _server_answer(summary: str, explanation: str, records: list[dict[str, Any]],
                   revision: int, newer: bool | None, focus: tuple[str, str] | None) -> dict[str, Any]:
    return {"status": "answered", "summary": summary, "explanation": explanation,
            "clarification": "", "fact_refs": [], "recommendations": [],
            "evidence_ids": [str(record["evidence_id"]) for record in records],
            "entity_refs": [{"type": focus[0], "id": focus[1], "context_revision": revision}] if focus else [],
            "context_revision": revision, "newer_run_available": newer,
            "limitations": [], "sources": []}


def loan_answer(records: list[dict[str, Any]], revision: int, newer: bool | None,
                focus: tuple[str, str] | None = None) -> dict[str, Any] | None:
    """Answer a direct loan-hours question from assignments without an LLM call."""
    record = next((item for item in records if item["source_type"] == "snapshot"
                   and "loan_hours" in item["payload"]), None)
    if record is None:
        return None
    payload = record["payload"]
    scope = "выбранной задачи" if focus and focus[0] == "task" else (
        "выбранной команды" if focus else "выбранного плана")
    reference = f"[evidence:{record['evidence_id']}]"
    summary = f"Заёмные часы {scope}: {payload['loan_hours']} ч."
    explanation = "Это часы назначений, списанные с орбит других команд, а не дополнительные часы сверх фонда."
    rows = payload.get("loan_hours_by_sprint", [])
    if rows:
        explanation += "\n\nПо спринтам:\n" + "\n".join(
            f"- Спринт {row['sprint_no']}: {row['hours']} ч." for row in rows)
    explanation += f"\n\nИсточник: назначения закреплённого прогона {reference}."
    answer = _server_answer(summary, explanation, records, revision, newer, focus)
    answer["intent"] = "loan_hours"
    return answer


def task_answer(records: list[dict[str, Any]], revision: int, newer: bool | None,
                focus: tuple[str, str] | None) -> dict[str, Any] | None:
    """Keep the saved task explanation when model output cannot be verified."""
    record = next((item for item in records if item["source_type"] == "snapshot"
                   and item["payload"].get("operation") == "get_task_trace"), None)
    if record is None or not record["payload"].get("schedule"):
        return None
    row = record["payload"]["schedule"][0]
    reason = row.get("reason_text")
    if not reason:
        return None
    summary = (f"Задача [entity:{row['task_id']}] включена в план." if row["decision"] == "in_quarter"
               else f"Задача [entity:{row['task_id']}] перенесена.")
    explanation = reason + f"\n\nИсточник: решение планировщика [evidence:{record['evidence_id']}]."
    return _server_answer(summary, explanation, records, revision, newer, focus)


def action_answer(records: list[dict[str, Any]], revision: int, newer: bool | None) -> dict[str, Any] | None:
    record = next((item for item in records if item["source_type"] == "snapshot"
                   and item["payload"].get("operation") == "get_improvement_plan"), None)
    if record is None:
        return None
    payload = record["payload"]
    roles = payload["missing_roles"]
    reasons = payload["reason_counts"]
    steps = []
    if not payload["deferred_tasks"]:
        summary = "Все задачи выбранного снимка уже включены в план."
        steps.append("Проверьте сроки и устойчивость назначений; увеличение объёма требует новых задач и отдельного расчёта.")
    elif roles:
        top = roles[0]
        summary = f"Начните с роли «{top['role_name']}»: её нет у доступных специалистов в сохранённом снимке."
        steps.append("Подтвердите потребности и роли в исходных данных. Отсутствующие роли:")
        steps.extend(f"- «{role['role_name']}»: требуется для {len(role['task_ids'])} отложенных задач; "
                     f"сметная работа {role['required_work_hours']} ч." for role in roles)
        steps.append("Выберите способ закрыть дефицит: исправить ошибку в данных или рассчитать сценарий найма/обучения. "
                     "Для сценария нужны команда, роль, ставка и спринт начала; для обучения также навыки и затраты наставника.")
    else:
        summary = "Начните с причин переноса задач в закреплённом прогоне."
    if payload["skill_gap_tasks"]:
        steps.append(f"Проверьте подтверждённые навыки: подходящие исполнители не найдены для "
                     f"{len(payload['skill_gap_tasks'])} отложенных задач.")
    if reasons.get("ETC_REQUIRED"):
        steps.append("Запросите оставшуюся оценку работ для задач с неизвестным ETC; затем пересчитайте план.")
    if reasons.get("BLOCKED_BY_DEFERRED"):
        steps.append("Разберите блокирующие задачи перед зависимыми: добавление ресурса зависимой задаче не устраняет её предшественника.")
    if reasons.get("ROLE_HOURS_EXHAUSTED"):
        steps.append("Для дефицита часов проверьте ставки, календарь и занятость подходящих людей; "
                     "сравните перераспределение и наём сценарием.")
    if reasons.get("TEAM_SP_EXHAUSTED"):
        steps.append("Для дефицита SP проверьте историю скорости и focus_factor команды; "
                     "дополнительные часы сами по себе не снимают лимит SP.")
    if payload["deferred_tasks"]:
        steps.append("После подтверждения меры пересчитайте сценарий и сравните состав задач, завершённые инициативы и сроки. "
                     "Число затронутых задач не равно гарантированному приросту: зависимости, другие роли "
                     "и ёмкость могут остаться ограничением.")
    explanation = (f"В плане {payload['selected_tasks']} из {payload['task_count']} задач; "
                   f"отложено {payload['deferred_tasks']}.\n\n" + "\n\n".join(steps)
                   + f"\n\nОснование: сохранённый снимок [evidence:{record['evidence_id']}].")
    result = _server_answer(summary, explanation, records, revision, newer, None)
    result["limitations"] = ["Эффект мер не рассчитан; опубликованный план не изменён."]
    return result
