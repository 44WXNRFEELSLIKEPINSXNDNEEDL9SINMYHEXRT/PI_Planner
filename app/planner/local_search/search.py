"""Large-neighborhood repair around a feasible CP-SAT plan."""
from __future__ import annotations

import random
from itertools import combinations
from time import monotonic

from app.planner.local_search.solver import AllocationSolution, solve_allocation
from app.planner.model import Inputs, TaskInput


def _task_order(tasks: tuple[TaskInput, ...]) -> list[TaskInput]:
    return sorted(tasks, key=lambda task: (
        task.priority_rung is None,
        -(task.business_priority if task.business_priority is not None else task.priority_rung or 0),
        task.topo_order,
        task.task_id,
    ))


def _neighborhoods(
    inputs: Inputs,
    incumbent: AllocationSolution,
    *,
    limit: int,
) -> list[frozenset[str]]:
    """Deterministic small neighborhoods plus the 1-to-2 exchange operator."""
    tasks = {task.task_id: task for task in inputs.tasks}
    selected = incumbent.selected
    deferred = set(tasks) - selected
    ordered_deferred = [task.task_id for task in _task_order(tuple(tasks[key] for key in deferred))]
    ordered_selected = [task.task_id for task in _task_order(tuple(tasks[key] for key in selected))]
    result: list[frozenset[str]] = []
    seen: set[frozenset[str]] = set()

    def add(ids: set[str]) -> None:
        neighborhood = _close(inputs, ids, selected)
        if neighborhood and neighborhood not in seen and len(result) < limit:
            seen.add(neighborhood)
            result.append(neighborhood)

    # Atomic initiatives must be released as one unit, regardless of size.
    if incumbent is not None:
        for members in _initiative_groups(inputs).values():
            if any(task_id in deferred for task_id in members):
                add(set(members))

    # Repack each selected task on its own: this can move work to earlier
    # sprints, reduce borrowed capacity, or change engineers without changing selection.
    for task_id in ordered_selected:
        add({task_id})

    # One-for-two exchanges are necessary for the 80 -> 40 + 40 trap.
    for selected_id in ordered_selected:
        selected_task = tasks[selected_id]
        candidates = [
            task_id for task_id in ordered_deferred
            if tasks[task_id].team_id == selected_task.team_id
            or set(tasks[task_id].needed) & set(selected_task.needed)
        ]
        for first, second in combinations(candidates, 2):
            if tasks[first].team_id != tasks[second].team_id:
                continue
            add({selected_id, first, second})
            if len(result) >= limit:
                return result

    # Release a selected task and one competing deferred task together.
    for deferred_id in ordered_deferred:
        deferred_task = tasks[deferred_id]
        candidates = [
            selected_id for selected_id in ordered_selected
            if tasks[selected_id].team_id == deferred_task.team_id
            or set(tasks[selected_id].needed) & set(deferred_task.needed)
        ]
        for selected_id in candidates:
            add({selected_id, deferred_id})
            if len(result) >= limit:
                return result

    # A single moved task can fill a small unused gap.
    for task_id in ordered_deferred:
        add({task_id})
        if len(result) >= limit:
            return result

    # Repack two deferred tasks against an already selected task, even where
    # the shared bottleneck is an engineer rather than the task's home team.
    role_candidates: dict[int, list[str]] = {}
    for role_id in sorted({role for task in tasks.values() for role in task.needed}):
        role_candidates[role_id] = [
            task_id for task_id in ordered_deferred if role_id in tasks[task_id].needed
        ]
    for selected_id in ordered_selected:
        for role_id, deferred_ids in role_candidates.items():
            if role_id not in tasks[selected_id].needed:
                continue
            for first, second in combinations(deferred_ids, 2):
                add({selected_id, first, second})
                if len(result) >= limit:
                    return result
    return result


def _initiative_groups(inputs: Inputs) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for task in inputs.tasks:
        groups.setdefault(task.prodf_id, []).append(task.task_id)
    return groups


def _close(inputs: Inputs, ids: set[str], selected: frozenset[str]) -> frozenset[str]:
    """Release what a selection change needs: whole initiatives and, for each
    deferred task, its deferred predecessors (transitively)."""
    groups = _initiative_groups(inputs)
    by_task = {task.task_id: task.prodf_id for task in inputs.tasks}
    blockers: dict[str, set[str]] = {}
    for blocking, blocked, _gap in inputs.deps:
        if blocking in by_task and blocked in by_task:
            blockers.setdefault(blocked, set()).add(blocking)
    result = set(ids)
    frontier = list(ids)
    while frontier:
        task_id = frontier.pop()
        related = set(groups[by_task[task_id]])
        if task_id not in selected:
            related |= {item for item in blockers.get(task_id, ()) if item not in selected}
        for item in related - result:
            result.add(item)
            frontier.append(item)
    return frozenset(result)


