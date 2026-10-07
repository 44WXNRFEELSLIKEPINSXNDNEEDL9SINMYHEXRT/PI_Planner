"""LNS must never publish a plan the greedy algorithm beats; neighborhoods stay sound."""
import random
from dataclasses import replace

from app import planner
from app.planner.local_search import search, solver
from tests.test_planner import engineer, inputs, task


def _source():
    return inputs(
        [task("A", prodf="P1", roles={1: 40}), task("B", prodf="P1", roles={1: 40}),
         task("C", prodf="P2", roles={1: 80}), task("D", prodf="P3", roles={1: 40})],
        [engineer("E")], sprint_count=2,
    )


def test_worse_than_greedy_result_falls_back_to_greedy(monkeypatch):
    source = _source()
    real = solver.solve_allocation

    def degraded(*args, **kwargs):
        result = real(*args, **kwargs)
        vector = tuple(str(int(v) - 1) for v in result.objective_vector)
        return replace(result, objective_vector=vector)

    monkeypatch.setattr("app.planner.core.solve_allocation", degraded)
    plan = planner.build_plan(source, algorithm=planner.ALGORITHM_LOCAL_SEARCH,
                              simulate_next_pi=False, max_time_seconds=3)
    greedy = planner.build_plan(source, simulate_next_pi=False)
    assert plan.params["fallback_reason"] == "lns result is lexicographically worse than greedy"
    assert {r.task_id for r in plan.in_quarter} == {r.task_id for r in greedy.in_quarter}


def test_random_neighborhood_releases_whole_initiatives_and_a_deferred_task():
    source = _source()
    greedy = planner.build_plan(source, simulate_next_pi=False)
    seed = solver.allocation_from_plan(greedy, source, priority_strategy="initiative")
    deferred = {t.task_id for t in source.tasks} - seed.selected
    assert deferred
    rng = random.Random(1)
    for _ in range(30):
        _kind, free = search._random_neighborhood(source, seed, rng, 2)
        assert free & deferred
        if free & {"A", "B"}:
            assert {"A", "B"} <= free


def test_conflict_neighborhood_releases_the_resource_holder():
    # E can do only one 80-hour task; the deferred one must meet its holder.
    source = inputs([task("HOLD", prodf="P1", roles={1: 80}), task("WANT", prodf="P2", roles={1: 80})],
                    [engineer("E")], sprint_count=1)
    greedy = planner.build_plan(source, simulate_next_pi=False)
    seed = solver.allocation_from_plan(greedy, source, priority_strategy="initiative")
    deferred = ({"HOLD", "WANT"} - seed.selected).pop()
    free = search._conflict_neighborhood(
        source, seed, {"HOLD": {1: ["E"]}, "WANT": {1: ["E"]}}, random.Random(0), 2)
    assert free == {"HOLD", "WANT"} and deferred in free


def test_closure_adds_deferred_predecessors_but_not_selected_ones():
    source = inputs([task("A"), task("B"), task("C")], [engineer("E")],
                    deps=(("A", "B", 0), ("B", "C", 0)))
    assert search._close(source, {"C"}, frozenset()) == {"A", "B", "C"}
    assert search._close(source, {"C"}, frozenset({"A"})) == {"B", "C"}


def test_business_priority_without_rung_is_searched_first():
    from decimal import Decimal

    high = replace(task("HIGH"), priority_rung=None, business_priority=Decimal(10))
    assert search._task_order((task("LOW", rung=1), high))[0] == high


def test_large_selection_keeps_exchange_operators_available():
    selected = [task(f"S{i:03}") for i in range(130)]
    deferred = [task(f"D{i:03}") for i in range(3)]
    source = inputs(selected + deferred, [engineer("E")])
    from types import SimpleNamespace

    seed = SimpleNamespace(selected=frozenset(row.task_id for row in selected))
    groups = search._neighborhoods(source, seed, limit=128)
    assert len(groups) <= 128
    assert any(group & seed.selected and len(group - seed.selected) >= 2 for group in groups)


