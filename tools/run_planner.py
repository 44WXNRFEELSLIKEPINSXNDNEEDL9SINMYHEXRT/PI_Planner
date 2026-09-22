"""Запуск планировщика: один прогон = один вызов.

    uv run python tools/run_planner.py                    # базовый план (as_of_sprint = 0)
    uv run python tools/run_planner.py --as-of-sprint 3   # пересчёт на начало 3-го спринта
    uv run python tools/run_planner.py --dry-run          # посчитать, но не писать

    # варианты правил (ADR-013); по умолчанию — как в приёмке M2:
    uv run python tools/run_planner.py --dry-run --dependency-mode finish_start
    uv run python tools/run_planner.py --dry-run --initiative-mode atomic

Пишет контракт целиком одной транзакцией (`app.planner.write_plan`). Приёмка
результата — одним запросом. Блокируют строки `severity = 'error'`; строки
`severity = 'warning'` требуют показа в UI, но план не отменяют
(docs/PLANNER_SPEC.md, раздел 7):

    SELECT * FROM v_plan_violations WHERE run_id = <run_id> AND severity = 'error';

**Заморозка.** После первого прогона базу не пересевать: `plan_runs` хранит
историю пересчётов, а `build/seed.sql` её сносит (docs/RUNBOOK.md, разделы 3 и 6).
"""
from __future__ import annotations

import argparse
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
        default=planner.DEPENDENCY_MODE_START_START,
        help=(
            "start_start (по умолчанию) — старт после СТАРТА блокирующей; "
            "finish_start — старт после КОНЦА блокирующей (ADR-013)"
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
    args = parser.parse_args(argv)

    print(f"dsn: {db.dsn()}")
    if args.if_empty:
        baselines = int(
            db.scalar(
                "SELECT COUNT(*) FROM plan_runs WHERE as_of_sprint = 0 AND status = 'ok'"
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
    plan = planner.build_plan(
        inputs,
        as_of_sprint=args.as_of_sprint,
        baseline_starts=baseline_starts,
        dependency_mode=args.dependency_mode,
        initiative_mode=args.initiative_mode,
    )
    build_plan_seconds = time.perf_counter() - phase_started
    plan.params["observability"] = {
        "load_inputs_seconds": round(load_inputs_seconds, 6),
        "load_baseline_seconds": round(load_baseline_seconds, 6),
        "build_plan_seconds": round(build_plan_seconds, 6),
        "before_write_seconds": round(time.perf_counter() - total_started, 6),
    }

    print(f"статус: {plan.status}")
    print(f"итог: {plan.note}")
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

    run_id = planner.write_plan(plan)
    print(
        f"записано: run_id = {run_id} "
        f"(as_of_sprint = {plan.as_of_sprint}, алгоритм {planner.ALGORITHM})"
    )
    print(
        f"приёмка: SELECT * FROM v_plan_violations WHERE run_id = {run_id} "
        f"AND severity = 'error';   -- пусто = нет ошибок"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
