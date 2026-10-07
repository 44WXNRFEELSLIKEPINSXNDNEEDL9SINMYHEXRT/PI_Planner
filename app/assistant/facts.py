"""Typed, exhaustive read operations over immutable planner snapshots."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache, wraps
import json
from typing import Any
from uuid import UUID

from app.assistant import snapshots


def _cached_fact(function: Any) -> Any:
    """Snapshot IDs are immutable; return a fresh JSON value to every caller."""
    @lru_cache(maxsize=8)
    def cached(*args: Any) -> str:
        return json.dumps(function(*args), ensure_ascii=False, sort_keys=True)

    @wraps(function)
    def read(*args: Any) -> dict[str, Any]:
        return json.loads(cached(*args))

    return read


def plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, UUID)):
        return str(value)
    if is_dataclass(value):
        return plain(asdict(value))
    if hasattr(value, "__dict__"):
        return plain(vars(value))
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [plain(item) for item in value]
    return value


def _load(snapshot_id: UUID) -> tuple[Any, Any, dict[str, Any]]:
    inputs, plan, _baseline, _options = snapshots.load(snapshot_id)
    return inputs, plan, {"snapshot_id": str(snapshot_id), "pi_id": plan.pi_id,
                          "as_of_sprint": plan.as_of_sprint,
                          "algorithm": plan.params.get("algorithm"),
                          "formula_version": plan.params.get("formula_version"),
                          "actuals_upload_id": plan.actuals_upload_id}


def _loan_totals(assignments, sprint_count: int = 0) -> dict[str, Any]:
    by_sprint: dict[int, Decimal] = {}
    for row in assignments:
        if row.home_team_id != row.serving_team_id:
            by_sprint[row.sprint_no] = by_sprint.get(row.sprint_no, Decimal(0)) + row.hours
    return {"loan_hours": sum(by_sprint.values(), Decimal(0)),
            "loan_hours_by_sprint": [{"sprint_no": sprint, "hours": hours}
                                     for sprint, hours in sorted({**dict.fromkeys(range(1, sprint_count + 1), Decimal(0)),
                                                                 **by_sprint}.items())]}


@_cached_fact
def get_overview(snapshot_id: UUID) -> dict[str, Any]:
    inputs, plan, origin = _load(snapshot_id)
    decisions = Counter(row.decision for row in plan.schedule)
    alerts = Counter((row.alert_type, row.level) for row in plan.alerts)
    return plain({**origin, **_loan_totals(plan.assignments, getattr(inputs, "sprint_count", 0)),
                  "operation": "get_overview", "status": plan.status,
                  "task_count": len(inputs.tasks), "scheduled_count": len(plan.schedule),
                  "team_count": len({task.team_id for task in inputs.tasks}),
                  "assignment_count": len(plan.assignments),
                  "decisions": dict(decisions),
                  "deferred_by_reason": dict(Counter(row.reason_code for row in plan.schedule
                                                     if row.decision != "in_quarter")),
                  "planned_task_ids": [row.task_id for row in plan.schedule if row.decision == "in_quarter"],
                  "complete_initiatives": plan.params.get("initiatives_complete"),
                  "alerts_by_type_level": [{"type": kind, "level": level, "count": count}
                                           for (kind, level), count in sorted(alerts.items())],
                  "kpi_rows": len(plan.kpis), "complete_selection": True})


@_cached_fact
def get_team(snapshot_id: UUID, team_id: str) -> dict[str, Any]:
    inputs, plan, origin = _load(snapshot_id)
    team_tasks = [task for task in inputs.tasks if task.team_id == team_id]
    if not team_tasks and team_id not in inputs.team_sp_per_sprint:
        return {**origin, "operation": "get_team", "status": "not_found", "team_id": team_id}
    ids = {task.task_id for task in team_tasks}
    assignments = [row for row in plan.assignments if row.task_id in ids]
    return plain({**origin, **_loan_totals(assignments, getattr(inputs, "sprint_count", 0)),
                  "operation": "get_team", "status": "available", "team_id": team_id,
                  "complete_selection": True,
                  "tasks": [{"task_id": task.task_id, "summary": task.summary,
                             "initiative_id": task.prodf_id, "remaining_hh": task.demand_hh,
                             "remaining_sp": task.sp_to_plan, "status": task.status}
                            for task in team_tasks],
                  "schedule": [row for row in plan.schedule if row.task_id in ids],
                  "states": [row for row in plan.states if row.task_id in ids],
                  "assignments": assignments,
                  "alerts": [row for row in plan.alerts
                             if row.entity_id == team_id or row.entity_id in ids],
                  "capacity": [row for row in plan.team_capacity if row.team_id == team_id],
                  "demand": [row for row in plan.role_demands if row[0] == team_id]})


@_cached_fact
def get_task_trace(snapshot_id: UUID, task_id: str) -> dict[str, Any]:
    inputs, plan, origin = _load(snapshot_id)
    task = next((item for item in inputs.tasks if item.task_id == task_id), None)
    if task is None:
        return {**origin, "operation": "get_task_trace", "status": "not_found", "task_id": task_id}
    related = {task_id}
    for blocking, blocked, _gap in inputs.all_deps:
        if blocked == task_id:
            related.add(blocking)
    return plain({**origin, **_loan_totals([row for row in plan.assignments if row.task_id == task_id]),
                  "operation": "get_task_trace", "status": "available",
                  "complete_selection": True, "task": task,
                  "schedule": [row for row in plan.schedule if row.task_id == task_id],
                  "states": [row for row in plan.states if row.task_id == task_id],
                  "assignments": [row for row in plan.assignments if row.task_id == task_id],
                  "baseline": [row for row in plan.baseline if row.task_id == task_id],
                  "dependencies": [row for row in inputs.all_deps if task_id in row[:2]],
                  "dependency_schedule": [row for row in plan.schedule if row.task_id in related - {task_id}],
                  "alerts": [row for row in plan.alerts if row.entity_id == task_id]})


@_cached_fact
def get_metric(snapshot_id: UUID, code: str) -> dict[str, Any]:
    inputs, plan, origin = _load(snapshot_id)
    rows = [row for row in plan.kpis if row.kpi_code.casefold() == code.casefold()]
    if not rows:
        return {**origin, "operation": "get_metric", "status": "not_found", "code": code,
                "available_codes": sorted({row.kpi_code for row in plan.kpis})}
    return plain({**origin, "operation": "get_metric", "status": "available", "code": code,
                  "complete_selection": True, "rows": rows,
                  "baseline": plan.baseline, "total_baseline_rows": len(plan.baseline),
                  "period": {"sprint_count": inputs.sprint_count,
                             "last_reported_sprint": inputs.last_reported_sprint,
                             "fund_hours_per_fte": inputs.fund_hours_per_fte}})


@_cached_fact
def compare_runs(before_id: UUID, after_id: UUID) -> dict[str, Any]:
    before_inputs, before, before_origin = _load(before_id)
    after_inputs, after, after_origin = _load(after_id)
    if before.pi_id != after.pi_id:
        return {"operation": "compare_runs", "status": "incompatible_pi",
                "before": before_origin, "after": after_origin}
    old = {row.task_id: row for row in before.schedule}
    new = {row.task_id: row for row in after.schedule}
    shared = sorted(old.keys() & new.keys())
    changed = [{"task_id": task_id, "before": plain(old[task_id]), "after": plain(new[task_id])}
               for task_id in shared if old[task_id] != new[task_id]]
    old_kpis = {(row.kpi_code, row.sprint_no, row.kind): row for row in before.kpis}
    new_kpis = {(row.kpi_code, row.sprint_no, row.kind): row for row in after.kpis}
    kpi_changes = [{"code": key[0], "sprint": key[1], "kind": key[2],
                    "before": plain(old_kpis[key]), "after": plain(new_kpis[key])}
                   for key in sorted(old_kpis.keys() & new_kpis.keys())
                   if old_kpis[key] != new_kpis[key]]
    return {"operation": "compare_runs", "status": "available", "before": before_origin,
            "after": after_origin, "same_source": before_inputs.source_sha256 == after_inputs.source_sha256,
            "shared_tasks": len(shared), "changed_tasks": changed,
            "added_task_ids": sorted(new.keys() - old.keys()),
            "removed_task_ids": sorted(old.keys() - new.keys()),
            "kpi_changes": kpi_changes, "complete_selection": True,
            "interpretation": "Наблюдаемые различия; причины этим сравнением не установлены."}


@_cached_fact
def get_improvement_plan(snapshot_id: UUID) -> dict[str, Any]:
    """Rank observed missing competencies; do not claim a calculated hiring effect."""
    inputs, plan, origin = _load(snapshot_id)
    deferred = {row.task_id for row in plan.schedule if row.decision != "in_quarter"}
    reasons = Counter(row.reason_code for row in plan.schedule if row.task_id in deferred)
    roles: dict[int, dict[str, Any]] = {}
    skill_tasks: set[str] = set()
    for task in inputs.tasks:
        if task.task_id not in deferred:
            continue
        for role_id, hours in task.needed.items():
            candidates = [engineer.engineer_id for engineer in inputs.engineers
                          if (engineer.engineer_id, role_id) in inputs.coverage]
            if not candidates:
                role = roles.setdefault(role_id, {"role_id": role_id,
                    "role_name": task.role_names.get(role_id, str(role_id)),
                    "task_ids": [], "required_work_hours": Decimal(0)})
                role["task_ids"].append(task.task_id)
                role["required_work_hours"] += hours
            elif not any(inputs.skill_requirements.get((task.task_id, role_id), frozenset()).issubset(
                    inputs.engineer_skills.get(engineer_id, frozenset())) for engineer_id in candidates):
                skill_tasks.add(task.task_id)
    missing = sorted(roles.values(), key=lambda item: (-len(item["task_ids"]), item["role_name"]))
    return plain({**origin, "operation": "get_improvement_plan", "complete_selection": True,
                  "task_count": len(inputs.tasks), "selected_tasks": len(plan.in_quarter),
                  "deferred_tasks": len(deferred), "reason_counts": dict(reasons), "missing_roles": missing,
                  "skill_gap_tasks": sorted(skill_tasks), "effect_calculated": False})
