"""Stability preferences must never remove a resource-feasible task."""
from dataclasses import replace

import pytest

from app import planner
from tests.test_planner import engineer, inputs, task


@pytest.mark.parametrize('source,previous,changes', [
    # A 20-hour role is smaller than half of the 80-hour task.
    (inputs([task('A', roles={1: 20, 2: 60})],
            [engineer('old', role_id=1), engineer('other', role_id=2)], sprint_count=1),
     {('A', 1): frozenset({'old'})}, 0),
    # Both people are needed: the previous engineer can only cover 20 of 100 hours.
    (inputs([task('A', roles={1: 100})],
            [engineer('old', rate='0.25'), engineer('new')], sprint_count=1),
     {('A', 1): frozenset({'old'})}, 1),
    # Work kept by the previous group is preserved even if no individual has half.
    (inputs([task('A', roles={1: 60})],
            [engineer(name, rate='0.25') for name in ('old1', 'old2', 'old3')], sprint_count=1),
     {('A', 1): frozenset({'old1', 'old2', 'old3'})}, 0),
])
def test_stability_is_a_soft_role_specific_preference(source, previous, changes):
    plan = planner.build_plan(
        replace(source, preferred_engineers=previous),
        algorithm=planner.ALGORITHM_LOCAL_SEARCH, max_time_seconds=5,
    )
    assert {row.task_id for row in plan.in_quarter} == {'A'}
    optimization = plan.params['optimization']
    assert optimization['independent_validation'] == 'passed'
    assert optimization['objective']['assignment_changes'] == changes
    index = optimization['objective_levels'].index('-assignment_changes')
    assert int(optimization['objective_vector'][index]) == -changes


def test_tiny_search_budget_keeps_feasible_greedy_plan():
    from app.planner.local_search.objective import evaluate_objective

    source = inputs([task('A', roles={1: 40}), task('B', roles={1: 40})],
                    [engineer('E')], sprint_count=1)
    greedy = planner.build_plan(source, simulate_next_pi=False)
    local = planner.build_plan(source, algorithm=planner.ALGORITHM_LOCAL_SEARCH,
                               max_time_seconds=0.000001)
    assert local.params['algorithm'] == planner.ALGORITHM_LOCAL_SEARCH
    assert {row.task_id for row in local.in_quarter} == {'A', 'B'}
    assert evaluate_objective(local, source).vector >= evaluate_objective(greedy, source).vector
    assert local.params['optimization']['solver_status'] != 'OPTIMAL'
