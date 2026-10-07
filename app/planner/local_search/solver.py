"""CP-SAT модель для совместного выбора задач и расписания ресурсов."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from time import monotonic
from typing import Any

from app.planner.constants import INITIATIVE_MODE_ATOMIC
from app.planner.model import Assignment, Inputs, Plan
from app.planner.priority import DEFAULT_PRIORITY_STRATEGY, effective_priority


ALGORITHM_LOCAL_SEARCH = "lns-cpsat@1"
_TIME_SCALE = 10_000


@dataclass(frozen=True)
class AllocationSolution:
    selected: frozenset[str]
    starts: dict[str, int]
    ends: dict[str, int]
    assignments: tuple[Assignment, ...]
    sp_shares: tuple[tuple[str, int, Decimal], ...]
    solver_status: str
    lexicographic_status: tuple[tuple[str, str], ...]
    objective_vector: tuple[str, ...]
    objective_levels: tuple[str, ...]
    objective_bounds: tuple[str, ...]
    wall_time_seconds: float
    work_scale: int
    hours_scale: int
    sp_scale: int
    search_method: str = "full_cp_sat"
    neighborhoods_attempted: int = 0
    neighborhoods_improved: int = 0
    neighborhood_statuses: tuple[tuple[str, str, bool], ...] = ()
    neighborhood_log: tuple[dict[str, object], ...] = ()


def _decimal_scale(values: list[Decimal]) -> int:
    places = max((max(0, -value.normalize().as_tuple().exponent) for value in values), default=0)
    return 10 ** max(2, places)


def _units(value: Decimal, scale: int, *, name: str) -> int:
    scaled = value * scale
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{name}={value} is not representable at scale {scale}")
    return int(scaled)


def _capacity_units(value: Decimal, scale: int) -> int:
    """Round resource capacity down to the persisted unit; never invent capacity."""
    return int((value * scale).to_integral_value(rounding=ROUND_FLOOR))


def plan_vector_units(plan: Plan, inputs: Inputs, *, priority_strategy: str) -> tuple[int, ...]:
    """Objective vector of any plan (valid for the model or not) in solver units."""
    from app.planner.local_search.objective import evaluate_objective

    objective = evaluate_objective(plan, inputs, priority_strategy=priority_strategy)
    vector = list(objective.vector)
    index = objective.loan_hours_index
    vector[index] = _units(Decimal(vector[index]), 100, name="seed loan hours")
    return tuple(int(value) for value in vector)


def allocation_from_plan(plan: Plan, inputs: Inputs, *, priority_strategy: str) -> AllocationSolution:
    """Convert a validated greedy plan into a feasible search incumbent."""
    from app.planner.local_search.objective import evaluate_objective

    objective = evaluate_objective(plan, inputs, priority_strategy=priority_strategy)
    vector = list(plan_vector_units(plan, inputs, priority_strategy=priority_strategy))
    levels = (
        tuple(f"complete_initiatives:{level}" for level in objective.priority_levels)
        + ("-lost_baseline_initiatives",)
        + tuple(f"complete_tasks:{level}" for level in objective.priority_levels)
        + ("-assignment_changes", "-baseline_delay", "-loan_hours", "-completion_sprint_sum",
           "-schedule_gaps", "-assignees", "-assignment_rows")
    )
    return AllocationSolution(
        selected=frozenset(row.task_id for row in plan.in_quarter),
        starts={row.task_id: row.start_sprint for row in plan.in_quarter},
        ends={row.task_id: row.end_sprint for row in plan.in_quarter},
        assignments=plan.assignments, sp_shares=plan.sp_shares,
        solver_status="FEASIBLE", lexicographic_status=(),
        objective_vector=tuple(str(value) for value in vector),
        objective_levels=levels, objective_bounds=(), wall_time_seconds=0.0,
        work_scale=max(_TIME_SCALE, _decimal_scale([v for t in inputs.tasks for v in t.needed.values()])),
        hours_scale=100,
        sp_scale=_decimal_scale([t.sp_to_plan for t in inputs.tasks] + list(inputs.team_sp_per_sprint.values())),
        search_method="greedy_seed",
    )


def solve_allocation(
    inputs: Inputs,
    eligible_by_task: dict[str, dict[int, list[str]]],
    *,
    replan_floor: int,
    dependency_mode: str,
    initiative_mode: str,
    priority_strategy: str = DEFAULT_PRIORITY_STRATEGY,
    max_time_seconds: float = 10.0,
    random_seed: int = 0,
    incumbent: AllocationSolution | None = None,
    free_task_ids: frozenset[str] | None = None,
    floor_vector: tuple[int, ...] | None = None,
    num_workers: int = 1,
    hint_solution: AllocationSolution | None = None,
) -> AllocationSolution:
    """Строит допустимое расписание CP-SAT с лексикографической целью.

    Все оценки переводятся в целые единицы без округления вниз. Для каждого
    уровня цели решатель сохраняет лучшее найденное значение как префикс
    следующего уровня. Неподтверждённый префикс отмечается как условный.
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise RuntimeError("algorithm=lns-cpsat requires the ortools dependency") from exc

    if max_time_seconds <= 0:
        raise ValueError("max_time_seconds must be positive")
    started = monotonic()
    tasks = tuple(inputs.tasks)
    task_by_id = {task.task_id: task for task in tasks}
    engineers = {engineer.engineer_id: engineer for engineer in inputs.engineers}
    if not tasks:
        return AllocationSolution(
            frozenset(), {}, {}, (), (), "OPTIMAL", (), (), (), (), 0.0, 100, 1_000_000, 100,
        )

    work_values = [hours for task in tasks for hours in task.needed.values()]
    sp_values = [task.sp_to_plan for task in tasks]
    sp_values.extend(inputs.team_sp_per_sprint.values())
    work_scale = max(_TIME_SCALE, _decimal_scale(work_values))
    sp_scale = _decimal_scale(sp_values)
    efficiencies = [
        inputs.coverage[(engineer_id, role_id)]
        for eligible in eligible_by_task.values()
        for role_id, engineer_ids in eligible.items()
        for engineer_id in engineer_ids
        if (engineer_id, role_id) in inputs.coverage
    ]
    efficiency_scale = _decimal_scale(efficiencies)
    # Assignment hours persist to two decimals; role work persists to four.
    hours_scale = 100

    model = cp_model.CpModel()
    selected_var = {task.task_id: model.NewBoolVar(f"selected_{i}") for i, task in enumerate(tasks)}
    work_vars: dict[tuple[str, int, str, str, int], Any] = {}
    hour_vars: dict[tuple[str, int, str, str, int], Any] = {}
    work_by_task_sprint: dict[tuple[str, int], list[Any]] = defaultdict(list)
    hours_by_engineer_sprint: dict[tuple[str, int], list[Any]] = defaultdict(list)
    hours_by_orbit_sprint: dict[tuple[str, str, int], list[Any]] = defaultdict(list)
    loan_hour_vars: list[Any] = []

    task_work_units: dict[str, int] = {
        task.task_id: _units(task.demand_hh, work_scale, name=f"task[{task.task_id}].demand_hh")
        for task in tasks
    }
    task_sp_units: dict[str, int] = {
        task.task_id: _units(task.sp_to_plan, sp_scale, name=f"task[{task.task_id}].sp")
        for task in tasks
    }

    eligible_for_cuts: dict[tuple[str, int], list[str]] = {}
    unlocked = set(task_by_id) if incumbent is None else set(free_task_ids or ())
    if not unlocked <= set(task_by_id):
        raise ValueError("free_task_ids contains an unknown task")

    # Tasks outside the neighborhood enter the model as constants: their hours
    # and SP are subtracted from the funds and their windows are fixed numbers,
    # so CP-SAT only searches over the released tasks.
    fixed_ids = set(task_by_id) - unlocked
    fixed_rows: dict[str, list[Assignment]] = defaultdict(list)
    fixed_work: dict[tuple[str, int, str, str, int], int] = defaultdict(int)
    fixed_sp: dict[tuple[str, int], int] = {}
    if incumbent is not None:
        for row in incumbent.assignments:
            if row.task_id in fixed_ids:
                fixed_rows[row.task_id].append(row)
        for task_id, sprint_no, sp in incumbent.sp_shares:
            if task_id in fixed_ids:
                fixed_sp[(task_id, sprint_no)] = _units(sp, sp_scale, name="fixed SP")
        for task_id in fixed_ids:
            selected_var[task_id] = model.NewConstant(int(task_id in incumbent.selected))

    for task_index, task in enumerate(tasks):
        z = selected_var[task.task_id]
        required_skills_by_role = {
            role_id: inputs.skill_requirements.get((task.task_id, role_id), frozenset())
            for role_id in task.needed
        }
        if task.task_id in fixed_ids:
            for row in fixed_rows[task.task_id]:
                key = (row.task_id, row.role_id, row.engineer_id, row.home_team_id, row.sprint_no)
                work = row.work_hours if row.work_hours is not None else (
                    row.hours / inputs.coverage[(row.engineer_id, row.role_id)])
                work_units = _units(work, work_scale, name="fixed work")
                hours_units = _units(row.hours, hours_scale, name="fixed hours")
                fixed_work[key] += work_units
                work_by_task_sprint[(task.task_id, row.sprint_no)].append(work_units)
                hours_by_engineer_sprint[(row.engineer_id, row.sprint_no)].append(hours_units)
                hours_by_orbit_sprint[(row.engineer_id, row.home_team_id, row.sprint_no)].append(hours_units)
                if row.home_team_id != task.team_id:
                    loan_hour_vars.append(hours_units)
            for role_id in task.needed:
                eligible_for_cuts[(task.task_id, role_id)] = [
                    engineer_id for engineer_id in eligible_by_task.get(task.task_id, {}).get(role_id, [])
                    if engineer_id in engineers and (engineer_id, role_id) in inputs.coverage
                ]
            continue
        if task.remaining_unknown or not task.needed or task_work_units[task.task_id] <= 0:
            model.Add(z == 0)
            continue

        any_missing_candidate = False
        for role_id, role_demand in task.needed.items():
            role_work: list[Any] = []
            eligible_ids = eligible_by_task.get(task.task_id, {}).get(role_id, [])
            eligible_ids = [
                engineer_id for engineer_id in eligible_ids
                if engineer_id in engineers
                and (engineer_id, role_id) in inputs.coverage
                and required_skills_by_role[role_id].issubset(
                    inputs.engineer_skills.get(engineer_id, frozenset())
                )
            ]
            eligible_for_cuts[(task.task_id, role_id)] = list(eligible_ids)
            if not eligible_ids:
                any_missing_candidate = True
            role_demand_units = _units(role_demand, work_scale, name=f"{task.task_id}/{role_id}.demand")
            for engineer_id in eligible_ids:
                engineer = engineers[engineer_id]
                efficiency = inputs.coverage[(engineer_id, role_id)]
                efficiency_units = _units(
                    efficiency, efficiency_scale, name=f"coverage[{engineer_id},{role_id}]"
                )
                for home_team_id, _rate in sorted(engineer.orbits.items()):
                    for sprint_no in range(max(1, replan_floor), inputs.sprint_count + 1):
                        factor = inputs.sprint_factors.get(sprint_no, Decimal(1))
                        orbit_rate = inputs.sprint_orbit_rates.get(
                            (engineer_id, home_team_id, sprint_no), engineer.orbits[home_team_id]
                        )
                        if orbit_rate <= 0 or factor <= 0:
                            continue
                        key = (task.task_id, role_id, engineer_id, home_team_id, sprint_no)
                        work = model.NewIntVar(0, role_demand_units, f"w_{task_index}_{role_id}_{engineer_id}_{home_team_id}_{sprint_no}")
                        max_hours = int(
                            (role_demand * efficiency * hours_scale).to_integral_value(rounding=ROUND_CEILING)
                        )
                        hours = model.NewIntVar(0, max_hours, f"h_{task_index}_{role_id}_{engineer_id}_{home_team_id}_{sprint_no}")
                        model.Add(
                            hours * work_scale * efficiency_scale
                            == work * efficiency_units * hours_scale
                        )
                        work_vars[key] = work
                        hour_vars[key] = hours
                        role_work.append(work)
                        work_by_task_sprint[(task.task_id, sprint_no)].append(work)
                        hours_by_engineer_sprint[(engineer_id, sprint_no)].append(hours)
                        hours_by_orbit_sprint[(engineer_id, home_team_id, sprint_no)].append(hours)
                        if home_team_id != task.team_id:
                            loan_hour_vars.append(hours)
            model.Add(sum(role_work) == role_demand_units * z)
        if any_missing_candidate:
            model.Add(z == 0)

    # Общий фонд сотрудника и отдельный фонд каждой команды-орбиты.
    engineer_capacity: dict[tuple[str, int], int] = {}
    for engineer in inputs.engineers:
        for sprint_no in range(max(1, replan_floor), inputs.sprint_count + 1):
            factor = inputs.sprint_factors.get(sprint_no, Decimal(1))
            rates = {
                team_id: inputs.sprint_orbit_rates.get(
                    (engineer.engineer_id, team_id, sprint_no), rate
                )
                for team_id, rate in engineer.orbits.items()
            }
            for team_id, rate in rates.items():
                capacity = rate * Decimal(inputs.fte_hours_per_sprint) * factor
                capacity_units = _capacity_units(capacity, hours_scale)
                model.Add(sum(hours_by_orbit_sprint[(engineer.engineer_id, team_id, sprint_no)]) <= capacity_units)
            total_rate = min(engineer.total_capacity_rate, sum(rates.values(), Decimal(0)))
            total_capacity = total_rate * Decimal(inputs.fte_hours_per_sprint) * factor
            total_units = _capacity_units(total_capacity, hours_scale)
            engineer_capacity[(engineer.engineer_id, sprint_no)] = total_units
            model.Add(sum(hours_by_engineer_sprint[(engineer.engineer_id, sprint_no)]) <= total_units)

    sprints = range(max(1, replan_floor), inputs.sprint_count + 1)

    def role_work_capacity(task_id: str, role_id: int, sprint_no: int) -> int:
        """Upper bound on role work (work units) the eligible people can do in a sprint."""
        total = 0
        for engineer_id in eligible_for_cuts.get((task_id, role_id), ()):
            efficiency = inputs.coverage[(engineer_id, role_id)]
            hours_units = engineer_capacity.get((engineer_id, sprint_no), 0)
            total += int((Decimal(hours_units) * work_scale / hours_scale / efficiency)
                         .to_integral_value(rounding=ROUND_CEILING))
        return total

    starts: dict[str, Any] = {}
    ends: dict[str, Any] = {}
    sp_vars: dict[tuple[str, int], Any] = {}
    active_vars: dict[tuple[str, int], Any] = {}
    running: dict[tuple[str, int], Any] = {}
    for task_index, task in enumerate(tasks):
        z = selected_var[task.task_id]
        if task.task_id in fixed_ids:
            chosen = task.task_id in incumbent.selected
            used = {row.sprint_no for row in fixed_rows[task.task_id]} if chosen else set()
            first = min(used) if used else inputs.sprint_count + 1
            last = max(used) if used else 0
            starts[task.task_id] = model.NewConstant(first)
            ends[task.task_id] = model.NewConstant(last)
            for sprint_no in sprints:
                active_vars[(task.task_id, sprint_no)] = model.NewConstant(int(sprint_no in used))
                running[(task.task_id, sprint_no)] = model.NewConstant(int(chosen and last >= sprint_no))
                share = fixed_sp.get((task.task_id, sprint_no), 0)
                if task_sp_units[task.task_id] > 0 and task_work_units[task.task_id] > 0:
                    sp_vars[(task.task_id, sprint_no)] = model.NewConstant(share)
            continue
        total_work = task_work_units[task.task_id]
        total_sp = task_sp_units[task.task_id]
        role_units = {
            role_id: _units(demand, work_scale, name=f"{task.task_id}/{role_id}.demand")
            for role_id, demand in task.needed.items()
        }
        sprint_work_vars: list[Any] = []
        first_values: list[Any] = []
        last_values: list[Any] = []
        start = model.NewIntVar(0, inputs.sprint_count + 1, f"start_{task_index}")
        end = model.NewIntVar(0, inputs.sprint_count, f"end_{task_index}")
        role_capacity = {
            (role_id, sprint_no): min(units, role_work_capacity(task.task_id, role_id, sprint_no))
            for role_id, units in role_units.items() for sprint_no in sprints
        }
        for sprint_no in sprints:
            work_s = model.NewIntVar(0, max(0, total_work), f"task_work_{task_index}_{sprint_no}")
            model.Add(work_s == sum(work_by_task_sprint[(task.task_id, sprint_no)]))
            active = model.NewBoolVar(f"task_active_{task_index}_{sprint_no}")
            active_vars[(task.task_id, sprint_no)] = active
            # Redundant cut: a sprint can absorb only what eligible people can do in it.
            absorb = min(total_work, sum(role_capacity[(r, sprint_no)] for r in role_units))
            model.Add(work_s <= absorb * active)
            model.Add(work_s >= active)
            sprint_work_vars.append(work_s)
            first_values.append(sprint_no * active + (inputs.sprint_count + 1) * (1 - active))
            last_values.append(sprint_no * active)
            # Linear forms of the window definition, for a stronger relaxation.
            model.Add(end >= sprint_no * active)
            model.Add(start <= sprint_no + (inputs.sprint_count + 1) * (1 - active))

            if total_sp > 0 and total_work > 0:
                # share = floor(SP*work/W) or that +1, written linearly:
                # SP*work - (W-1) <= W*share <= SP*work + W - 1.
                share = model.NewIntVar(0, total_sp, f"sp_share_{task_index}_{sprint_no}")
                model.Add(total_work * share >= total_sp * work_s - (total_work - 1))
                model.Add(total_work * share <= total_sp * work_s + total_work - 1)
                model.Add(share <= total_sp * active)
                sp_vars[(task.task_id, sprint_no)] = share

        model.Add(sum(sprint_work_vars) == total_work * z)
        if total_sp > 0 and total_work > 0:
            model.Add(sum(
                sp_vars[(task.task_id, sprint_no)] for sprint_no in sprints
            ) == total_sp * z)

        model.AddMinEquality(start, first_values)
        model.AddMaxEquality(end, last_values)
        model.Add(start >= max(replan_floor, task.earliest_start_sprint) * z)

        # Minimum duration: each role needs at least this many active sprints.
        min_duration = 1
        for role_id, units in role_units.items():
            per_sprint = sorted((role_capacity[(role_id, n)] for n in sprints), reverse=True)
            done, count = 0, 0
            for capacity in per_sprint:
                if done >= units:
                    break
                done += capacity
                count += 1
            min_duration = max(min_duration, count)
        actives = [active_vars[(task.task_id, n)] for n in sprints]
        model.Add(sum(actives) >= min_duration * z)
        model.Add(end - start + 1 >= sum(actives)).OnlyEnforceIf(z)
        model.Add(end >= max(replan_floor, task.earliest_start_sprint) + min_duration - 1).OnlyEnforceIf(z)
        # running[k] == "the task still has work in sprint k or later".
        # Then end == (floor-1)*z + sum(running): a time-indexed form whose
        # relaxation, with the energy cuts below, bounds completion sums well.
        floor_sprint = max(1, replan_floor)
        running_list: list[Any] = []
        for sprint_no in sprints:
            flag = model.NewBoolVar(f"running_{task_index}_{sprint_no}")
            later = [active_vars[(task.task_id, n)] for n in sprints if n >= sprint_no]
            for item in later:
                model.Add(flag >= item)
            model.Add(flag <= sum(later))
            running[(task.task_id, sprint_no)] = flag
            running_list.append(flag)
        for earlier, later_flag in zip(running_list, running_list[1:]):
            model.Add(earlier >= later_flag)
        model.Add(end == (floor_sprint - 1) * z + sum(running_list))
        starts[task.task_id] = start
        ends[task.task_id] = end

    # Energy cuts: work of tasks finished by sprint k fits into sprints <= k.
    for role_id in sorted({role for task in tasks for role in task.needed}):
        holders = sorted({
            engineer_id for (task_id, candidate_role), ids in eligible_for_cuts.items()
            if candidate_role == role_id for engineer_id in ids
        })
        cumulative = 0
        for sprint_no in sprints:
            cumulative += sum(
                int((Decimal(engineer_capacity.get((engineer_id, sprint_no), 0)) * work_scale
                     / hours_scale / inputs.coverage[(engineer_id, role_id)])
                    .to_integral_value(rounding=ROUND_CEILING))
                for engineer_id in holders
            )
            if sprint_no == inputs.sprint_count:
                break
            finished = [
                _units(task.needed[role_id], work_scale, name="cut")
                * (selected_var[task.task_id] - running[(task.task_id, sprint_no + 1)])
                for task in tasks if role_id in task.needed
            ]
            if finished:
                model.Add(sum(finished) <= cumulative)
    for team_id in sorted({task.team_id for task in tasks}):
        cumulative = 0
        for sprint_no in sprints:
            cumulative += _capacity_units(
                inputs.team_sp_per_sprint.get(team_id, Decimal(0))
                * inputs.sprint_factors.get(sprint_no, Decimal(1)), sp_scale)
            if sprint_no == inputs.sprint_count:
                break
            model.Add(sum(
                task_sp_units[task.task_id]
                * (selected_var[task.task_id] - running[(task.task_id, sprint_no + 1)])
                for task in tasks if task.team_id == team_id
            ) <= cumulative)

    # Redundant aggregate knapsacks: role work and team SP against whole-PI funds.
    for role_id in sorted({role for task in tasks for role in task.needed}):
        holders = sorted({
            engineer_id for (task_id, candidate_role), ids in eligible_for_cuts.items()
            if candidate_role == role_id for engineer_id in ids
        })
        role_fund = sum(
            int((Decimal(engineer_capacity.get((engineer_id, n), 0)) * work_scale / hours_scale
                 / inputs.coverage[(engineer_id, role_id)]).to_integral_value(rounding=ROUND_CEILING))
            for engineer_id in holders for n in sprints
        )
        demand_terms = [
            _units(task.needed[role_id], work_scale, name="cut") * selected_var[task.task_id]
            for task in tasks if role_id in task.needed
        ]
        if demand_terms:
            model.Add(sum(demand_terms) <= role_fund)
    for team_id in sorted({task.team_id for task in tasks}):
        team_fund = sum(
            _capacity_units(
                inputs.team_sp_per_sprint.get(team_id, Decimal(0))
                * inputs.sprint_factors.get(n, Decimal(1)), sp_scale)
            for n in sprints
        )
        model.Add(sum(
            task_sp_units[task.task_id] * selected_var[task.task_id]
            for task in tasks if task.team_id == team_id
        ) <= team_fund)

    # SP в проекте округляется до сотой. Доли выбираются из floor/ceil
    # пропорциональной работы, при этом их сумма ровно равна SP задачи.
    sp_by_team_sprint: dict[tuple[str, int], list[Any]] = defaultdict(list)
    for task in tasks:
        for sprint_no in range(max(1, replan_floor), inputs.sprint_count + 1):
            share = sp_vars.get((task.task_id, sprint_no))
            if share is not None:
                sp_by_team_sprint[(task.team_id, sprint_no)].append(share)
    for (team_id, sprint_no), shares in sp_by_team_sprint.items():
        capacity = (
            inputs.team_sp_per_sprint.get(team_id, Decimal(0))
            * inputs.sprint_factors.get(sprint_no, Decimal(1))
        )
        capacity_units = _capacity_units(capacity, sp_scale)
        model.Add(sum(shares) <= capacity_units)

    # Живая зависимая задача не может быть выбрана без блокирующей.
    task_ids = set(task_by_id)
    for blocking, blocked, gap in inputs.deps:
        if blocking not in task_ids or blocked not in task_ids:
            continue
        model.Add(selected_var[blocked] <= selected_var[blocking])
        if dependency_mode == "finish_start":
            anchor = ends[blocking]
        else:
            anchor = starts[blocking]
        model.Add(starts[blocked] >= anchor + gap).OnlyEnforceIf(selected_var[blocked])

    if initiative_mode == INITIATIVE_MODE_ATOMIC:
        initiative_tasks: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            initiative_tasks[task.prodf_id].append(task.task_id)
        for members in initiative_tasks.values():
            for task_id in members[1:]:
                model.Add(selected_var[task_id] == selected_var[members[0]])

    # Лексикографические компоненты цели, все приведены к максимизации.
    groups: dict[str, list[TaskInput]] = defaultdict(list)
    for task in tasks:
        groups[task.prodf_id].append(task)
    initiative_priority = {
        prodf_id: max(
            (effective_priority(task, priority_strategy, groups) for task in members
             if effective_priority(task, priority_strategy, groups) is not None),
            default=None,
        )
        for prodf_id, members in groups.items()
    }
    if priority_strategy == "task":
        raise ValueError("priority_strategy='task' is unsupported by lns-cpsat")
    levels: list[Decimal | None] = sorted(
        {value for value in initiative_priority.values() if value is not None}, reverse=True
    )
    if any(value is None for value in initiative_priority.values()):
        levels.append(None)

    complete_vars: dict[str, Any] = {}
    initiative_members: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        initiative_members[task.prodf_id].append(task.task_id)
    for index, (prodf_id, members) in enumerate(sorted(initiative_members.items())):
        complete = model.NewBoolVar(f"initiative_complete_{index}")
        model.Add(sum(selected_var[task_id] for task_id in members) == len(members)).OnlyEnforceIf(complete)
        model.Add(sum(selected_var[task_id] for task_id in members) <= len(members) - 1).OnlyEnforceIf(complete.Not())
        complete_vars[prodf_id] = complete

    objective_exprs: list[tuple[str, Any]] = []
    for level in levels:
        objective_exprs.append((
            f"complete_initiatives:{'unranked' if level is None else level}",
            sum(complete_vars[prodf_id] for prodf_id in initiative_members
                if initiative_priority[prodf_id] == level),
        ))

    baseline_initiatives: set[str] = set()
    baseline_tasks: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        baseline = inputs.baseline_schedule.get(task.task_id)
        if baseline is not None and baseline[0] == "in_quarter":
            baseline_tasks[task.prodf_id].append(task.task_id)
    for prodf_id, members in baseline_tasks.items():
        if len(members) == len(initiative_members[prodf_id]):
            baseline_initiatives.add(prodf_id)
    lost_baseline = sum(1 - complete_vars[prodf_id] for prodf_id in baseline_initiatives)
    objective_exprs.append(("-lost_baseline_initiatives", -lost_baseline))

    task_priority: dict[str, Decimal | None] = {}
    task_levels: list[Decimal | None] = []
    for task in tasks:
        task_priority[task.task_id] = initiative_priority[task.prodf_id]
    task_levels = sorted({value for value in task_priority.values() if value is not None}, reverse=True)
    if any(value is None for value in task_priority.values()):
        task_levels.append(None)
    for level in task_levels:
        objective_exprs.append((
            f"complete_tasks:{'unranked' if level is None else level}",
            sum(selected_var[task_id] for task_id in task_priority if task_priority[task_id] == level),
        ))

    change_vars: list[Any] = []
    for pair_index, ((task_id, role_id), preferred) in enumerate(sorted(inputs.preferred_engineers.items())):
        if task_id not in selected_var or not preferred:
            continue
        role_demand = task_by_id[task_id].needed.get(role_id, Decimal(0))
        if role_demand <= 0:
            continue
        role_units = _units(role_demand, work_scale, name=f"stability[{task_id},{role_id}]")
        retained_work = sum(
            variable for (candidate_task, candidate_role, engineer_id, _orbit, _sprint), variable
            in [*work_vars.items(), *fixed_work.items()]
            if candidate_task == task_id and candidate_role == role_id and engineer_id in preferred
        )
        kept = model.NewBoolVar(f"kept_role_pair_{pair_index}")
        # A soft stability indicator: keeping at least half of THIS role's
        # work with the previous group avoids the penalty. Smaller positive
        # contributions remain feasible; they merely count as a change.
        model.Add(2 * retained_work >= role_units).OnlyEnforceIf(kept)
        model.Add(2 * retained_work < role_units).OnlyEnforceIf(kept.Not())
        changed = model.NewBoolVar(f"changed_role_pair_{pair_index}")
        model.Add(changed == selected_var[task_id] - kept)
        change_vars.append(changed)
    objective_exprs.append(("-assignment_changes", -sum(change_vars)))

    delay_vars: list[Any] = []
    for index, (task_id, baseline) in enumerate(sorted(inputs.baseline_schedule.items())):
        old_end = baseline[2]
        if old_end is None or task_id not in selected_var:
            continue
        delay = model.NewIntVar(0, inputs.sprint_count + 1, f"baseline_delay_{index}")
        model.AddMaxEquality(delay, [
            0, ends[task_id] - old_end,
            (inputs.sprint_count + 1 - old_end) * (1 - selected_var[task_id]),
        ])
        delay_vars.append(delay)
    objective_exprs.append(("-baseline_delay", -sum(delay_vars)))
    objective_exprs.append(("-loan_hours", -sum(loan_hour_vars)))
    objective_exprs.append((
        "-completion_sprint_sum",
        -sum(ends[task.task_id] for task in tasks),
    ))
    # Plan shape: never traded against delivery, only breaks ties after it.
    fixed_gaps = 0
    for task_id in fixed_ids:
        used = {row.sprint_no for row in fixed_rows[task_id]}
        if task_id in incumbent.selected and used:
            fixed_gaps += max(used) - min(used) + 1 - len(used)
    gap_terms = [
        ends[task.task_id] - starts[task.task_id] + 1
        - sum(active_vars[(task.task_id, n)] for n in sprints)
        for task in tasks if task.task_id not in fixed_ids
    ]
    gap_vars: list[Any] = [fixed_gaps] if fixed_gaps else []
    for index, expression in enumerate(gap_terms):
        gap = model.NewIntVar(0, inputs.sprint_count, f"gap_{index}")
        # For a deferred task start=S+1, end=0: the raw term is negative.
        model.AddMaxEquality(gap, [0, expression])
        gap_vars.append(gap)
    objective_exprs.append(("-schedule_gaps", -sum(gap_vars)))
    person_work: dict[tuple[str, int, str], list[Any]] = defaultdict(list)
    for (task_id, role_id, engineer_id, _orbit, _sprint), variable in work_vars.items():
        person_work[(task_id, role_id, engineer_id)].append(variable)
    used_people: list[Any] = [len({key[:3] for key, units in fixed_work.items() if units > 0})]
    for index, ((task_id, role_id, _engineer), variables) in enumerate(sorted(person_work.items())):
        used = model.NewBoolVar(f"assignee_{index}")
        role_units = _units(task_by_id[task_id].needed[role_id], work_scale, name="assignee")
        model.Add(sum(variables) <= role_units * used)
        model.Add(sum(variables) >= used)
        used_people.append(used)
    objective_exprs.append(("-assignees", -sum(used_people)))
    used_rows: list[Any] = [sum(1 for units in fixed_work.values() if units > 0)]
    for index, (key, variable) in enumerate(sorted(work_vars.items())):
        used = model.NewBoolVar(f"row_{index}")
        role_units = _units(task_by_id[key[0]].needed[key[1]], work_scale, name="row")
        model.Add(variable <= role_units * used)
        model.Add(variable >= used)
        used_rows.append(used)
    objective_exprs.append(("-assignment_rows", -sum(used_rows)))

    last_snapshot: AllocationSolution | None = incumbent
    statuses: list[tuple[str, str]] = []
    objective_bounds: list[str] = []
    last_solver_status = "UNKNOWN"
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = max(1, int(num_workers))
    solver.parameters.random_seed = int(random_seed)
    solver.parameters.log_search_progress = False
    all_optimal = True

    def snapshot(current_solver: Any, status_name: str) -> AllocationSolution:
        chosen = frozenset(task_id for task_id, variable in selected_var.items() if current_solver.Value(variable))
        current_starts = {task_id: current_solver.Value(starts[task_id]) for task_id in chosen}
        current_ends = {task_id: current_solver.Value(ends[task_id]) for task_id in chosen}
        assignments: list[Assignment] = []
        for key, work_var in work_vars.items():
            task_id, role_id, engineer_id, home_team_id, sprint_no = key
            work_units = current_solver.Value(work_var)
            if work_units <= 0:
                continue
            hours_units = current_solver.Value(hour_vars[key])
            assignments.append(Assignment(
                task_id=task_id,
                sprint_no=sprint_no,
                engineer_id=engineer_id,
                role_id=role_id,
                hours=Decimal(hours_units) / hours_scale,
                home_team_id=home_team_id,
                serving_team_id=task_by_id[task_id].team_id,
                work_hours=Decimal(work_units) / work_scale,
            ))
        for task_id in fixed_ids:
            if task_id in chosen:
                assignments.extend(fixed_rows[task_id])
        shares = tuple(
            (task_id, sprint_no, Decimal(current_solver.Value(variable)) / sp_scale)
            for (task_id, sprint_no), variable in sp_vars.items()
            if current_solver.Value(variable) > 0
        )
        vector = tuple(str(current_solver.Value(expr)) for _name, expr in objective_exprs)
        return AllocationSolution(
            selected=chosen,
            starts=current_starts,
            ends=current_ends,
            assignments=tuple(sorted(assignments, key=lambda row: (
                row.task_id, row.sprint_no, row.role_id, row.engineer_id, row.home_team_id
            ))),
            sp_shares=shares,
            solver_status=status_name,
            lexicographic_status=tuple(statuses),
            objective_vector=vector,
            objective_levels=tuple(name for name, _expr in objective_exprs),
            objective_bounds=tuple(objective_bounds),
            wall_time_seconds=monotonic() - started,
        work_scale=work_scale,
            hours_scale=hours_scale,
            sp_scale=sp_scale,
        )

    def hint(solution: AllocationSolution) -> None:
        model.ClearHints()
        work: dict[tuple[str, int, str, str, int], Decimal] = defaultdict(Decimal)
        for row in solution.assignments:
            key = (row.task_id, row.role_id, row.engineer_id, row.home_team_id, row.sprint_no)
            work[key] += (row.work_hours if row.work_hours is not None else
                          row.hours / inputs.coverage[(row.engineer_id, row.role_id)])
        shares = {(task_id, sprint): sp for task_id, sprint, sp in solution.sp_shares}
        # Fixed tasks are constants (shared by value), never hinted.
        for task_id, variable in selected_var.items():
            if task_id not in fixed_ids:
                model.AddHint(variable, int(task_id in solution.selected))
        for key, variable in work_vars.items():
            model.AddHint(variable, _units(work[key], work_scale, name="hint work"))
        for key, variable in sp_vars.items():
            if key[0] not in fixed_ids:
                model.AddHint(variable, _units(shares.get(key, Decimal(0)), sp_scale, name="hint SP"))

    # Lexicographic lower bound: a level must not fall below the reference
    # (incumbent or greedy plan) only while all earlier levels equal it. Once an
    # earlier level is strictly better, later levels are free to trade off.
    reference = (tuple(int(v) for v in last_snapshot.objective_vector)
                 if last_snapshot is not None else floor_vector)
    prefix_equal = reference is not None
    constant_levels = {i for i, (_n, e) in enumerate(objective_exprs) if isinstance(e, int)}
    for index, (name, expression) in enumerate(objective_exprs):
        if index in constant_levels:
            statuses.append((name, "OPTIMAL"))
            objective_bounds.append(str(expression))
            continue
        if last_snapshot is not None:
            hint(last_snapshot)
        elif hint_solution is not None:
            hint(hint_solution)
        if reference is not None and prefix_equal and index < len(reference):
            model.Add(expression >= reference[index])
        remaining = max_time_seconds - (monotonic() - started)
        if remaining <= 0:
            statuses.append((name, "BUDGET_EXHAUSTED"))
            last_solver_status = "BUDGET_EXHAUSTED"
            all_optimal = False
            break
        # Share the budget: a level must not starve the ones after it.
        levels_left = sum(1 for i in range(index, len(objective_exprs)) if i not in constant_levels)
        # Without any feasible point the level gets the whole budget; once one
        # exists, a level must not starve the ones after it.
        solver.parameters.max_time_in_seconds = (
            remaining if last_snapshot is None or levels_left <= 1
            else min(remaining, remaining * 1.5 / levels_left)
        )
        model.Maximize(expression)
        status = solver.Solve(model)
        last_solver_status = solver.StatusName(status)
        if status == cp_model.UNKNOWN and last_snapshot is not None:
            # Out of time on this level: keep the known value and go on, so
            # the later levels are still optimised around it.
            statuses.append((name, "KEPT_INCUMBENT"))
            all_optimal = False
            if index >= len(last_snapshot.objective_vector):
                statuses[-1] = (name, "BUDGET_EXHAUSTED")
                objective_bounds.append(str(solver.BestObjectiveBound()))
                break
            value = int(last_snapshot.objective_vector[index])
            objective_bounds.append(str(solver.BestObjectiveBound()))
            if reference is not None and prefix_equal and index < len(reference) and value > reference[index]:
                prefix_equal = False
            model.Add(expression == value)
            continue
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            statuses.append((name, last_solver_status))
            all_optimal = False
            break
        value = solver.Value(expression)
        objective_bounds.append(str(solver.BestObjectiveBound()))
        statuses.append((name, "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE_PREFIX"))
        if status != cp_model.OPTIMAL:
            all_optimal = False
        candidate = snapshot(solver, last_solver_status)
        if (last_snapshot is None or tuple(map(int, candidate.objective_vector))
                >= tuple(map(int, last_snapshot.objective_vector))):
            last_snapshot = candidate
        if reference is not None and prefix_equal and index < len(reference) and value > reference[index]:
            prefix_equal = False
        model.Add(expression == value)

    if last_snapshot is None:
        raise RuntimeError(f"lns-cpsat found no feasible schedule: {last_solver_status}")
    if incumbent is not None and not statuses:
        return replace(
            incumbent,
            solver_status=last_solver_status,
            lexicographic_status=(("repair", last_solver_status),),
            wall_time_seconds=monotonic() - started,
        )
    if all_optimal and len(statuses) == len(objective_exprs):
        last_solver_status = "OPTIMAL"
    elif last_solver_status == "OPTIMAL":
        last_solver_status = "FEASIBLE"
    return AllocationSolution(
        **{
            **last_snapshot.__dict__,
            "solver_status": last_solver_status,
            "lexicographic_status": tuple(statuses),
            "objective_bounds": tuple(objective_bounds),
            "wall_time_seconds": monotonic() - started,
        }
    )


__all__ = ["ALGORITHM_LOCAL_SEARCH", "AllocationSolution", "solve_allocation"]
