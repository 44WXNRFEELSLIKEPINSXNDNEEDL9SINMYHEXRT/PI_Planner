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

import pytest

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

    # 526 ЧЧ не помещаются ни в этот квартал, ни в следующий с тем же штатом,
    # поэтому планировщик не переносит задачу, а рекомендует пересогласовать
    # её объём (ADR-022): перенос означал бы «в следующий раз получится».
    too_much = planner.build_plan(
        inputs(
            [task("T-1", roles={1: 526})],
            [engineer("ENG-1")],
            sprint_count=7,
            sprint_lengths={7: 8},
        )
    )
    assert too_much.schedule[0].decision == "cancelled"
    assert too_much.schedule[0].reason_code == planner.REASON_NOT_FEASIBLE
    assert "ни в следующий" in too_much.schedule[0].reason_text
    assert too_much.assignments == ()


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
    assert all(row.value == Decimal("100.00") for row in say_do)
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
#  Ревью M2 (docs/REVIEW_RESPONSE.md): зависимости, атомарность инициатив,
#  пересчёт, одна орбита на назначение, efficiency, кандидаты из вьюхи.
# ---------------------------------------------------------------------------
def test_dependency_finish_start_waits_for_the_end_of_the_blocker() -> None:
    """ADR-013: `finish_start` — старт блокируемой после КОНЦА блокирующей.

    A растянута на спринты 1..2 (120 ЧЧ при фонде 80), B зависит от A.
    При `start_start` B влезает в спринт 2, при `finish_start` — только в 3-й.
    """
    tasks = [task("A", roles={1: 120}, topo=1), task("B", roles={1: 40}, topo=2)]
    source = inputs(tasks, [engineer("ENG-1")], deps=(("A", "B", 1),))

    start_start = planner.build_plan(source)
    assert (start_start.schedule[0].start_sprint, start_start.schedule[0].end_sprint) == (1, 2)
    assert starts_of(start_start) == {"A": 1, "B": 2}
    assert start_start.params["dependency_mode"] == planner.DEPENDENCY_MODE_START_START

    finish_start = planner.build_plan(source, dependency_mode=planner.DEPENDENCY_MODE_FINISH_START)
    assert starts_of(finish_start) == {"A": 1, "B": 3}
    assert finish_start.params["dependency_mode"] == planner.DEPENDENCY_MODE_FINISH_START


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


def test_one_assignment_takes_hours_from_a_single_orbit() -> None:
    """ADR-015: одна строка `plan_assignments` = одна орбита.

    Парттаймер 0.5 + 0.5: бюджет орбиты — 40 ЧЧ. Задача на 80 ЧЧ получит
    40 со своей орбиты в спринте 1 и 40 в спринте 2 — но не одной строкой.
    """
    part_timer = engineer("ENG-1", orbits=(T1, T2))
    plan = planner.build_plan(inputs([task("T-1", team=T1, roles={1: 80})], [part_timer]))

    assert [(a.sprint_no, a.hours, a.home_team_id) for a in plan.assignments] == [
        (1, Decimal("40"), T1),
        (2, Decimal("40"), T1),
    ]
    keys = [(a.task_id, a.sprint_no, a.engineer_id, a.role_id) for a in plan.assignments]
    assert len(keys) == len(set(keys)), "ключ контракта обязан быть уникальным"


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
    assert plan.params["dependency_mode"] == planner.DEPENDENCY_MODE_START_START
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
    assert "растянуты" in row.reason_text


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
    assert row.decision == "deferred_next_pi"
    assert row.reason_code == planner.REASON_PI_CLOSED
    assert plan.assignments == ()
