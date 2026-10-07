"""Bounded chat history and read-only facts from a pinned planner snapshot."""
from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from app import db
from app.assistant import snapshots

MAX_HISTORY_CHARS = 16000
MAX_FACT_CHARS = 18000

SYSTEM_HELP = """PI Planner хранит загруженный датасет, строит план PI и пересчитывает его после факта спринтов.
Пользователь выбирает PI и сценарий; план имеет номер прогона. Справочный чат работает без прогона.
Плановый чат закреплён за конкретным прогоном; после нового прогона пользователь явно меняет его контекст.
План показывает расписание задач, назначения, KPI и предупреждения. Сценарии ресурсов и чувствительности
не публикуют штатный план. Для точных чисел, причин решений и источников нужен закреплённый снимок прогона.
Роли сотрудников и потребности задач по ролям приходят из загруженного Excel; доступность исполнителей
определяется покрытием ролей и подтверждёнными навыками. Замещение отсутствующей роли не предполагается.
Ёмкость команды в SP рассчитывается из истории фактической скорости и focus_factor; при пересчёте
учитывается подтверждённый факт закрытых спринтов. Фонд часов людей считается отдельно по ставкам,
орбитам команд, календарю и доступности. Достаточно часов не означает достаточно ёмкости SP.
Bus Factor относится к навыкам, требуемым живым бэклогом, и подтверждённым носителям критичных навыков.
Если релевантные навыки не размечены, показатель не определён; оценка через роль — лишь приближение.
Эти общие правила доступны без снимка. Не спрашивай у пользователя, где сервер хранит поля или данные.
Если данных для ответа нет, объясни, какие именно данные нужны. Не обещай действия, которых сервер не выполнил."""

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object", "properties": {
        "status": {"type": "string", "enum": ["answered", "needs_clarification", "insufficient_data"]},
        "summary": {"type": "string"},
        "explanation": {"type": "string"},
        "clarification": {"type": "string"},
        "fact_refs": {"type": "array", "maxItems": 10, "items": {
            "type": "object", "properties": {"evidence_id": {"type": "string"},
                                          "field": {"type": "string"}},
            "required": ["evidence_id", "field"], "additionalProperties": False}},
    },
    "required": ["status", "summary", "explanation", "clarification", "fact_refs"],
    "additionalProperties": False,
}


def history(conversation_id: UUID, revision: int, latest_message_id: UUID) -> list[dict[str, str]]:
    rows = db.query_dicts(
        "SELECT message_id, role, content FROM public.assistant_messages "
        "WHERE conversation_id = %s AND context_revision = %s "
        "ORDER BY sequence_no DESC LIMIT 40", (conversation_id, revision),
    )
    result: list[dict[str, str]] = []
    used = 0
    for row in rows:
        content = row["content"]
        if row["message_id"] == latest_message_id:
            content = content[-10000:]
        if used + len(content) > MAX_HISTORY_CHARS and result:
            break
        result.append({"role": row["role"], "content": content})
        used += len(content)
    result.reverse()
    if not result or rows[0]["message_id"] != latest_message_id:
        raise ValueError("message history changed before generation")
    return result


def entity_mentions(question: str, identifiers) -> list[str]:
    """Match canonical IDs with omitted/alternate separators, preserving ambiguity."""
    result = []
    for identifier in identifiers:
        parts = re.split(r"[-_\s–—‑]+", identifier.casefold())
        pattern = r"(?<![\w])" + r"[-_\s–—‑]*".join(re.escape(part) for part in parts) + r"(?![\w])"
        if re.search(pattern, question.casefold()):
            result.append(identifier)
    return result


def focus(conversation_id: UUID, revision: int, snapshot_id: UUID | None,
          question: str) -> tuple[str, str] | None:
    if snapshot_id is None:
        return None
    inputs, _plan, _baseline, _options = snapshots.load(snapshot_id)
    teams = entity_mentions(question, inputs.team_sp_per_sprint)
    tasks = entity_mentions(question, (task.task_id for task in inputs.tasks))
    if len(tasks) == 1 and not teams:
        return "task", tasks[0]
    if len(teams) == 1 and not tasks:
        return "team", teams[0]
    if teams or tasks:
        return None
    # Carry focus only for references to the previous object, not a new general question.
    if not re.search(r"\b(?:эт\w*|он[аи]?|не[её]|них|его|е[её])\b|^\s*(?:почему|а дальше|подробнее)\s*[?!]*$", question.casefold()):
        return None
    row = db.query_one(
        "SELECT payload FROM public.assistant_messages WHERE conversation_id = %s "
        "AND context_revision = %s AND role = 'assistant' AND payload IS NOT NULL "
        "ORDER BY sequence_no DESC LIMIT 1", (conversation_id, revision),
    )
    refs = (row or {}).get("payload", {}).get("entity_refs", [])
    if len(refs) != 1:
        return None
    ref = refs[0]
    if ref.get("type") == "team" and ref.get("id") in inputs.team_sp_per_sprint:
        return "team", ref["id"]
    if ref.get("type") == "task" and any(task.task_id == ref.get("id") for task in inputs.tasks):
        return "task", ref["id"]
    return None