def test_lns_exchange_releases_selected_dependency_successor():
    source = inputs(
        [task("A", roles={1: 80}), task("B", roles={2: 80}),
         task("C", roles={1: 20}), task("D", roles={1: 20}), task("F", roles={1: 20})],
        [engineer("E1", role_id=1), engineer("E2", role_id=2)],
        sprint_count=1, deps=(("A", "B", 0),),
    )
    greedy = planner.build_plan(source, simulate_next_pi=False)
    seed = solver.allocation_from_plan(greedy, source, priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY)
    eligible = {row.task_id: {role: [f"E{role}"] for role in row.needed} for row in source.tasks}
    result = search.improve_with_lns(
        source, eligible, seed, replan_floor=1, dependency_mode="finish_start",
        initiative_mode="greedy", priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY, max_time_seconds=2, random_seed=0,
    )
    assert seed.selected == {"A", "B"}
    assert result.selected == {"C", "D", "F"}
    assert tuple(map(int, result.objective_vector)) > tuple(map(int, seed.objective_vector))


def test_fully_fixed_neighborhood_does_not_spend_budget_solving(monkeypatch):
    from ortools.sat.python import cp_model

    source = _source()
    greedy = planner.build_plan(source, simulate_next_pi=False)
    seed = solver.allocation_from_plan(greedy, source, priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY)

    def unexpected_solve(*args, **kwargs):
        raise AssertionError("A fully fixed neighborhood has no objective left to optimize")

    monkeypatch.setattr(cp_model.CpSolver, "Solve", unexpected_solve)
    result = solver.solve_allocation(
        source, {row.task_id: {1: ["E"]} for row in source.tasks},
        incumbent=seed, free_task_ids=frozenset(), replan_floor=1,
        dependency_mode="finish_start", initiative_mode="greedy", max_time_seconds=2,
    )
    assert result.objective_vector == seed.objective_vector
    assert result.solver_status == "OPTIMAL"
    assert result.objective_bounds_scope == "neighborhood"


def test_improving_full_solve_proves_optimality_without_repeating(monkeypatch):
    source = inputs([task("A", roles={1: 80}), task("B", roles={1: 40}), task("C", roles={1: 40})],
                    [engineer("E")], sprint_count=1)
    seed = solver.allocation_from_plan(planner.build_plan(source, simulate_next_pi=False), source,
                                       priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY)
    eligible = {row.task_id: {1: ["E"]} for row in source.tasks}
    optimum = solver.solve_allocation(source, eligible, replan_floor=1, dependency_mode="finish_start",
                                     initiative_mode="greedy", max_time_seconds=2)
    calls = []

    def full_solve(*args, **kwargs):
        calls.append(kwargs["free_task_ids"])
        return optimum

    monkeypatch.setattr(search, "_neighborhoods", lambda *args, **kwargs: [])
    monkeypatch.setattr(search, "solve_allocation", full_solve)
    result = search.improve_with_lns(
        source, eligible, seed, replan_floor=1, dependency_mode="finish_start",
        initiative_mode="greedy", priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY, max_time_seconds=2,
        random_seed=0, max_stale_random=0,
    )
    assert result.selected == {"B", "C"}
    assert result.solver_status == "OPTIMAL"
    assert len(calls) == 1