def _expand_initiatives(inputs: Inputs, ids: set[str]) -> frozenset[str]:
    """Release whole initiatives: a partial one can never count as complete."""
    groups = _initiative_groups(inputs)
    by_task = {task.task_id: task.prodf_id for task in inputs.tasks}
    result = set(ids)
    for task_id in ids:
        result.update(groups[by_task[task_id]])
    return frozenset(result)


def _conflict_neighborhood(
    inputs: Inputs,
    incumbent: AllocationSolution,
    eligible_by_task: dict[str, dict[int, list[str]]],
    rng: random.Random,
    size: int,
) -> frozenset[str]:
    """A deferred task plus the selected tasks holding the resources it needs.

    Deferred tasks are drawn with a bias to high priority. Competitors are the
    selected tasks that use an eligible engineer, or the team's SP, in sprints
    the deferred task could occupy.
    """
    tasks = {task.task_id: task for task in inputs.tasks}
    selected = incumbent.selected
    deferred = [task.task_id for task in _task_order(tuple(
        task for task in inputs.tasks if task.task_id not in selected and task.needed
    ))]
    if not deferred:
        return frozenset()
    pick = deferred[min(len(deferred) - 1, int(rng.expovariate(0.35)))]
    core = _close(inputs, {pick}, selected)
    wanted = [tasks[task_id] for task_id in core if task_id not in selected]
    earliest = min((task.earliest_start_sprint for task in wanted), default=1)
    people = {
        engineer_id
        for task in wanted for role_id in task.needed
        for engineer_id in eligible_by_task.get(task.task_id, {}).get(role_id, [])
    }
    teams = {task.team_id for task in wanted}
    competitors = sorted({
        row.task_id for row in incumbent.assignments
        if row.sprint_no >= earliest and row.task_id not in core
        and (row.engineer_id in people or tasks[row.task_id].team_id in teams)
    })
    rng.shuffle(competitors)
    chosen = set(core)
    for task_id in competitors:
        if len(chosen) >= max(size, len(core) + 1):
            break
        chosen |= _close(inputs, {task_id}, selected)
    return frozenset(chosen)


def _random_neighborhood(
    inputs: Inputs,
    incumbent: AllocationSolution,
    rng: random.Random,
    size: int,
    eligible_by_task: dict[str, dict[int, list[str]]] | None = None,
) -> tuple[str, frozenset[str]]:
    """Structured random neighborhood of about `size` tasks, with its kind.

    Kinds: a resource conflict around a deferred task (twice as likely),
    everything of one team, everything touching one engineer, everything
    inside a sprint window, or a mixed sample.
    """
    tasks = {task.task_id: task for task in inputs.tasks}
    selected = sorted(incumbent.selected)
    deferred = sorted(set(tasks) - incumbent.selected)
    kinds = ["team", "engineer", "window", "mixed"]
    if deferred and eligible_by_task is not None:
        kinds += ["conflict", "conflict"]
    kind = rng.choice(kinds)
    if kind == "conflict":
        free = _conflict_neighborhood(inputs, incumbent, eligible_by_task or {}, rng, size)
        if free:
            return kind, free
        kind = "mixed"
    pool: list[str] = []
    if kind == "team":
        team = rng.choice(sorted({task.team_id for task in tasks.values()}))
        pool = [task_id for task_id, task in tasks.items() if task.team_id == team]
    elif kind == "engineer" and incumbent.assignments:
        engineer_id = rng.choice(sorted({row.engineer_id for row in incumbent.assignments}))
        touched = {row.task_id for row in incumbent.assignments if row.engineer_id == engineer_id}
        roles = {row.role_id for row in incumbent.assignments if row.engineer_id == engineer_id}
        pool = sorted(touched) + [task_id for task_id in deferred if roles & set(tasks[task_id].needed)]
    elif kind == "window" and selected:
        low = rng.randint(1, max(1, inputs.sprint_count))
        high = min(inputs.sprint_count, low + rng.randint(0, 2))
        pool = [task_id for task_id in selected
                if incumbent.starts[task_id] <= high and incumbent.ends[task_id] >= low]
        pool += [task_id for task_id in deferred if tasks[task_id].earliest_start_sprint <= high]
    if not pool:
        pool = selected + deferred
    rng.shuffle(pool)
    chosen: list[str] = []
    for task_id in pool:
        if task_id not in chosen:
            chosen.append(task_id)
        if len(chosen) >= size:
            break
    # Always mix in at least one deferred task so selection can change.
    if deferred and not any(task_id in deferred for task_id in chosen):
        chosen.append(rng.choice(deferred))
    return kind, _close(inputs, set(chosen), incumbent.selected)


def _signature(solution: AllocationSolution) -> tuple[object, ...]:
    return (
        tuple(sorted(solution.selected)),
        tuple((row.task_id, row.sprint_no, row.engineer_id, row.home_team_id, row.work_hours)
              for row in solution.assignments),
        tuple(solution.sp_shares),
    )


def _vector(solution: AllocationSolution) -> tuple[int, ...]:
    return tuple(int(value) for value in solution.objective_vector)


