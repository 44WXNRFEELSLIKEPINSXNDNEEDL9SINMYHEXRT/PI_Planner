"""Общий точный оценщик расписаний для поиска и сравнения кандидатов."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from app.planner.model import Inputs, Plan
from app.planner.priority import DEFAULT_PRIORITY_STRATEGY, effective_priority


@dataclass(frozen=True)
class ObjectiveValue:
    """Лексикографический вектор из `business_completion_v1`.

    Все компоненты оценки максимизируются. `priority_levels` задаёт категории
    приоритета от высшей к низшей; компоненты в таком же порядке идут в векторе.
    """

    priority_levels: tuple[str, ...]
    complete_initiatives: tuple[int, ...]
    lost_baseline_initiatives: int
    complete_tasks: tuple[int, ...]
    assignment_changes: int
    baseline_delay: int
    loan_hours: Decimal
    negative_completion_sprint_sum: int
    # Plan shape, strictly after every delivery criterion: idle sprints inside
    # a task window, distinct people per task role, distinct assignment rows.
    schedule_gaps: int = 0
    assignees: int = 0
    assignment_rows: int = 0

    @property
    def vector(self) -> tuple[int | Decimal, ...]:
        return (
            *self.complete_initiatives,
            -self.lost_baseline_initiatives,
            *self.complete_tasks,
            -self.assignment_changes,
            -self.baseline_delay,
            -self.loan_hours,
            self.negative_completion_sprint_sum,
            -self.schedule_gaps,
            -self.assignees,
            -self.assignment_rows,
        )

    @property
    def loan_hours_index(self) -> int:
        return len(self.complete_initiatives) + 1 + len(self.complete_tasks) + 2

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": "business_completion_v1",
            "priority_levels": list(self.priority_levels),
            "complete_initiatives": list(self.complete_initiatives),
            "lost_baseline_initiatives": self.lost_baseline_initiatives,
            "complete_tasks": list(self.complete_tasks),
            "assignment_changes": self.assignment_changes,
            "baseline_delay": self.baseline_delay,
            "loan_hours": str(self.loan_hours),
            "negative_completion_sprint_sum": self.negative_completion_sprint_sum,
            "schedule_gaps": self.schedule_gaps,
            "assignees": self.assignees,
            "assignment_rows": self.assignment_rows,
            "vector": [str(value) for value in self.vector],
        }


def evaluate_objective(
    plan: Plan,
    inputs: Inputs,
    *,
    priority_strategy: str = DEFAULT_PRIORITY_STRATEGY,
) -> ObjectiveValue:
    """Оценивает целый план, не опираясь на порядок его построения.

    План сначала группируется по инициативе. Завершённые ранее задачи не
    требуют повторного назначения; обещание baseline считается потерянным,
    если хотя бы одна живая задача обещанной инициативы не вошла в текущий PI.
    """
    tasks_by_id = {task.task_id: task for task in inputs.tasks}
    tasks_by_initiative: dict[str, list] = defaultdict(list)
    for task in inputs.tasks:
        tasks_by_initiative[task.prodf_id].append(task)

    initiative_priority: dict[str, Decimal | None] = {}
    for prodf_id, tasks in tasks_by_initiative.items():
        priority = [effective_priority(task, priority_strategy, tasks_by_initiative) for task in tasks]
        initiative_priority[prodf_id] = max((value for value in priority if value is not None), default=None)

    levels = sorted(
        {value for value in initiative_priority.values() if value is not None}, reverse=True
    )
    include_unranked = any(value is None for value in initiative_priority.values())
    if include_unranked:
        levels.append(None)  # unranked work is always after ranked work

    selected = {row.task_id for row in plan.schedule if row.decision == "in_quarter"}
    complete_initiatives_by_level: list[int] = []
    for level in levels:
        complete_initiatives_by_level.append(sum(
            1
            for prodf_id, tasks in tasks_by_initiative.items()
            if initiative_priority[prodf_id] == level
            and all(task.task_id in selected for task in tasks)
        ))

    baseline_tasks_by_initiative: dict[str, list[str]] = defaultdict(list)
    for task in inputs.tasks:
        baseline = inputs.baseline_schedule.get(task.task_id)
        if baseline is not None and baseline[0] == "in_quarter":
            baseline_tasks_by_initiative[task.prodf_id].append(task.task_id)
    baseline_promises = {
        prodf_id
        for prodf_id, promised_tasks in baseline_tasks_by_initiative.items()
        if len(promised_tasks) == len(tasks_by_initiative[prodf_id])
    }
    lost_baseline = sum(
        1
        for prodf_id in baseline_promises
        if not all(task.task_id in selected for task in tasks_by_initiative[prodf_id])
    )

    task_levels: dict[str, Decimal | None] = {}
    grouped_tasks: dict[Decimal | None, list[str]] = defaultdict(list)
    for task in inputs.tasks:
        if priority_strategy == "task":
            task_priority = Decimal(task.own_rung) if task.own_rung is not None else None
        else:
            task_priority = initiative_priority[task.prodf_id]
        task_levels[task.task_id] = task_priority
        grouped_tasks[task_priority].append(task.task_id)
    task_priority_levels = sorted(
        (value for value in grouped_tasks if value is not None), reverse=True
    )
    if None in grouped_tasks:
        task_priority_levels.append(None)
    complete_tasks = tuple(
        sum(task_id in selected for task_id in grouped_tasks[level])
        for level in task_priority_levels
    )

    changes = 0
    assigned_work: dict[tuple[str, int, str], Decimal] = defaultdict(Decimal)
    for row in plan.assignments:
        work = (row.work_hours if row.work_hours is not None else
                row.hours / inputs.coverage[(row.engineer_id, row.role_id)])
        assigned_work[(row.task_id, row.role_id, row.engineer_id)] += work
    task_by_id = {task.task_id: task for task in inputs.tasks}
    for key, previous in inputs.preferred_engineers.items():
        task = task_by_id.get(key[0])
        total = task.needed.get(key[1], Decimal(0)) if task else Decimal(0)
        if total <= 0:
            continue
        retained_work = sum((assigned_work[(key[0], key[1], engineer)] for engineer in previous), Decimal(0))
        kept = 2 * retained_work >= total
        if key[0] in selected and previous and not kept:
            changes += 1

    baseline_delay = 0
    schedule_by_id = {row.task_id: row for row in plan.schedule}
    for task_id, baseline in inputs.baseline_schedule.items():
        previous_end = baseline[2]
        if previous_end is None or task_id not in schedule_by_id:
            continue
        row = schedule_by_id[task_id]
        current_end = row.end_sprint if row.decision == "in_quarter" else inputs.sprint_count + 1
        baseline_delay += max(0, current_end - previous_end)

    loan_hours = sum(
        (row.hours for row in plan.assignments if row.home_team_id != row.serving_team_id),
        Decimal("0"),
    )
    completion_sprint_sum = sum(
        row.end_sprint or 0 for row in plan.schedule if row.decision == "in_quarter"
    )

    task_sprints: dict[str, set[int]] = defaultdict(set)
    people: set[tuple[str, int, str]] = set()
    rows: set[tuple[str, int, str, str, int]] = set()
    for row in plan.assignments:
        work = (row.work_hours if row.work_hours is not None else
                row.hours / inputs.coverage[(row.engineer_id, row.role_id)])
        if work <= 0 or row.task_id not in selected:
            continue
        task_sprints[row.task_id].add(row.sprint_no)
        people.add((row.task_id, row.role_id, row.engineer_id))
        rows.add((row.task_id, row.role_id, row.engineer_id, row.home_team_id, row.sprint_no))
    schedule_gaps = sum(max(used) - min(used) + 1 - len(used) for used in task_sprints.values())

    return ObjectiveValue(
        priority_levels=tuple("unranked" if value is None else str(value) for value in levels),
        complete_initiatives=tuple(complete_initiatives_by_level),
        lost_baseline_initiatives=lost_baseline,
        complete_tasks=complete_tasks,
        assignment_changes=changes,
        baseline_delay=baseline_delay,
        loan_hours=loan_hours,
        negative_completion_sprint_sum=-completion_sprint_sum,
        schedule_gaps=schedule_gaps,
        assignees=len(people),
        assignment_rows=len(rows),
    )
