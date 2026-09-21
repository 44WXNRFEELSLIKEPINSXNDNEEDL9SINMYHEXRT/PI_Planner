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
from datetime import date, timedelta
from decimal import Decimal

from app import planner

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
) -> planner.Inputs:
    return planner.Inputs(
        pi_id="PI-TEST",
        sprint_count=sprint_count,
        fte_hours_per_sprint=fte,
        team_sp_per_sprint={team: Decimal(cap) for team, cap in (team_sp or {T1: 100}).items()},
        tasks=tuple(tasks),
        engineers=tuple(engineers),
        deps=tuple(deps),
        sprints={
            n: (
                date(2026, 6, 1) + timedelta(days=14 * (n - 1)),
                date(2026, 6, 14) + timedelta(days=14 * (n - 1)),
            )
            for n in range(1, sprint_count + 1)
        },
        all_tasks=tuple(all_tasks),
        bus_factor=tuple(bus_factor),
        estimate_conflicts=conflicts,
        estimate_conflict_warnings=conflicts,
        active_substitutions=0,
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


def test_zero_remaining_task_gets_symbolic_assignment() -> None:
    """MOB-7011 в живых данных: смета выбрана полностью, остаток 0 ЧЧ."""
    plan = planner.build_plan(inputs([task("T-0", roles={1: 0})], [engineer("ENG-1")]))

    row = plan.schedule[0]
    assert (row.decision, row.start_sprint, row.end_sprint) == ("in_quarter", 1, 1)
    assert len(plan.assignments) == 1
    assert plan.assignments[0].hours == planner.SYMBOLIC_HOURS == Decimal("0.01")


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
    # Инициатива выполнена, только если ВСЕ её задачи в квартале: PRODF-1 да, PRODF-2 нет.
    assert by_code["pi_predictability"].value == Decimal("50.00")
    assert by_code["pi_predictability"].target_min == Decimal("80")
    assert by_code["pi_predictability"].details["completed_initiatives"] == ["PRODF-1"]
    assert by_code["bus_factor"].value == Decimal("1")
    assert by_code["bus_factor"].details["critical"] == ["Роль 1"]

    say_do = [row for row in plan.kpis if row.kpi_code == "say_do_ratio"]
    assert [row.sprint_no for row in say_do] == [1, 2, 3, 4, 5, 6]
    assert all(row.value == Decimal("100.00") for row in say_do)
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

    shifted = planner.build_plan(plain, as_of_sprint=1, baseline_starts={"B": 1})
    yellow = [alert for alert in shifted.alerts if alert.level == "yellow"]
    assert [alert.entity_id for alert in yellow] == ["B"]
    assert yellow[0].alert_type == "cascade_shift"
    assert yellow[0].payload["baseline_start_sprint"] == 1
    assert yellow[0].payload["new_start_sprint"] == 2
    assert yellow[0].payload["dependents"] == ["C"]

    # Первый прогон: сравнивать не с чем — жёлтых алертов нет.
    first = planner.build_plan(plain)
    assert not [alert for alert in first.alerts if alert.level == "yellow"]


def test_say_do_ratio_drops_for_the_sprint_that_was_promised() -> None:
    """Обещали B в спринт 1, а он уехал в 2 — провален именно спринт 1."""
    tasks = [task("A", roles={1: 80}, topo=1), task("B", roles={1: 80}, topo=2)]
    source = inputs(tasks, [engineer("ENG-1")])

    plan = planner.build_plan(source, as_of_sprint=1, baseline_starts={"A": 1, "B": 1})

    say_do = {row.sprint_no: row.value for row in plan.kpis if row.kpi_code == "say_do_ratio"}
    assert say_do[1] == Decimal("50.00")  # обещали 10 SP, стартовало 5
    assert say_do[2] == Decimal("100.00")  # на спринт 2 ничего не обещали
    assert say_do[3] == Decimal("100.00")


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