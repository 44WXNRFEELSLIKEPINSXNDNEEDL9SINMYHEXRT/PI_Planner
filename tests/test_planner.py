"""Юнит-тесты планировщика: чистая логика, без PostgreSQL.

Проверяем ровно то, за что планировщик отвечает перед `v_plan_violations`:
ёмкость команды в SP, фонд часов по орбитам (сумма ВСЕХ орбит, а не «80 на
каждую»), зазор зависимостей, строгий режим ролей (замещений нет), переносы с
причиной M2/M3, алерты, KPI и базовая линия.

Живая база не нужна: `build_plan` не обращается к ней вовсе. Приёмка на живых
данных — `tools/run_planner.py` + `v_plan_violations` (docs/RUNBOOK.md).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import plan_quality, planner

T1, T2 = "Team-1", "Team-2"


def task(
    task_id: str,
    *,
    team: str = T1,
    sp: int = 5,
    rung: int = 1,
    topo: int = 1,
    earliest: int = 1,
    roles: dict[int, int] | None = None,
    prodf: str | None = None,
    status: str = "ToDo",
) -> planner.TaskInput:
    """Задача как её видит планировщик: часы по ролям и границы старта."""
    hours = roles if roles is not None else {1: 40}
    return planner.TaskInput(
        task_id=task_id,
        prodf_id=prodf or f"PRODF-{task_id}",
        team_id=team,
        status=status,
        priority_rung=rung,
        estimation_sp=Decimal(sp),
        summary=f"задача {task_id}",
        earliest_start_sprint=earliest,
        topo_order=topo,
        remaining={role_id: Decimal(value) for role_id, value in hours.items()},
        role_names={role_id: f"Роль {role_id}" for role_id in hours},
        estimate_disputed=False,
    )


def engineer(
    engineer_id: str,
    *,
    role_id: int = 1,
    rate: str = "1.00",
    orbits: tuple[str, ...] = (T1,),
    grade: str = "Middle",
) -> planner.EngineerInput:
    """Инженер: ставка делится между орбитами (парттаймер = 0.5 + 0.5)."""
    return planner.EngineerInput(
        engineer_id=engineer_id,
        role_id=role_id,
        grade=grade,
        total_capacity_rate=Decimal(rate),
        orbits={team: Decimal(rate) / len(orbits) for team in orbits},
    )


def test_continuing_task_keeps_previous_engineer_when_capacity_allows() -> None:
    current = inputs(
        [task("A", status="InProgress", roles={1: 10})],
        [engineer("E1", rate="1.00"), engineer("E2", rate="0.50")],
    )
    stable = replace(current, preferred_engineers={("A", 1): frozenset({"E2"})})
    plan = planner.build_plan(stable, simulate_next_pi=False)
    assert {row.engineer_id for row in plan.assignments if row.task_id == "A"} == {"E2"}
    assert plan.params["stability"]["kept_role_pairs"] == 1


def inputs(
    tasks: list[planner.TaskInput],
    engineers: list[planner.EngineerInput],
    *,
    deps: tuple[tuple[str, str, int], ...] = (),
    team_sp: dict[str, int] | None = None,
    sprint_count: int = 6,
    fte: int = 80,
    bus_factor: tuple[tuple[str, int, Decimal], ...] = (),
    all_tasks: tuple[tuple[str, str, Decimal, Decimal], ...] = (),
    conflicts: int = 0,
    coverage: dict[tuple[str, int], Decimal] | None = None,
    sprint_lengths: dict[int, int] | None = None,
    baseline_schedule: dict[str, tuple[str, int | None, int | None]] | None = None,
    done_in_sprint: dict[int, frozenset[str]] | None = None,
    last_reported_sprint: int = 0,
    skill_bus_factor: tuple[tuple[str, int, bool, bool], ...] = (),
) -> planner.Inputs:
    """Вход планировщика. Календарь — как в БД: спринты подряд, длина по умолчанию 14.

    `sprint_lengths` задаёт длину отдельных спринтов: короткий спринт
    (например, `{7: 8}`) даёт пропорционально меньший фонд — ровно так, как
    это делает `v_sprint_fund_factor` (ADR-017). Множитель округляется до
    4 знаков, как `ROUND(..., 4)` в Postgres.
    """
    lengths = {n: (sprint_lengths or {}).get(n, 14) for n in range(1, sprint_count + 1)}
    sprint_factors = {
        n: (Decimal(days) / Decimal(14)).quantize(Decimal("0.0001"))
        for n, days in lengths.items()
    }
    starts: dict[int, date] = {}
    cursor = date(2026, 6, 1)
    for n in range(1, sprint_count + 1):
        starts[n] = cursor
        cursor += timedelta(days=lengths[n])

    return planner.Inputs(
        pi_id="PI-TEST",
        sprint_count=sprint_count,
        fte_hours_per_sprint=fte,
        # Фонд квартала — сумма фондов спринтов, а не «sprint_count × 80»:
        # на календаре 92 дня это 6.5714 вместо 7 (ADR-017).
        fund_factor=sum(sprint_factors.values(), Decimal("0")),
        pi_days=sum(lengths.values()),
        sprint_factors=sprint_factors,
        team_sp_per_sprint={team: Decimal(cap) for team, cap in (team_sp or {T1: 100}).items()},
        tasks=tuple(tasks),
        engineers=tuple(engineers),
        # По умолчанию — «родная роль, efficiency = 1»: так же, как отдаёт
        # v_engineer_role_coverage на живых данных (ADR-012).
        coverage=(
            coverage
            if coverage is not None
            else {(row.engineer_id, row.role_id): Decimal("1") for row in engineers}
        ),
        deps=tuple(deps),
        sprints={
            n: (starts[n], starts[n] + timedelta(days=lengths[n] - 1))
            for n in range(1, sprint_count + 1)
        },
        all_tasks=tuple(all_tasks),
        bus_factor=tuple(bus_factor),
        estimate_conflicts=conflicts,
        estimate_conflict_warnings=conflicts,
        active_substitutions=0,
        # Факт спринтов и первоначальный план: без них KPI считаются как прогноз
        # по этому же прогону (ADR-023).
        last_reported_sprint=last_reported_sprint,
        done_in_sprint=done_in_sprint or {},
        baseline_schedule=baseline_schedule or {},
        task_prodf={row.task_id: row.prodf_id for row in tasks},
        skill_bus_factor=skill_bus_factor,
    )


def starts_of(plan: planner.Plan) -> dict[str, int | None]:
    return {row.task_id: row.start_sprint for row in plan.schedule}


def test_task_fits_in_first_sprint_with_own_engineer() -> None:
    plan = planner.build_plan(inputs([task("T-1")], [engineer("ENG-1")]))

    row = plan.schedule[0]
    assert (row.decision, row.start_sprint, row.end_sprint) == ("in_quarter", 1, 1)
    assert row.decision_reason is None
    assert row.forecast_end_date == date(2026, 6, 14)
    assert [
        (a.engineer_id, a.hours, a.home_team_id, a.serving_team_id) for a in plan.assignments
    ] == [("ENG-1", Decimal("40"), T1, T1)]
    assert plan.status == "ok"


def test_local_search_finds_one_to_two_task_exchange() -> None:
    source = inputs(
        [
            task("A-80", sp=1, prodf="P-80", roles={1: 80}),
            task("B-40", sp=1, prodf="P-40-A", roles={1: 40}),
            task("C-40", sp=1, prodf="P-40-B", roles={1: 40}),
        ],
        [engineer("ENG-1")],
        sprint_count=1,
        team_sp={T1: 10},
    )

    plan = planner.build_plan(
        source,
        algorithm=planner.ALGORITHM_LOCAL_SEARCH,
        simulate_next_pi=False,
        max_time_seconds=10,
    )

    assert {row.task_id for row in plan.in_quarter} == {"B-40", "C-40"}
    assert plan.params["algorithm"] == planner.ALGORITHM_LOCAL_SEARCH
    assert plan.params["optimization"]["independent_validation"] == "passed"
    assert plan.params["optimization"]["solver_status"] == "OPTIMAL"


def test_local_search_rejects_unconfirmed_unknown_role_coverage() -> None:
    source = replace(
        inputs(
            [task("T-1", roles={1: 10})],
            [engineer("ENG-1")],
            sprint_count=1,
            team_sp={T1: 10},
        ),
        skill_requirements={("T-1", 1): frozenset({99})},
        engineer_skills={"ENG-1": frozenset()},
    )

    plan = planner.build_plan(
        source,
        algorithm=planner.ALGORITHM_LOCAL_SEARCH,
        simulate_next_pi=False,
        max_time_seconds=10,
    )

    assert not plan.in_quarter
    assert plan.schedule[0].reason_code == planner.REASON_SKILL_UNAVAILABLE


def test_lns_repairs_greedy_one_to_two_exchange_neighborhood() -> None:
    from app.planner.local_search.search import improve_with_lns
    from app.planner.local_search.solver import AllocationSolution

    source = inputs(
        [
            task("A-80", sp=1, prodf="P-80", roles={1: 80}),
            task("B-40", sp=1, prodf="P-40-A", roles={1: 40}),
            task("C-40", sp=1, prodf="P-40-B", roles={1: 40}),
        ],
        [engineer("ENG-1")],
        sprint_count=1,
        team_sp={T1: 10},
    )
    greedy = planner.build_plan(source, simulate_next_pi=False)
    assert {row.task_id for row in greedy.in_quarter} == {"A-80"}
    incumbent = AllocationSolution(
        selected=frozenset({"A-80"}),
        starts={"A-80": 1},
        ends={"A-80": 1},
        assignments=tuple(greedy.assignments),
        sp_shares=tuple(greedy.sp_shares),
        solver_status="FEASIBLE",
        lexicographic_status=(),
        objective_vector=("1", "0", "1", "0", "0", "0", "-1"),
        objective_levels=(),
        objective_bounds=(),
        wall_time_seconds=0,
        work_scale=10000,
        hours_scale=100,
        sp_scale=100,
    )

    result = improve_with_lns(
        source,
        {task_id: {1: ["ENG-1"]} for task_id in ("A-80", "B-40", "C-40")},
        incumbent,
        replan_floor=1,
        dependency_mode=planner.DEFAULT_DEPENDENCY_MODE,
        initiative_mode=planner.INITIATIVE_MODE_GREEDY,
        priority_strategy=planner.DEFAULT_PRIORITY_STRATEGY,
        max_time_seconds=5,
        random_seed=0,
    )

    assert result.selected == frozenset({"B-40", "C-40"})
    assert result.search_method == "full_cp_sat_seed_plus_lns"
    assert result.neighborhoods_attempted >= 1
    assert result.neighborhoods_improved >= 1
    assert result.neighborhood_statuses
    assert any(accepted for _tasks, _status, accepted in result.neighborhood_statuses)


def test_confirmed_task_skill_filters_same_role_candidates() -> None:
    source = replace(
        inputs([task("T-1")], [engineer("E-1"), engineer("E-2")]),
        skill_requirements={("T-1", 1): frozenset({10})},
        skill_reviews=frozenset({("T-1", 1)}),
        engineer_skills={"E-1": frozenset({20}), "E-2": frozenset({10})},
        skill_names={10: "Kafka"},
    )
    plan = planner.build_plan(source)
    assert {row.engineer_id for row in plan.assignments} == {"E-2"}
    assert plan.params["skill_validation"]["status"] == "confirmed"


def test_missing_confirmed_skill_has_distinct_reason() -> None:
    source = replace(
        inputs([task("T-1")], [engineer("E-1")]),
        skill_requirements={("T-1", 1): frozenset({10})},
        skill_reviews=frozenset({("T-1", 1)}),
        engineer_skills={"E-1": frozenset({20})},
        skill_names={10: "Kafka"},
    )
    plan = planner.build_plan(source)
    assert plan.schedule[0].reason_code == planner.REASON_SKILL_UNAVAILABLE
    assert plan.schedule[0].reason_details["skill_gaps"] == [
        {"role": "Роль 1", "skills": ["Kafka"]}
    ]


def test_unreviewed_task_stack_is_explicit_in_run_quality() -> None:
    plan = planner.build_plan(inputs([task("T-1")], [engineer("E-1")]))
    assert plan.params["skill_validation"]["status"] == "unverified"
    assert plan.params["skill_validation"]["unreviewed"] == [{"task_id": "T-1", "role_id": 1}]


def test_hours_stretch_across_sprints() -> None:
    plan = planner.build_plan(inputs([task("T-1", roles={1: 120})], [engineer("ENG-1")]))

    row = plan.schedule[0]
    assert (row.start_sprint, row.end_sprint) == (1, 2)
    assert sorted(a.sprint_no for a in plan.assignments) == [1, 2]
    assert sum(a.hours for a in plan.assignments) == Decimal("120")


def test_engineer_fund_is_the_sum_of_all_orbits() -> None:
    """Парттаймер 0.5 + 0.5 даёт 80 ЧЧ за спринт СУММАРНО, а не 80 на команду."""
    part_timer = engineer("ENG-1", orbits=(T1, T2))
    tasks = [
        task("T-1", team=T1, topo=1),
        task("T-2", team=T2, topo=2),
        task("T-3", team=T1, topo=3),
    ]
    plan = planner.build_plan(inputs(tasks, [part_timer], team_sp={T1: 100, T2: 100}))

    assert starts_of(plan) == {"T-1": 1, "T-2": 1, "T-3": 2}
    per_sprint: dict[int, Decimal] = defaultdict(Decimal)
    for row in plan.assignments:
        per_sprint[row.sprint_no] += row.hours
    assert all(hours <= Decimal("80") for hours in per_sprint.values())


def test_short_sprint_gives_proportionally_less_hours() -> None:
    """ADR-017: короткий спринт даёт МЕНЬШЕ часов, а не «те же 80».

    8 дней из 14 — множитель 0.5714, ставка 1.0 даёт 45.712 ЧЧ вместо 80.
    Задача на 45 ЧЧ в такой спринт влезает, на 46 — уже нет; на полном
    спринте влезают обе. Иначе фонд квартала вылез бы за 92 дня календаря.
    """
    short = {7: 8}
    fits = planner.build_plan(
        inputs(
            [task("T-1", roles={1: 45}, earliest=7)],
            [engineer("ENG-1")],
            sprint_count=7,
            sprint_lengths=short,
        )
    )
    over = planner.build_plan(
        inputs(
            [task("T-1", roles={1: 46}, earliest=7)],
            [engineer("ENG-1")],
            sprint_count=7,
            sprint_lengths=short,
        )
    )
    full = planner.build_plan(
        inputs([task("T-1", roles={1: 46}, earliest=7)], [engineer("ENG-1")], sprint_count=7)
    )

    assert [(a.sprint_no, a.hours) for a in fits.assignments] == [(7, Decimal("45"))]
    assert over.schedule[0].decision == "deferred_next_pi"
    assert over.schedule[0].decision_reason == planner.DEFERRED_REASON
    assert over.assignments == ()
    assert [(a.sprint_no, a.hours) for a in full.assignments] == [(7, Decimal("46"))]


def test_pi_fund_is_proportional_to_calendar_length() -> None:
    """Фонд квартала = 80 ЧЧ × fund_factor, где fund_factor = дни / 14.

    Календарь Q3-2026: 6 × 14 + 8 = 92 дня → 6.5714 → 525.71 ЧЧ на ставку.
    525 ЧЧ за квартал помещаются, 526 — уже нет (иначе «седьмой спринт
    подарил бы 8 дней, которых в квартале нет»).
    """
    source = inputs(
        [task("T-1", roles={1: 525})],
        [engineer("ENG-1")],
        sprint_count=7,
        sprint_lengths={7: 8},
    )

    assert source.pi_days == 92
    assert source.fund_factor == Decimal("6.5714")

    plan = planner.build_plan(source)
    assert sum(a.hours for a in plan.assignments) == Decimal("525")
    assert plan.schedule[0].end_sprint == 7

    # 526 ЧЧ не помещаются при тех же ресурсах; это повод пересмотреть объём,
    # но не автоматическая рекомендация отменить бизнес-задачу.
    too_much = planner.build_plan(
        inputs(
            [task("T-1", roles={1: 526})],
            [engineer("ENG-1")],
            sprint_count=7,
            sprint_lengths={7: 8},
        )
    )
    assert too_much.schedule[0].decision == "deferred_next_pi"
    assert too_much.schedule[0].reason_details["next_pi_scenario"] == "needs_scope_or_capacity_review"
    assert "Отдельно" in too_much.schedule[0].reason_text
    assert too_much.assignments == ()


def test_next_pi_competition_does_not_make_a_feasible_task_cancelled() -> None:
    source = inputs(
        [task("A", roles={1: 80}, topo=1),
         task("B", roles={1: 80}, topo=2),
         task("C", roles={1: 80}, topo=3)],
        [engineer("E")], sprint_count=1,
    )
    plan = planner.build_plan(source)
    by_id = {row.task_id: row for row in plan.schedule}

    assert by_id["A"].decision == "in_quarter"
    assert by_id["B"].decision == "deferred_next_pi"
    assert by_id["C"].decision == "deferred_next_pi"
    assert by_id["C"].reason_details["next_pi_scenario"] == "competing_capacity"
    assert "отдельно она помещается" in by_id["C"].reason_text
    assert plan.params["next_pi_check"]["not_selected"] == ["C"]
    assert by_id["B"].forecast_end_date == date(2026, 6, 28)
    assert by_id["C"].forecast_end_date is None
    red = {alert.entity_id: alert for alert in plan.alerts if alert.level == "red"}
    assert red["PRODF-B"].payload["delay_days"] == 14
    assert red["PRODF-C"].payload["forecast_end_date"] is None


def test_calendar_lands_in_params() -> None:
    """Прогон несёт календарь: без него фонд 525.71 ЧЧ необъясним (ADR-017)."""
    plan = planner.build_plan(
        inputs([task("T-1")], [engineer("ENG-1")], sprint_count=7, sprint_lengths={7: 8})
    )

    calendar = plan.params["calendar"]
    assert calendar["sprint_count"] == 7
    assert calendar["pi_days"] == 92
    assert calendar["fund_factor"] == "6.5714"
    assert calendar["fund_hh_per_fte"] == "525.71"
    assert calendar["short_sprints"] == {"7": "0.5714"}
    assert calendar["pi_start"] == "2026-06-01"
    assert calendar["pi_end"] == "2026-08-31"


def test_full_calendar_has_no_short_sprints_in_params() -> None:
    """Календарь без коротких спринтов: множитель = числу спринтов, список пуст."""
    plan = planner.build_plan(inputs([task("T-1")], [engineer("ENG-1")]))

    calendar = plan.params["calendar"]
    assert calendar["fund_factor"] == "6.0000"
    assert calendar["fund_hh_per_fte"] == "480.00"
    assert calendar["short_sprints"] == {}


def test_own_orbit_goes_before_loan() -> None:
    engineers = [engineer("ENG-1", orbits=(T1,)), engineer("ENG-2", orbits=(T2,))]
    plan = planner.build_plan(
        inputs([task("T-1", team=T2)], engineers, team_sp={T1: 100, T2: 100})
    )

    assignment = plan.assignments[0]
    assert assignment.engineer_id == "ENG-2"
    assert assignment.home_team_id == assignment.serving_team_id == T2


def test_loan_when_the_team_has_no_engineer_of_that_role() -> None:
    plan = planner.build_plan(
        inputs(
            [task("T-1", team=T2)],
            [engineer("ENG-9", orbits=(T1,))],
            team_sp={T1: 100, T2: 100},
        )
    )

    assignment = plan.assignments[0]
    assert (assignment.home_team_id, assignment.serving_team_id) == (T1, T2)
    assert assignment.home_team_id != assignment.serving_team_id  # is_loan посчитает СУБД


def test_role_without_engineer_is_deferred_and_raises_orange() -> None:
    plan = planner.build_plan(inputs([task("T-1", roles={99: 40})], [engineer("ENG-1")]))

    row = plan.schedule[0]
    assert row.decision == "deferred_next_pi"
    assert row.decision_reason == planner.DEFERRED_REASON == "M2"
    assert row.start_sprint is None
    assert plan.assignments == ()

    orange = [alert for alert in plan.alerts if alert.level == "orange"]
    assert len(orange) == 1
    assert (orange[0].alert_type, orange[0].entity_type, orange[0].entity_id) == (
        "role_deficit",
        "role",
        "Роль 99",
    )
    assert orange[0].payload["verdict"] == "НАЙМ: закрыть некем"
    assert orange[0].payload["tasks"] == ["T-1"]
    assert orange[0].payload["demand_hh"] == "40"
    red = [alert for alert in plan.alerts if alert.level == "red"]
    assert [alert.entity_id for alert in red] == ["PRODF-T-1"]
    assert red[0].alert_type == "deadline_miss"


def test_orange_alert_counts_ready_work_delayed_within_pi() -> None:
    plan = planner.build_plan(inputs(
        [task("A", roles={1: 80}, topo=1), task("B", roles={1: 80}, topo=2)],
        [engineer("ENG-1")],
    ))
    assert {row.task_id: row.start_sprint for row in plan.schedule} == {"A": 1, "B": 2}
    orange = [alert for alert in plan.alerts if alert.level == "orange"]
    assert len(orange) == 1
    assert orange[0].payload["demand_hh"] == "160"
    assert Decimal(orange[0].payload["supply_hh"]) == 80
    assert orange[0].payload["tasks"] == ["A", "B"]

def test_dependency_gap_is_kept() -> None:
    tasks = [task("A", roles={1: 80}, topo=1), task("B", roles={1: 80}, topo=2)]
    plan = planner.build_plan(
        inputs(tasks, [engineer("ENG-1"), engineer("ENG-2")], deps=(("A", "B", 1),))
    )

    starts = starts_of(plan)
    assert starts == {"A": 1, "B": 2}
    assert starts["B"] >= starts["A"] + 1  # min_gap_sprints из task_dependencies


def test_deferred_blocker_defers_the_dependent_with_m3() -> None:
    """Задача чужой команды, которую держит перенесённая блокирующая, — тоже перенос."""
    tasks = [
        task("A", team=T1, roles={99: 40}, topo=1),
        task("B", team=T2, roles={1: 40}, topo=2),
    ]
    plan = planner.build_plan(
        inputs(
            tasks,
            [engineer("ENG-1", orbits=(T2,))],
            deps=(("A", "B", 1),),
            team_sp={T1: 100, T2: 100},
        )
    )

    reasons = {row.task_id: row.decision_reason for row in plan.schedule}
    assert reasons["A"] == planner.DEFERRED_REASON == "M2"
    assert reasons["B"] == planner.DEFERRED_REASON_BLOCKED == "M3"
    assert starts_of(plan) == {"A": None, "B": None}
    assert plan.assignments == ()


def test_team_sp_capacity_pushes_the_task_to_the_next_sprint() -> None:
    tasks = [task("A", sp=5, topo=1), task("B", sp=5, topo=2)]
    plan = planner.build_plan(
        inputs(tasks, [engineer("ENG-1"), engineer("ENG-2")], team_sp={T1: 5})
    )

    assert starts_of(plan) == {"A": 1, "B": 2}


def test_zero_remaining_live_task_requires_etc_instead_of_symbolic_assignment() -> None:
    """Исчерпанная смета не доказывает завершение незакрытой задачи."""
    plan = planner.build_plan(inputs([task("T-0", roles={1: 0})], [engineer("ENG-1")]))

    row = plan.schedule[0]
    assert row.decision == "deferred_next_pi"
    assert row.reason_code == planner.REASON_ETC_REQUIRED
    assert plan.assignments == ()


def test_explicit_etc_is_planned_even_when_spent_exceeds_original_estimate() -> None:
    # Планировщик получает 40 ЧЧ подтверждённого ETC, а не estimate-spent=0.
    plan = planner.build_plan(inputs([task("T-0", roles={1: 40}, status="InProgress")], [engineer("ENG-1")]))

    assert plan.schedule[0].decision == "in_quarter"
    assert sum((row.hours for row in plan.assignments), Decimal("0")) == Decimal("40")


def test_params_validate_the_estimate_source() -> None:
    """Ответ организаторов №4: планировщик обязан проверить и показать расхождения."""
    plan = planner.build_plan(inputs([task("T-1")], [engineer("ENG-1")], conflicts=25))

    assert plan.params["estimate_source"] == planner.ESTIMATE_SOURCE == "matrix_column_sum"
    assert plan.params["estimate_validated"] is True
    assert plan.params["estimate_conflicts"] == 25
    assert plan.params["substitution_mode"] == "rejected"
    assert plan.params["active_substitutions"] == 0
    assert plan.params["live_tasks"] == 1


def test_baseline_and_kpis_on_the_first_run() -> None:
    tasks = [
        task("A", roles={1: 40}, topo=1, prodf="PRODF-1"),
        task("B", roles={99: 40}, topo=2, prodf="PRODF-2"),
    ]
    plan = planner.build_plan(
        inputs(
            tasks,
            [engineer("ENG-1")],
            bus_factor=(("Роль 1", 1, Decimal("40")),),
            skill_bus_factor=(("Python", 1, True, True), ("SQL", 3, True, False)),
            all_tasks=(
                ("A", "ToDo", Decimal("5"), Decimal("40")),
                ("B", "ToDo", Decimal("5"), Decimal("40")),
                ("D", "Done", Decimal("3"), Decimal("0")),
            ),
        )
    )

    # Базовая линия — только по живым задачам, и только на as_of_sprint = 0.
    assert [(row.task_id, row.planned_sp, row.committed) for row in plan.baseline] == [
        ("A", Decimal("5"), True),
        ("B", Decimal("5"), False),
    ]

    by_code = {row.kpi_code: row for row in plan.kpis if row.kpi_code != "say_do_ratio"}
    assert set(by_code) == {"pi_predictability", "bus_factor"}

    # ТЗ: знаменатель — инициативы, ВКЛЮЧЁННЫЕ В ПЕРВОНАЧАЛЬНЫЙ ПЛАН, а не весь
    # бэклог. План обещал завершить только PRODF-1 (все её задачи в квартале),
    # PRODF-2 он завершить не обещал — значит и спроса с него нет.
    predictability = by_code["pi_predictability"]
    assert predictability.kind == "forecast"
    assert predictability.value == Decimal("100.00")
    assert predictability.target_min == Decimal("80")
    assert predictability.details["committed_initiatives"] == ["PRODF-1"]
    assert predictability.details["on_track_initiatives"] == ["PRODF-1"]
    # Факта ещё не загружали — строки kind = actual быть не должно.
    assert [row.kind for row in plan.kpis if row.kpi_code == "pi_predictability"] == ["forecast"]

    # Bus Factor считается ПО КОМПЕТЕНЦИЯМ (ТЗ), а не по ролям.
    assert by_code["bus_factor"].value == Decimal("1")
    assert by_code["bus_factor"].kind == "actual"
    assert by_code["bus_factor"].details["critical"] == ["Python"]
    assert by_code["bus_factor"].details["single_holder_n"] == 1
    assert by_code["bus_factor"].target_min == Decimal("2")  # онбординг: Bus Factor > 1

    say_do = [row for row in plan.kpis if row.kpi_code == "say_do_ratio"]
    assert [row.sprint_no for row in say_do] == [1, 2, 3, 4, 5, 6]
    assert say_do[0].value == Decimal("100.00")
    assert all(row.value is None and row.calculation_status == "no_plan" for row in say_do[1:])
    assert all(row.kind == "forecast" for row in say_do)
    assert say_do[0].target_min == Decimal("90") and say_do[0].target_max == Decimal("105")

    states = {row.task_id: row for row in plan.states}
    assert (states["D"].status, states["D"].remaining_hh) == ("Done", Decimal("0"))
    assert (states["B"].status, states["B"].forecast_end_sprint) == ("Deferred", None)
    assert (states["A"].status, states["A"].forecast_end_sprint) == ("ToDo", 1)
    assert all(row.as_of_sprint == 0 for row in plan.states)


def test_replan_has_no_baseline_and_snapshots_its_own_sprint() -> None:
    plan = planner.build_plan(
        inputs(
            [task("T-1")],
            [engineer("ENG-1")],
            all_tasks=(("T-1", "ToDo", Decimal("5"), Decimal("40")),),
        ),
        as_of_sprint=3,
    )

    assert plan.baseline == ()
    assert {row.as_of_sprint for row in plan.states} == {3}
    assert plan.params["baseline_starts_used"] is False


def test_yellow_alert_when_a_task_with_dependents_shifts() -> None:
    tasks = [
        task("A", roles={1: 80}, topo=1),
        task("B", roles={1: 80}, topo=2),
        task("C", roles={1: 80}, topo=3),
    ]
    deps = (("A", "B", 1), ("B", "C", 1))
    plain = inputs(tasks, [engineer("ENG-1")], deps=deps)

    shifted = planner.build_plan(
        inputs(tasks, [engineer("ENG-1")], deps=deps,
               baseline_schedule={"B": ("in_quarter", 2, 2), "C": ("in_quarter", 3, 3)}),
        as_of_sprint=2, baseline_starts={"B": 2},
    )
    yellow = [alert for alert in shifted.alerts if alert.level == "yellow"]
    assert [alert.entity_id for alert in yellow] == ["B"]
    assert yellow[0].alert_type == "cascade_shift"
    assert yellow[0].payload["baseline_start_sprint"] == 2
    assert yellow[0].payload["new_start_sprint"] == 3
    assert yellow[0].payload["dependents"] == ["C"]
    assert yellow[0].payload["affected_dependents"][0]["delay_sprints"] == 1

    # Первый прогон: сравнивать не с чем — жёлтых алертов нет.
    first = planner.build_plan(plain)
    assert not [alert for alert in first.alerts if alert.level == "yellow"]


def test_continuing_work_without_downstream_delay_is_not_a_cascade() -> None:
    tasks = [task("A", roles={1: 160}, topo=1), task("B", roles={1: 80}, topo=2)]
    plan = planner.build_plan(
        inputs(tasks, [engineer("ENG-1")], deps=(("A", "B", 1),),
               baseline_schedule={"A": ("in_quarter", 1, 3), "B": ("in_quarter", 4, 4)}),
        as_of_sprint=2,
    )
    assert not [alert for alert in plan.alerts if alert.level == "yellow"]


def test_say_do_ratio_compares_fact_with_the_original_promise() -> None:
    """ТЗ: фактически выполненные SP / первоначально запланированные SP.

    План Недели 0 обещал закрыть A и B (по 5 SP) в спринте 1. По факту спринта 1
    выполнена только A — показатель именно первого спринта 50%, и это ФАКТ
    (kind = actual), а не прогноз.
    """
    tasks = [task("A", roles={1: 80}, topo=1), task("B", roles={1: 80}, topo=2)]
    source = inputs(
        tasks,
        [engineer("ENG-1")],
        baseline_schedule={"A": ("in_quarter", 1, 1), "B": ("in_quarter", 1, 1)},
        done_in_sprint={1: frozenset({"A"})},
        last_reported_sprint=1,
    )

    plan = planner.build_plan(source, as_of_sprint=2, baseline_starts={"A": 1, "B": 1})

    say_do = {row.sprint_no: row for row in plan.kpis if row.kpi_code == "say_do_ratio"}
    assert say_do[1].value == Decimal("50.00")
    assert say_do[1].kind == "actual"
    assert say_do[1].details["planned_sp"] == "10" and say_do[1].details["done_sp"] == "5"
    assert say_do[2].kind == "forecast"  # спринт ещё не отчитан — прогноз

    # Процент выполнения квартала тоже раздваивается: прогноз и факт.
    kinds = {row.kind for row in plan.kpis if row.kpi_code == "pi_predictability"}
    assert kinds == {"forecast", "actual"}


def test_plan_is_deterministic() -> None:
    tasks = [
        task("A", roles={1: 40, 2: 40}, topo=1),
        task("B", roles={2: 40}, topo=2),
        task("C", roles={1: 40}, topo=3),
    ]
    engineers = [engineer("ENG-1", role_id=1), engineer("ENG-2", role_id=2)]
    source = inputs(tasks, engineers, team_sp={T1: 10})

    first = planner.build_plan(source)
    second = planner.build_plan(source)
    assert first.schedule == second.schedule
    assert first.assignments == second.assignments
    assert first.alerts == second.alerts


# ---------------------------------------------------------------------------
#  Ревью M2: зависимости, атомарность инициатив,
#  пересчёт, одна орбита на назначение, efficiency, кандидаты из вьюхи.
# ---------------------------------------------------------------------------
def test_dependency_finish_start_waits_for_the_end_of_the_blocker() -> None:
    """ADR-028: по умолчанию блокируемая стартует после КОНЦА блокирующей.

    A растянута на спринты 1..2 (120 ЧЧ при фонде 80), B зависит от A.
    При `finish_start` (умолчание) B встаёт только в 3-й, при явном `start_start` — во 2-й.
    """
    tasks = [task("A", roles={1: 120}, topo=1), task("B", roles={1: 40}, topo=2)]
    source = inputs(tasks, [engineer("ENG-1")], deps=(("A", "B", 1),))

    default = planner.build_plan(source)
    assert (default.schedule[0].start_sprint, default.schedule[0].end_sprint) == (1, 2)
    assert starts_of(default) == {"A": 1, "B": 3}
    assert default.params["dependency_mode"] == planner.DEFAULT_DEPENDENCY_MODE
    assert planner.DEFAULT_DEPENDENCY_MODE == planner.DEPENDENCY_MODE_FINISH_START

    start_start = planner.build_plan(source, dependency_mode=planner.DEPENDENCY_MODE_START_START)
    assert starts_of(start_start) == {"A": 1, "B": 2}
    assert start_start.params["dependency_mode"] == planner.DEPENDENCY_MODE_START_START


def test_default_mode_never_starts_a_dependent_before_its_blocker_ends() -> None:
    """Зависимая не завершается раньше предшественника при любых окнах (DA-09)."""
    tasks = [task("A", roles={1: 240}, topo=1), task("B", roles={2: 40}, topo=2)]
    source = inputs(tasks, [engineer("ENG-1"), engineer("ENG-2", role_id=2)], deps=(("A", "B", 1),))

    plan = planner.build_plan(source)
    rows = {row.task_id: row for row in plan.schedule}
    assert rows["B"].start_sprint > rows["A"].end_sprint


def test_unknown_modes_are_rejected() -> None:
    source = inputs([task("T-1")], [engineer("ENG-1")])
    for bad in ("fs", "", "start_finish"):
        with pytest.raises(ValueError):
            planner.build_plan(source, dependency_mode=bad)
        with pytest.raises(ValueError):
            planner.build_plan(source, initiative_mode=bad)


def test_replan_never_plans_into_closed_sprints() -> None:
    """ADR-014: при `as_of_sprint = 3` спринты 1..2 уже прожиты."""
    plan = planner.build_plan(inputs([task("T-1", earliest=1)], [engineer("ENG-1")]), as_of_sprint=3)

    assert starts_of(plan) == {"T-1": 3}
    assert plan.params["replan_floor"] == 3
    assert all(row.sprint_no >= 3 for row in plan.assignments)


def test_atomic_initiatives_defer_the_whole_initiative() -> None:
    """ADR-013: «всё или ничего» — пробная упаковка с откатом.

    Фонд — 80 ЧЧ за квартал, у P-1 две задачи по 80 ЧЧ, у P-2 одна.
    Жадный режим закрывает половину P-1 и теряет ресурс; атомарный откатывает
    P-1 и отдаёт освободившиеся часы P-2 — та закрывается целиком.
    """
    tasks = [
        task("A1", roles={1: 80}, topo=1, prodf="P-1"),
        task("A2", roles={1: 80}, topo=2, prodf="P-1"),
        task("B1", roles={1: 80}, topo=3, prodf="P-2"),
    ]
    source = inputs(tasks, [engineer("ENG-1")], sprint_count=1)

    greedy = planner.build_plan(source)
    assert starts_of(greedy) == {"A1": 1, "A2": None, "B1": None}
    assert greedy.params["initiative_mode"] == planner.INITIATIVE_MODE_GREEDY
    assert greedy.params["initiatives_complete"] == 0
    assert greedy.params["initiatives_partial"] == ["P-1"]

    atomic = planner.build_plan(source, initiative_mode=planner.INITIATIVE_MODE_ATOMIC)
    assert starts_of(atomic) == {"A1": None, "A2": None, "B1": 1}
    assert atomic.params["initiatives_complete"] == 1
    assert atomic.params["initiatives_partial"] == []
    # Пробное назначение A1 откатано: в фонде остались ровно 80 ЧЧ под B1.
    assert [(a.task_id, a.hours) for a in atomic.assignments] == [("B1", Decimal("80"))]


def test_one_engineer_can_use_two_orbits_on_one_task_in_one_sprint() -> None:
    """Парттаймер 0.5 + 0.5 отдаёт свои 40 и заёмные 40 ЧЧ за спринт."""
    part_timer = engineer("ENG-1", orbits=(T1, T2))
    plan = planner.build_plan(inputs([task("T-1", team=T1, roles={1: 80})], [part_timer]))

    assert [(a.sprint_no, a.hours, a.home_team_id) for a in plan.assignments] == [
        (1, Decimal("40"), T1),
        (1, Decimal("40"), T2),
    ]
    keys = [(a.task_id, a.sprint_no, a.engineer_id, a.role_id, a.home_team_id)
            for a in plan.assignments]
    assert len(keys) == len(set(keys)), "ключ контракта обязан быть уникальным"
    assert sum((a.hours for a in plan.assignments), Decimal(0)) == Decimal(80)
    assert plan.schedule[0].end_sprint == 1


def test_loan_comes_from_the_other_orbit_of_the_same_engineer() -> None:
    """Своё ядро выбирается первым, но заём берётся с ЧУЖОЙ орбиты того же человека."""
    part_timer = engineer("ENG-1", orbits=(T1, T2))
    tasks = [
        task("T-1", team=T2, roles={1: 40}, topo=1),
        task("T-2", team=T2, roles={1: 40}, topo=2),
    ]
    plan = planner.build_plan(inputs(tasks, [part_timer], team_sp={T1: 100, T2: 100}))

    assert [(a.task_id, a.sprint_no, a.home_team_id, a.serving_team_id) for a in plan.assignments] == [
        ("T-1", 1, T2, T2),
        ("T-2", 1, T1, T2),
    ]


def test_efficiency_multiplies_the_required_hours() -> None:
    """ADR-016: смету 40 ЧЧ при `efficiency = 1.25` закрывают 50 часов исполнителя."""
    plan = planner.build_plan(
        inputs(
            [task("T-1", roles={1: 40})],
            [engineer("ENG-1")],
            coverage={("ENG-1", 1): Decimal("1.25")},
        )
    )

    assert [(a.sprint_no, a.hours) for a in plan.assignments] == [(1, Decimal("50.00"))]
    assert [a.work_hours for a in plan.assignments] == [Decimal("40.0000")]
    assert plan.role_demands == (("T-1", 1, Decimal("40")),)
    assert plan.params["efficiency_note"] == planner.EFFICIENCY_NOTE


def test_candidates_come_from_the_coverage_view() -> None:
    """ADR-012: пару «инженер × роль» определяет вьюха, а не `engineers.role_id`."""
    plan = planner.build_plan(
        inputs(
            [task("T-1", roles={1: 40})],
            [engineer("ENG-1", role_id=7)],
            coverage={("ENG-1", 1): Decimal("1")},
        )
    )

    assert starts_of(plan) == {"T-1": 1}
    assert plan.assignments[0].role_id == 1
    assert plan.assignments[0].engineer_id == "ENG-1"


def test_objective_and_modes_are_recorded_in_params() -> None:
    """ADR-015: у прогона всегда есть объяснение, по каким правилам он построен."""
    plan = planner.build_plan(inputs([task("T-1")], [engineer("ENG-1")]))

    assert plan.params["objective"] == planner.OBJECTIVE
    assert plan.params["initiative_mode"] == planner.INITIATIVE_MODE_GREEDY
    assert plan.params["dependency_mode"] == planner.DEFAULT_DEPENDENCY_MODE
    assert plan.params["replan_floor"] == 1
    assert plan.params["initiatives_planned"] == 1
    assert plan.params["initiatives_complete"] == 1
    assert plan.params["initiatives_partial"] == []

def test_big_task_spreads_its_sp_over_several_sprints() -> None:
    """Задача крупнее ёмкости спринта растягивается, а не переносится навсегда.

    Пример из онбординга: DB-202 «алгоритм должен растянуть эту задачу минимум
    на 2 спринта». При прежней модели (все SP в спринт старта) такая задача не
    попадала в план НИКОГДА, при любых ресурсах (ADR-020).
    """
    plan = planner.build_plan(
        inputs([task("DB-202", sp=8, roles={1: 40})], [engineer("ENG-1")], team_sp={T1: 7})
    )

    row = plan.schedule[0]
    assert row.decision == "in_quarter"
    assert (row.start_sprint, row.end_sprint) == (1, 2)
    assert dict((sprint, sp) for _task, sprint, sp in plan.sp_shares) == {
        1: Decimal("7"),
        2: Decimal("1"),
    }
    assert "выполняются вместе" in row.reason_text


def test_completed_blocker_recalculates_live_start_from_fact_date() -> None:
    base = inputs([task("B", earliest=5)], [engineer("ENG-1")])
    early = replace(base, all_deps=(("A", "B", 1),),
                    done_task_dates={"A": (date(2026, 6, 2), date(2026, 6, 2))})
    late = replace(base, all_deps=(("A", "B", 1),),
                   done_task_dates={"A": (date(2026, 6, 16), date(2026, 6, 16))})

    assert starts_of(planner.build_plan(early, as_of_sprint=2, simulate_next_pi=False)) == {"B": 2}
    assert starts_of(planner.build_plan(late, as_of_sprint=3, simulate_next_pi=False)) == {"B": 3}
    future = planner.build_plan(late, as_of_sprint=2, simulate_next_pi=False)
    assert future.schedule[0].decision != "in_quarter"
    assert future.params["dependency_fact_issues"]


def test_task_waits_for_a_sprint_where_the_team_has_free_capacity() -> None:
    """Ёмкость спринта исчерпана — задача начинается позже (поведение сохранено)."""
    plan = planner.build_plan(
        inputs(
            [task("A", sp=5, topo=1), task("B", sp=5, topo=2)],
            [engineer("ENG-1"), engineer("ENG-2")],
            team_sp={T1: 5},
        )
    )

    assert starts_of(plan) == {"A": 1, "B": 2}


def test_repack_uses_capacity_freed_by_deferrals() -> None:
    """Проход 4: часы, освобождённые переносом, достаются следующей задаче.

    A перенесена (роли нет в штате) и тянет за собой B; освободившиеся 80 ЧЧ
    получает C, у которой приоритет ниже. Без повторной упаковки C осталась бы
    перенесённой с причиной «не хватило ресурсов», что было бы неправдой.
    """
    tasks = [
        task("A", rung=90, topo=1, roles={99: 40}),
        task("B", rung=90, topo=2, roles={1: 80}),
        task("C", rung=50, topo=3, roles={1: 80}),
    ]
    plan = planner.build_plan(
        inputs(tasks, [engineer("ENG-1")], deps=(("A", "B", 1),), sprint_count=1)
    )

    decisions = {row.task_id: row.decision for row in plan.schedule}
    assert decisions == {
        "A": "deferred_next_pi",
        "B": "deferred_next_pi",
        "C": "in_quarter",
    }


def test_atomic_repack_uses_capacity_released_by_blocked_initiative() -> None:
    """An initiative rejected after dependency propagation must release its hours for the next one."""
    tasks = [
        task("B", rung=90, topo=1, roles={1: 80}, prodf="P-1"),
        task("C", rung=50, topo=2, roles={1: 80}, prodf="P-2"),
        task("A", rung=10, topo=3, roles={99: 40}, prodf="P-3"),
    ]
    plan = planner.build_plan(
        inputs(tasks, [engineer("ENG-1")], deps=(("A", "B", 1),), sprint_count=1),
        initiative_mode=planner.INITIATIVE_MODE_ATOMIC, simulate_next_pi=False,
    )
    assert starts_of(plan) == {"A": None, "B": None, "C": 1}
    assert plan.params["repack"] is True


def test_small_order_search_exposes_a_better_complete_initiative() -> None:
    source = inputs([
        task("A", sp=5, rung=90, roles={1: 80}, prodf="P-A"),
        task("B", sp=10, rung=50, roles={1: 80}, prodf="P-B"),
    ], [engineer("ENG-1")], sprint_count=1)
    ordinary = planner.build_plan(source, simulate_next_pi=False)
    assert {row.task_id for row in ordinary.in_quarter} == {"A"}
    benchmark = plan_quality.reference_order_search(source, 0, {})
    assert benchmark["permutations"] == 2
    assert benchmark["best_by_mode"]["atomic"]["complete_initiative_sp"] == "10"
    assert benchmark["best_by_mode"]["greedy"]["order"][0] == "P-B"


def test_every_decision_carries_a_human_explanation() -> None:
    """ТЗ: объяснять причины включения, переноса и отмены."""
    tasks = [task("A", topo=1, roles={1: 40}), task("B", topo=2, roles={99: 40})]
    plan = planner.build_plan(inputs(tasks, [engineer("ENG-1")]))

    rows = {row.task_id: row for row in plan.schedule}
    assert all(row.reason_code and row.reason_text for row in rows.values())

    assert rows["A"].reason_code == planner.REASON_PLANNED
    assert "Включена" in rows["A"].reason_text
    assert rows["A"].reason_details["roles"] == {"Роль 1": ["ENG-1"]}

    assert rows["B"].reason_code == planner.REASON_ROLE_NOT_IN_STAFF
    assert "в штате нет роли «Роль 99»" in rows["B"].reason_text
    assert rows["B"].reason_details["missing_roles"] == [{"role": "Роль 99", "hours": "40"}]
    assert rows["B"].decision_reason == planner.DEFERRED_REASON  # совместимость контракта


def test_quarter_end_run_moves_the_rest_to_the_next_pi() -> None:
    """Факт за последний спринт: планировать больше некуда, но прогон валиден."""
    plan = planner.build_plan(
        inputs([task("A", roles={1: 40})], [engineer("ENG-1")], sprint_count=6),
        as_of_sprint=7,
    )

    row = plan.schedule[0]
    assert plan.status == "ok"  # не infeasible: квартал просто закончился
    assert plan.params["business_outcome"] == "nothing_scheduled"
    assert row.decision == "deferred_next_pi"
    assert row.reason_code == planner.REASON_PI_CLOSED
    assert plan.assignments == ()


def test_empty_backlog_is_a_successful_completed_run() -> None:
    plan = planner.build_plan(inputs([], [], all_tasks=(("A", "Done", Decimal(1), Decimal(0)),)))

    assert plan.status == "ok"
    assert plan.params["business_outcome"] == "completed"
    assert plan.schedule == ()


def test_all_deferred_is_published_as_the_current_result() -> None:
    plan = planner.build_plan(inputs([task("A", roles={99: 40})], [engineer("ENG-1")]))

    assert plan.status == "ok"
    assert plan.params["business_outcome"] == "nothing_scheduled"
    assert plan.schedule[0].decision == "deferred_next_pi"


def test_sp_and_hours_move_together_in_every_sprint() -> None:
    """ADR-029 (DA-07): спринт с долей SP содержит часы, спринт с часами — долю SP,
    доли пропорциональны выполненной работе, сумма равна SP задачи, ёмкость цела."""
    capacity = Decimal("3")
    plan = planner.build_plan(
        inputs([task("T", sp=8, roles={1: 120}), task("U", sp=2, roles={1: 20}, topo=2)],
               [engineer("ENG-1"), engineer("ENG-2")], team_sp={T1: capacity})
    )
    shares: dict[str, dict[int, Decimal]] = {}
    for task_id, sprint, sp in plan.sp_shares:
        shares.setdefault(task_id, {})[sprint] = sp
    for row in plan.in_quarter:
        hours: dict[int, Decimal] = {}
        for item in plan.assignments:
            if item.task_id == row.task_id:
                hours[item.sprint_no] = hours.get(item.sprint_no, Decimal(0)) + item.work_hours
        task_shares = shares.get(row.task_id, {})
        assert set(task_shares) <= set(hours), "SP без часов"
        assert set(hours) <= set(task_shares) or all(
            hours[n] * Decimal(8) / sum(hours.values()) < Decimal("0.01") for n in set(hours) - set(task_shares)
        ), "часы без SP"
        total_sp = Decimal(8) if row.task_id == "T" else Decimal(2)
        assert sum(task_shares.values()) == total_sp
        total_hours = sum(hours.values())
        for sprint, value in task_shares.items():
            assert abs(value - total_sp * hours[sprint] / total_hours) <= Decimal("0.02")
    for sprint in range(1, 7):
        used = sum((sp for _t, n, sp in plan.sp_shares if n == sprint), Decimal(0))
        assert used <= capacity


def test_sp_cap_limits_hours_per_sprint_so_a_big_task_stretches() -> None:
    """Часов у людей много, но ёмкость команды 2 SP за спринт: 8 SP — минимум 4 спринта."""
    plan = planner.build_plan(
        inputs([task("T", sp=8, roles={1: 40})], [engineer("ENG-1"), engineer("ENG-2")], team_sp={T1: Decimal("2")})
    )
    row = plan.schedule[0]
    assert row.decision == "in_quarter" and row.end_sprint - row.start_sprint + 1 >= 4
    by_sprint: dict[int, Decimal] = {}
    for item in plan.assignments:
        by_sprint[item.sprint_no] = by_sprint.get(item.sprint_no, Decimal(0)) + item.work_hours
    assert all(hours <= Decimal("10.01") for hours in by_sprint.values())  # 40 ч × 2/8 SP


# ---------------------------------------------------------------------------
#  DA-16: причина отказа строится из той же проверки, по которой принято решение
# ---------------------------------------------------------------------------
def _row(plan: planner.Plan, task_id: str) -> planner.ScheduleRow:
    return next(row for row in plan.schedule if row.task_id == task_id)


def test_sp_refusal_is_explained_with_the_numbers_that_decided_it() -> None:
    """Часов у людей много, ёмкость 2 SP/спринт: A (8 SP) берёт 8 из 12, B (8 SP) не помещается."""
    plan = planner.build_plan(
        inputs([task("A", sp=8, roles={1: 40}, rung=9, topo=1), task("B", sp=8, roles={1: 40}, rung=1, topo=2)],
               [engineer("ENG-1"), engineer("ENG-2")], team_sp={T1: 2}),
        simulate_next_pi=False,
    )
    assert _row(plan, "A").decision == "in_quarter"
    refusal = _row(plan, "B")
    assert refusal.reason_code == planner.REASON_TEAM_SP
    assert refusal.reason_details["free_sp"] == "4.00" and refusal.reason_details["need_sp"] == "8"
    rival = refusal.reason_details["competitors"][0]
    assert rival["task_id"] == "A" and rival["higher_priority"] is True and Decimal(rival["sp"]) == 8
    assert "выше в очереди" in refusal.reason_text and "свободно 4" in refusal.reason_text


def test_hours_refusal_names_unplaced_and_free_hours_and_the_rival() -> None:
    """Один инженер (480 ч за квартал): A занимает 400, B просит 200 — не хватает 120, свободно 80."""
    plan = planner.build_plan(
        inputs([task("A", sp=1, roles={1: 400}, rung=9, topo=1), task("B", sp=1, roles={1: 200}, rung=1, topo=2)],
               [engineer("ENG-1")], team_sp={T1: 100}),
        simulate_next_pi=False,
    )
    refusal = _row(plan, "B")
    assert refusal.reason_code == planner.REASON_ROLE_HOURS
    item = refusal.reason_details["shortages"][0]
    assert Decimal(item["need_hh"]) == 200 and Decimal(item["unplaced_hh"]) == 120 and Decimal(item["free_hh"]) == 80
    assert item["competitors"][0]["task_id"] == "A" and item["competitors"][0]["higher_priority"] is True
    assert "не удалось разместить 120" in refusal.reason_text and "свободно у людей роли 80" in refusal.reason_text


def test_lower_priority_rival_is_not_called_higher() -> None:
    """Если место заняла задача ниже в очереди, текст не врёт про «более приоритетную»."""
    plan = planner.build_plan(
        inputs([task("A", sp=1, roles={1: 400}, rung=1, topo=1), task("B", sp=1, roles={1: 200}, rung=9, topo=2)],
               [engineer("ENG-1")], team_sp={T1: 100}),
        simulate_next_pi=False,
    )
    # B стоит выше A и берёт 200; A (400 ч) не помещается: конкурент B — выше в очереди.
    refusal = _row(plan, "A")
    assert refusal.reason_code == planner.REASON_ROLE_HOURS
    assert all(c["higher_priority"] for c in refusal.reason_details["shortages"][0]["competitors"])


def test_dependency_chain_beyond_the_horizon_has_its_own_reason() -> None:
    """A занимает весь квартал, B зависит от неё: самый ранний старт B — спринт 7 при шести спринтах."""
    plan = planner.build_plan(
        inputs([task("A", sp=1, roles={1: 480}, rung=9, topo=1), task("B", sp=1, roles={2: 10}, rung=1, topo=2)],
               [engineer("ENG-1"), engineer("ENG-2", role_id=2)], deps=(("A", "B", 1),), team_sp={T1: 100}),
        simulate_next_pi=False,
    )
    assert _row(plan, "A").decision == "in_quarter"
    refusal = _row(plan, "B")
    assert refusal.reason_code == planner.REASON_GRAPH_HORIZON
    assert refusal.reason_details["earliest_start_sprint"] == 7
    assert refusal.reason_details["blocking"][0]["task_id"] == "A"
    assert "спринт 7" in refusal.reason_text and "шесть" not in refusal.reason_text


def test_atomic_reason_checks_hours_and_sp_together() -> None:
    """Задача X сама помещается, но Y инициативы — нет: X уходит с инициативой, и текст говорит про часы И SP."""
    plan = planner.build_plan(
        inputs([task("X", sp=1, roles={1: 40}, rung=5, topo=1, prodf="P"),
                task("Y", sp=50, roles={1: 40}, rung=5, topo=2, prodf="P")],
               [engineer("ENG-1")], team_sp={T1: 2}),
        initiative_mode=planner.INITIATIVE_MODE_ATOMIC, simulate_next_pi=False,
    )
    x = _row(plan, "X")
    assert x.decision != "in_quarter" and x.reason_code == planner.REASON_ATOMIC
    assert "по часам и ёмкости SP" in x.reason_text
