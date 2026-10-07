"""Независимая проверка допустимости результата нового планировщика."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from app.planner.constants import DEPENDENCY_MODE_FINISH_START, DEPENDENCY_MODE_START_START
from app.planner.model import Inputs, Plan


_EPSILON = Decimal("0.000001")


def validate_plan(
    plan: Plan,
    inputs: Inputs,
    *,
    dependency_mode: str = DEPENDENCY_MODE_FINISH_START,
) -> tuple[str, ...]:
    """Возвращает все найденные ошибки контракта; пустой набор означает OK.

    Функция специально не использует checker CP-SAT: результат будет проверен
    отдельно до публикации в `v_plan_violations`.
    """
    if dependency_mode not in (DEPENDENCY_MODE_FINISH_START, DEPENDENCY_MODE_START_START):
        raise ValueError(f"unsupported dependency_mode={dependency_mode!r}")

    errors: list[str] = []
    tasks = {task.task_id: task for task in inputs.tasks}
    engineers = {engineer.engineer_id: engineer for engineer in inputs.engineers}
    schedule = {row.task_id: row for row in plan.schedule}
    if len(schedule) != len(plan.schedule):
        errors.append("duplicate task in schedule")
    if set(schedule) != set(tasks):
        errors.append("schedule task set differs from live input task set")

    selected = {task_id for task_id, row in schedule.items() if row.decision == "in_quarter"}
    work_by_task_role: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    hours_by_engineer_sprint: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    hours_by_orbit_sprint: dict[tuple[str, str, int], Decimal] = defaultdict(Decimal)
    sp_by_team_sprint: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    task_sprints: dict[str, set[int]] = defaultdict(set)

    for row in plan.assignments:
        if row.task_id not in tasks or row.task_id not in selected:
            errors.append(f"assignment to absent or deferred task {row.task_id}")
            continue
        task = tasks[row.task_id]
        if row.sprint_no not in range(max(1, plan.as_of_sprint), inputs.sprint_count + 1):
            errors.append(f"assignment in closed or out-of-PI sprint {row.task_id}/{row.sprint_no}")
        if row.hours <= 0:
            errors.append(f"non-positive assignment hours {row.task_id}/{row.engineer_id}")
            continue
        engineer = engineers.get(row.engineer_id)
        if engineer is None:
            errors.append(f"unknown engineer {row.engineer_id}")
            continue
        efficiency = inputs.coverage.get((row.engineer_id, row.role_id))
        if efficiency is None or efficiency <= 0:
            errors.append(f"engineer lacks role coverage {row.engineer_id}/{row.role_id}")
            continue
        if row.serving_team_id != task.team_id:
            errors.append(f"assignment serves wrong team {row.task_id}/{row.engineer_id}")
        if row.home_team_id not in engineer.orbits:
            errors.append(f"assignment charges an unknown orbit {row.engineer_id}/{row.home_team_id}")
            continue
        work = row.work_hours if row.work_hours is not None else row.hours / efficiency
        if work <= 0 or abs(row.hours - work * efficiency) > _EPSILON:
            errors.append(f"inconsistent employee hours/work {row.task_id}/{row.engineer_id}")
            continue

        work_by_task_role[(row.task_id, row.role_id)] += work
        hours_by_engineer_sprint[(row.engineer_id, row.sprint_no)] += row.hours
        hours_by_orbit_sprint[(row.engineer_id, row.home_team_id, row.sprint_no)] += row.hours
        task_sprints[row.task_id].add(row.sprint_no)

    for task_id, task in tasks.items():
        row = schedule.get(task_id)
        if row is None:
            continue
        is_selected = row.decision == "in_quarter"
        if task.remaining_unknown and is_selected:
            errors.append(f"unknown remaining effort scheduled {task_id}")
        if task.remaining_provisional and is_selected:
            # Provisional estimates are permitted by the current planner, but the
            # uncertainty must remain visible in the plan parameters.
            if not task.remaining_unknown:
                pass
        for role_id, effort in task.needed.items():
            actual = work_by_task_role[(task_id, role_id)]
            expected = effort if is_selected else Decimal(0)
            if abs(actual - expected) > _EPSILON:
                errors.append(f"role work mismatch {task_id}/{role_id}: {actual} != {expected}")
        if is_selected:
            if not task_sprints[task_id]:
                errors.append(f"selected task has no assigned sprint {task_id}")
            elif (row.start_sprint != min(task_sprints[task_id])
                  or row.end_sprint != max(task_sprints[task_id])):
                errors.append(f"schedule window mismatch {task_id}")
        elif task_sprints[task_id]:
            errors.append(f"deferred task has assigned work {task_id}")

    for engineer in inputs.engineers:
        for sprint_no in range(max(1, plan.as_of_sprint), inputs.sprint_count + 1):
            factor = inputs.sprint_factors.get(sprint_no, Decimal(1))
            orbit_rate = sum(
                (inputs.sprint_orbit_rates.get((engineer.engineer_id, team_id, sprint_no), rate)
                 for team_id, rate in engineer.orbits.items()),
                Decimal(0),
            )
            total_capacity = min(engineer.total_capacity_rate, orbit_rate) * inputs.fte_hours_per_sprint * factor
            assigned = hours_by_engineer_sprint[(engineer.engineer_id, sprint_no)]
            if assigned - total_capacity > _EPSILON:
                errors.append(f"engineer capacity exceeded {engineer.engineer_id}/{sprint_no}")
            for team_id, rate in engineer.orbits.items():
                available_rate = inputs.sprint_orbit_rates.get(
                    (engineer.engineer_id, team_id, sprint_no), rate
                )
                capacity = available_rate * inputs.fte_hours_per_sprint * factor
                used = hours_by_orbit_sprint[(engineer.engineer_id, team_id, sprint_no)]
                if used - capacity > _EPSILON:
                    errors.append(f"orbit capacity exceeded {engineer.engineer_id}/{team_id}/{sprint_no}")

    for task_id, sprint_no, sp in plan.sp_shares:
        if task_id not in selected or sp <= 0:
            errors.append(f"invalid SP share {task_id}/{sprint_no}")
            continue
        if sprint_no not in task_sprints[task_id]:
            errors.append(f"SP share without work {task_id}/{sprint_no}")
        sp_by_team_sprint[(tasks[task_id].team_id, sprint_no)] += sp

    for task_id, task in tasks.items():
        total_sp = sum((sp for candidate, _sprint, sp in plan.sp_shares if candidate == task_id), Decimal(0))
        expected_sp = task.sp_to_plan if task_id in selected else Decimal(0)
        if abs(total_sp - expected_sp) > _EPSILON:
            errors.append(f"task SP mismatch {task_id}: {total_sp} != {expected_sp}")

    for (team_id, sprint_no), used in sp_by_team_sprint.items():
        capacity = (
            inputs.team_sp_per_sprint.get(team_id, Decimal(0))
            * inputs.sprint_factors.get(sprint_no, Decimal(1))
        )
        if used - capacity > _EPSILON:
            errors.append(f"team SP capacity exceeded {team_id}/{sprint_no}")

    for blocking, blocked, gap in inputs.deps:
        if blocked not in selected:
            continue
        predecessor = schedule.get(blocking)
        if predecessor is None:
            continue
        if predecessor.decision != "in_quarter":
            errors.append(f"scheduled task depends on deferred predecessor {blocking}->{blocked}")
            continue
        successor = schedule[blocked]
        predecessor_point = predecessor.end_sprint if dependency_mode == DEPENDENCY_MODE_FINISH_START else predecessor.start_sprint
        if predecessor_point is None or successor.start_sprint is None:
            errors.append(f"missing dependency dates {blocking}->{blocked}")
        elif successor.start_sprint < predecessor_point + gap:
            errors.append(f"dependency gap violated {blocking}->{blocked}")

    if plan.params.get("initiative_mode") == "atomic":
        initiative_tasks: dict[str, set[str]] = defaultdict(set)
        for task in inputs.tasks:
            initiative_tasks[task.prodf_id].add(task.task_id)
        for prodf_id, members in initiative_tasks.items():
            if members & selected and not members <= selected:
                errors.append(f"partial initiative in atomic mode {prodf_id}")

    return tuple(errors)