def test_improved_prefix_cannot_fix_a_worse_lower_level(monkeypatch):
    from ortools.sat.python import cp_model
    from decimal import Decimal

    source = inputs([task("A", roles={1: 80}), task("B", roles={1: 20}), task("C", roles={1: 20})],
                    [engineer("E", orbits=("Team-1", "Team-2"))], sprint_count=1)
    seed = solver.allocation_from_plan(planner.build_plan(source, simulate_next_pi=False), source,
                                       priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY)
    real_solve = cp_model.CpSolver.Solve
    forced_statuses = []

    def controlled_solve(self, model, *args, **kwargs):
        proto = model.Proto()
        names = [proto.variables[index].name for index in proto.objective.vars]
        loan_indices = [i for i, variable in enumerate(proto.variables)
                        if variable.name.startswith("h_") and "_Team-2_" in variable.name]
        forced = model.Clone()
        loans = [forced.get_int_var_from_proto_index(i) for i in loan_indices]
        if names and all(name.startswith("h_") for name in names):
            # Deliberately offer a feasible but worse loan allocation after
            # improving delivery. The retained zero-loan plan must protect it.
            forced.Add(sum(loans) == 4000)
            forced_statuses.append(real_solve(self, forced, *args, **kwargs))
            return real_solve(self, model, *args, **kwargs)
        if not forced_statuses:
            forced.Add(sum(loans) == 0)
        return real_solve(self, forced, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "Solve", controlled_solve)
    result = solver.solve_allocation(
        source, {row.task_id: {1: ["E"]} for row in source.tasks}, incumbent=seed,
        free_task_ids=frozenset(row.task_id for row in source.tasks),
        replan_floor=1, dependency_mode="finish_start", initiative_mode="greedy", max_time_seconds=2,
    )
    assert forced_statuses == [cp_model.INFEASIBLE]
    assert result.selected == {"B", "C"}
    assert sum((row.hours for row in result.assignments if row.home_team_id != row.serving_team_id), Decimal(0)) == 0


def test_neutral_solution_opens_a_different_search_path(monkeypatch):
    single = inputs([task("A", roles={1: 80}), task("B", roles={1: 40}), task("C", roles={1: 40})],
                    [engineer("E1")], sprint_count=1)
    source = replace(single, engineers=(engineer("E1"), engineer("E2")),
                     coverage={("E1", 1): single.coverage[("E1", 1)], ("E2", 1): single.coverage[("E1", 1)]})
    seed = solver.allocation_from_plan(planner.build_plan(single, simulate_next_pi=False), source,
                                       priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY)
    alternative = replace(seed, assignments=tuple(replace(row, engineer_id="E2") for row in seed.assignments))
    eligible = {row.task_id: {1: ["E1", "E2"]} for row in source.tasks}
    optimum = solver.solve_allocation(source, eligible, replan_floor=1, dependency_mode="finish_start",
                                     initiative_mode="greedy", max_time_seconds=2)
    bases = []

    def controlled_repair(*args, **kwargs):
        bases.append(kwargs["incumbent"])
        if len(bases) == 1:
            return alternative
        assert search._signature(kwargs["incumbent"]) == search._signature(alternative)
        return optimum

    monkeypatch.setattr(search, "_neighborhoods", lambda *args, **kwargs: [])
    monkeypatch.setattr(search, "_random_neighborhood", lambda *args, **kwargs: ("mixed", frozenset({"A"})))
    monkeypatch.setattr(search, "solve_allocation", controlled_repair)
    result = search.improve_with_lns(
        source, eligible, seed, replan_floor=1, dependency_mode="finish_start", initiative_mode="greedy",
        priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY, max_time_seconds=2, random_seed=0, max_stale_random=1,
    )
    assert len(bases) == 2
    assert result.selected == {"A", "B", "C"}
    assert result.neighborhoods_improved == 1


def test_conflict_pivot_uses_weighted_priority_of_entire_initiative():
    from types import SimpleNamespace

    source = inputs([replace(task("A", prodf="P1", sp=1), own_rung=10),
                     replace(task("B", prodf="P1", sp=9), own_rung=0),
                     replace(task("C", prodf="P2", sp=1), own_rung=5)], [engineer("E")])
    seed = SimpleNamespace(selected=frozenset({"B"}), assignments=())
    rng = SimpleNamespace(expovariate=lambda rate: 0, shuffle=lambda items: None)
    free = search._conflict_neighborhood(source, seed, {}, rng, 1, priority_strategy="weighted")
    # P1 has priority (10*1+0*9)/10=1, even though only A is deferred.
    assert free == {"C"}