def improve_with_lns(
    inputs: Inputs,
    eligible_by_task: dict[str, dict[int, list[str]]],
    initial: AllocationSolution,
    *,
    replan_floor: int,
    dependency_mode: str,
    initiative_mode: str,
    priority_strategy: str,
    max_time_seconds: float,
    random_seed: int,
    max_neighborhoods: int = 32,
    max_stale_random: int = 60,
) -> AllocationSolution:
    """Adaptive LNS that spends the whole budget.

    Deterministic operators (one attempt in three) alternate with structured
    random neighborhoods whose size adapts: an exactly solved neighborhood
    without gain grows, one that times out shrinks. After `max_stale_random`
    fruitless random attempts the search releases every task at once; if that
    is solved exactly, the incumbent is a proven lexicographic optimum and the
    search stops, otherwise the size resets and the search goes on.
    `max_neighborhoods <= 0` disables the search.
    """
    if initial.solver_status == "OPTIMAL" or max_neighborhoods <= 0:
        return initial

    started = monotonic()
    rng = random.Random(random_seed)
    best = initial
    attempted = improved = stale = 0
    proven = False
    neighborhood_statuses: list[tuple[str, str, bool]] = []
    neighborhood_log: list[dict[str, object]] = []
    tried: set[tuple[frozenset[str], tuple[object, ...]]] = set()
    all_tasks = frozenset(task.task_id for task in inputs.tasks)
    total_tasks = len(all_tasks)
    size = min(total_tasks, 6)
    call_budget = max(0.2, min(20.0, max_time_seconds / 10))
    deterministic_cache: tuple[tuple[object, ...], list[frozenset[str]]] | None = None

    while not proven:
        remaining = max_time_seconds - (monotonic() - started)
        if remaining <= 0.05:
            break
        signature = _signature(best)
        if deterministic_cache is None or deterministic_cache[0] != signature:
            deterministic_cache = (
                signature, _neighborhoods(inputs, best, limit=max_neighborhoods * 4)
            )
        free = next((group for group in deterministic_cache[1]
                     if (group, signature) not in tried), None)
        # Cheap deterministic operators must not starve the larger random
        # neighborhoods that can actually move the late-level objectives.
        kind = "deterministic"
        if free is None or attempted % 3 != 0:
            if stale >= max_stale_random:
                kind, free = "full", all_tasks
            else:
                kind, free = _random_neighborhood(inputs, best, rng, size, eligible_by_task)
        if kind != "full" and (free, signature) in tried:
            stale += 1
            continue
        tried.add((free, signature))
        attempted += 1
        budget = remaining if kind == "full" else min(call_budget, remaining)
        call_started = monotonic()
        candidate = solve_allocation(
            inputs, eligible_by_task,
            replan_floor=replan_floor, dependency_mode=dependency_mode,
            initiative_mode=initiative_mode, priority_strategy=priority_strategy,
            max_time_seconds=budget,
            random_seed=random_seed + attempted,
            incumbent=best, free_task_ids=free,
        )
        before, after = _vector(best), _vector(candidate)
        accepted = after > before
        changed = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), None)
        if len(neighborhood_statuses) < 500:
            neighborhood_statuses.append((",".join(sorted(free)), candidate.solver_status, accepted))
            neighborhood_log.append({
                "kind": kind,
                "size": len(free),
                "seconds": round(monotonic() - call_started, 3),
                "solver_status": candidate.solver_status,
                "accepted": accepted,
                "changed_level": None if changed is None else (
                    candidate.objective_levels[changed]
                    if changed < len(candidate.objective_levels) else changed
                ),
                "delta": None if changed is None else after[changed] - before[changed],
                "bounds": list(candidate.objective_bounds[-2:]),
            })
        if accepted:
            improved += 1
            stale = 0
            best = candidate  # a neighborhood proof is not a global optimality proof
        elif kind != "deterministic":
            stale += 1
        if kind == "full":
            # Every task was free: an exact solve without gain proves optimality.
            proven = candidate.solver_status == "OPTIMAL" and not accepted
            stale = 0
            size = min(total_tasks, 6)
        elif kind != "deterministic":
            if candidate.solver_status == "OPTIMAL" and not accepted:
                size = min(total_tasks, size + max(1, size // 3))
            elif candidate.solver_status != "OPTIMAL":
                size = max(3, size - max(1, size // 4))

    return AllocationSolution(
        **{
            **best.__dict__,
            "solver_status": "OPTIMAL" if proven else initial.solver_status,
            "search_method": "full_cp_sat_seed_plus_lns",
            "neighborhoods_attempted": attempted,
            "neighborhoods_improved": improved,
            "neighborhood_statuses": tuple(neighborhood_statuses),
            "neighborhood_log": tuple(neighborhood_log),
            "wall_time_seconds": initial.wall_time_seconds + (monotonic() - started),
        }
    )


__all__ = ["improve_with_lns"]