def pending_intent(conversation_id: UUID, revision: int) -> str | None:
    row = db.query_one("SELECT payload FROM public.assistant_messages "
                       "WHERE conversation_id = %s AND context_revision = %s "
                       "AND role = 'assistant' ORDER BY sequence_no DESC LIMIT 1",
                       (conversation_id, revision))
    payload = (row or {}).get("payload") or {}
    if payload.get("status") == "needs_clarification":
        return payload.get("pending_intent")
    return None


def facts(snapshot_id: UUID | None, question: str) -> str:
    if snapshot_id is None:
        return SYSTEM_HELP
    inputs, plan, _baseline, _options = snapshots.load(snapshot_id)
    words = set(re.findall(r"[\w.-]+", question.lower()))
    task_hits = [task for task in inputs.tasks if task.task_id.lower() in words]
    team_hits = {team for team in inputs.team_sp_per_sprint if team.lower() in words}
    selected_ids = {task.task_id for task in task_hits}
    if team_hits:
        selected_ids.update(task.task_id for task in inputs.tasks if task.team_id in team_hits)
    if not selected_ids:
        selected_ids = {task.task_id for task in inputs.tasks[:25]}
    selected_ids = set(list(sorted(selected_ids))[:60])
    tasks = [{"task_id": task.task_id, "team_id": task.team_id, "initiative_id": task.prodf_id,
              "summary": task.summary, "remaining_hours": str(task.demand_hh),
              "estimation_sp": str(task.sp_to_plan)}
             for task in inputs.tasks if task.task_id in selected_ids]
    schedule = [{"task_id": row.task_id, "decision": row.decision,
                 "start_sprint": row.start_sprint, "end_sprint": row.end_sprint,
                 "reason_code": row.reason_code, "reason_text": row.reason_text}
                for row in plan.schedule if row.task_id in selected_ids]
    kpis = [{"sprint": row.sprint_no, "code": row.kpi_code, "value": str(row.value),
             "kind": row.kind} for row in plan.kpis[:60]]
    alerts = [{"sprint": row.sprint_no, "type": row.alert_type, "entity": row.entity_id,
               "message": row.message} for row in plan.alerts[:30]]
    payload = {"snapshot_id": str(snapshot_id), "pi_id": plan.pi_id,
               "as_of_sprint": plan.as_of_sprint, "status": plan.status,
               "task_count": len(inputs.tasks), "team_count": len(inputs.team_sp_per_sprint),
               "tasks": tasks, "schedule": schedule, "kpis": kpis, "alerts": alerts,
               "selection_limited": len(selected_ids) < len(inputs.tasks)}
    text = json.dumps(payload, ensure_ascii=False)
    while len(text) > MAX_FACT_CHARS:
        largest = max(("tasks", "schedule", "kpis", "alerts"), key=lambda key: len(payload[key]))
        if not payload[largest]:
            raise ValueError("snapshot fact envelope is too large")
        payload[largest] = payload[largest][:len(payload[largest]) // 2]
        payload["selection_limited"] = True
        text = json.dumps(payload, ensure_ascii=False)
    return text


def parse_answer(raw: str, revision: int, newer_run_available: bool | None,
                 snapshot_id: UUID | None, selected_focus: tuple[str, str] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_model_json") from exc
    # Старые сохранённые ответы могут не содержать fact_refs; API модели
    # запрашивает поле обязательно для строгого JSON Schema режима Groq.
    required = set(ANSWER_SCHEMA["required"]) - {"fact_refs"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"fact_refs"}:
        raise ValueError("invalid_model_answer")
    if ("fact_refs" in value and
            (not isinstance(value["fact_refs"], list) or len(value["fact_refs"]) > 10 or
             any(not isinstance(ref, dict) or set(ref) != {"evidence_id", "field"} or
                 not isinstance(ref["evidence_id"], str) or not isinstance(ref["field"], str)
                 for ref in value["fact_refs"]))):
        raise ValueError("invalid_fact_refs")
    if (value["status"] not in {"answered", "needs_clarification", "insufficient_data"}
            or not isinstance(value["summary"], str) or not isinstance(value["explanation"], str)
            or not isinstance(value["clarification"], str)
            or not value["summary"].strip() or len(value["summary"]) > 3000
            or len(value["explanation"]) > 10000 or len(value["clarification"]) > 3000):
        raise ValueError("invalid_model_answer")
    if value["status"] == "needs_clarification" and not value["clarification"]:
        raise ValueError("missing_clarification")
    limitations = ([] if snapshot_id is not None else ["Разговор не привязан к прогону."])
    refs = ([{"type": selected_focus[0], "id": selected_focus[1], "context_revision": revision}]
            if selected_focus else [])
    return {**value, "fact_refs": value.get("fact_refs", []),
            "recommendations": [], "evidence_ids": [], "entity_refs": refs,
            "context_revision": revision, "newer_run_available": newer_run_available,
            "limitations": limitations}
