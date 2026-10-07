"""Запуск планировщика: один прогон = один вызов.

    uv run python tools/run_planner.py                    # базовый план (as_of_sprint = 0)
    uv run python tools/run_planner.py --as-of-sprint 3   # пересчёт на начало 3-го спринта
    uv run python tools/run_planner.py --dry-run          # посчитать, но не писать

    # варианты правил (ADR-013); по умолчанию — как в приёмке M2:
    uv run python tools/run_planner.py --dry-run --dependency-mode start_start
    uv run python tools/run_planner.py --dry-run --initiative-mode atomic

Пишет контракт целиком одной транзакцией (`app.planner.write_plan`). Приёмка
результата — одним запросом. Блокируют строки `severity = 'error'`; строки
`severity = 'warning'` требуют показа в UI, но план не отменяют
(docs/PLANNER_SPEC.md, раздел 7):

    SELECT * FROM v_plan_violations WHERE run_id = <run_id> AND severity = 'error';

**Заморозка.** После первого прогона базу не пересевать: `plan_runs` хранит
историю пересчётов, а `build/seed.sql` её сносит (docs/RUNBOOK.md, разделы 3 и 7).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, planner  # noqa: E402  (путь добавляем выше — иначе импорт не найдётся)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/run_planner.py",
        description="Прогон планировщика PI-Planner: читает витрины, пишет контракт.",
    )
    parser.add_argument(
        "--as-of-sprint",
        type=int,
        default=0,
        help="0 — базовый план Недели 0 (пишет plan_baseline), 1..12 — пересчёт (по умолчанию 0)",
    )
    parser.add_argument(
        "--algorithm",
        choices=(planner.ALGORITHM, planner.ALGORITHM_LOCAL_SEARCH),
        default=os.environ.get("PLANNER_ALGORITHM", planner.ALGORITHM),
        help="алгоритм планирования; lns-cpsat экспериментальный",
    )
    parser.add_argument(
        "--search-time-seconds",
        type=float,
        default=os.environ.get("PLANNER_SEARCH_TIME_SECONDS", "10"),
        help="общий лимит CP-SAT поиска в секундах (для lns-cpsat)",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=os.environ.get("PLANNER_RANDOM_SEED", "0"),
        help="seed решателя CP-SAT (для lns-cpsat)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="посчитать план и показать сводку, но в базу не писать",
    )
    parser.add_argument(
        "--if-empty",
        action="store_true",
        help=(
            "ничего не делать, если уже есть успешный базовый прогон; "
            "используется при первом запуске контейнерного стека"
        ),
    )
    parser.add_argument(
        "--dependency-mode",
        choices=planner.DEPENDENCY_MODES,
        default=planner.DEFAULT_DEPENDENCY_MODE,
        help=(
            "finish_start (по умолчанию) — старт после КОНЦА блокирующей; "
            "start_start — старт после СТАРТА блокирующей (ADR-013, ADR-028)"
        ),
    )
    parser.add_argument(
        "--initiative-mode",
        choices=planner.INITIATIVE_MODES,
        default=planner.INITIATIVE_MODE_GREEDY,
        help=(
            "greedy (по умолчанию) — задача решается отдельно, частичная "
            "инициатива допустима; atomic — пробная упаковка инициативы целиком "
            "с откатом (ADR-013)"
        ),
    )
    parser.add_argument(
        "--priority-strategy",
        choices=planner.PRIORITY_STRATEGIES,
        default=planner.DEFAULT_PRIORITY_STRATEGY,
        help=(
            "max (по умолчанию) — приоритет инициативы = MAX(rung) её задач; task — по rung самой "
            "задачи; weighted — средневзвешенный по SP; completion_first — при равном приоритете "
            "раньше инициатива, которую дешевле завершить (ADR-032)"
        ),
    )
    args = parser.parse_args(argv)

    # CLI и HTTP-загрузки используют один и тот же DB-lock и снимок входа.
    # Проверка --if-empty тоже под блокировкой, иначе два писателя могут
    # одновременно решить, что базового прогона ещё нет.
    with db.atomic_transaction():
        return _run_locked(args)


def _run_locked(args: argparse.Namespace) -> int:

    print(f"dsn: {db.dsn()}")
    if args.if_empty:
        baselines = int(
            db.scalar(
                "SELECT COUNT(*) FROM plan_runs WHERE as_of_sprint = 0 AND status IN ('ok', 'infeasible')"
            )
            or 0
        )
        if baselines > 0:
            print(f"пропуск: успешный базовый прогон уже существует ({baselines})")
            return 0

    total_started = time.perf_counter()
    phase_started = time.perf_counter()
    inputs = planner.load_inputs()
    load_inputs_seconds = time.perf_counter() - phase_started
    pi_start = min(pair[0] for pair in inputs.sprints.values())
    pi_end = max(pair[1] for pair in inputs.sprints.values())
    print(
        f"PI {inputs.pi_id}: {inputs.sprint_count} спринтов × {inputs.fte_hours_per_sprint} ЧЧ; "
        f"живых задач {len(inputs.tasks)}, инженеров {len(inputs.engineers)}, "
        f"зависимостей {len(inputs.deps)}"
    )
    print(
        f"календарь: {pi_start}..{pi_end} ({inputs.pi_days} дней); "
        f"фонд ставки за PI {inputs.fund_factor} × {inputs.fte_hours_per_sprint} = "
        f"{inputs.fund_hours_per_fte} ЧЧ "
        f"(короткие спринты: {inputs.short_sprints_note()})"
    )
    print(
        f"строгий режим: активных правил замещения {inputs.active_substitutions}; "
        f"расхождений источников часов {inputs.estimate_conflicts} "
        f"(авторитетен столбец матрицы сметы, ADR-002)"
    )

    phase_started = time.perf_counter()
    baseline_starts = planner.load_baseline_starts() if args.as_of_sprint > 0 else {}
    load_baseline_seconds = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    options = {
        "as_of_sprint": args.as_of_sprint,
        "dependency_mode": args.dependency_mode,
        "priority_strategy": args.priority_strategy,
        "initiative_mode": args.initiative_mode,
        "simulate_next_pi": args.algorithm != planner.ALGORITHM_LOCAL_SEARCH,
        "algorithm": args.algorithm,
        "max_time_seconds": args.search_time_seconds,
        "random_seed": args.random_seed,
    }
    plan = planner.build_plan(inputs, baseline_starts=baseline_starts, **options)
    build_plan_seconds = time.perf_counter() - phase_started
    plan.params["observability"] = {
        "load_inputs_seconds": round(load_inputs_seconds, 6),
        "load_baseline_seconds": round(load_baseline_seconds, 6),
        "build_plan_seconds": round(build_plan_seconds, 6),
        "before_write_seconds": round(time.perf_counter() - total_started, 6),
    }

    print(f"статус: {plan.status}")
    print(f"итог: {plan.note}")
    print(f"алгоритм: {plan.params['algorithm']}")
    optimization = plan.params.get("optimization")
    if optimization:
        print(
            f"поиск: {optimization.get('search_method', 'not_run')}, "
            f"статус решателя {optimization.get('solver_status', 'unknown')}, "
            f"улучшений LNS {optimization.get('neighborhoods_improved', 0)}"
        )
    print(
        f"режимы: зависимости {plan.params['dependency_mode']}, инициативы "
        f"{plan.params['initiative_mode']}, нижняя граница старта {plan.params['replan_floor']}"
    )
    print(
        f"инициативы: целиком {plan.params['initiatives_complete']} из "
        f"{plan.params['initiatives_planned']}, частично "
        f"{len(plan.params['initiatives_partial'])}"
    )
    print(
        f"строк контракта: расписание {len(plan.schedule)}, назначения {len(plan.assignments)}, "
        f"состояния {len(plan.states)}, алерты {len(plan.alerts)}, KPI {len(plan.kpis)}, "
        f"базовая линия {len(plan.baseline)}"
    )
    for level in ("red", "orange", "yellow"):
        rows = [row for row in plan.alerts if row.level == level]
        if rows:
            print(f"  {level}: {len(rows)}")
    for row in plan.kpis:
        if row.kpi_code != "say_do_ratio":
            print(f"  KPI {row.kpi_code} = {row.value} (норма {row.target_min}..{row.target_max})")

    if args.dry_run:
        print("--dry-run: в базу ничего не писали")
        return 0

    try:
        run_id = planner.write_plan(plan, inputs=inputs, baseline_starts=baseline_starts, options=options)
    except planner.PlanValidationError as exc:
        print(str(exc), file=sys.stderr)
        for row in exc.violations:
            print(f"  {row['check_code']}: {row['entity']} — {row['detail']}", file=sys.stderr)
        return 1
    print(
        f"записано: run_id = {run_id} "
        f"(as_of_sprint = {plan.as_of_sprint}, алгоритм {plan.params['algorithm']})"
    )
    print(
        f"приёмка: SELECT * FROM v_plan_violations WHERE run_id = {run_id} "
        f"AND severity = 'error';   -- пусто = нет ошибок"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
