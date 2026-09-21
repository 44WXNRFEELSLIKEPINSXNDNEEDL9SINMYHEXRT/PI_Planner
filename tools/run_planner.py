"""Запуск планировщика: один прогон = один вызов.

    uv run python tools/run_planner.py                    # базовый план (as_of_sprint = 0)
    uv run python tools/run_planner.py --as-of-sprint 3   # пересчёт на начало 3-го спринта
    uv run python tools/run_planner.py --dry-run          # посчитать, но не писать

Пишет контракт целиком одной транзакцией (`app.planner.write_plan`). Приёмка
результата — одним запросом, пустой ответ означает корректный план:

    SELECT * FROM v_plan_violations WHERE run_id = <run_id>;

**Заморозка.** После первого прогона базу не пересевать: `plan_runs` хранит
историю пересчётов, а `build/seed.sql` её сносит (docs/RUNBOOK.md, разделы 3 и 6).
"""
from __future__ import annotations

import argparse
import sys
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
    args = parser.parse_args(argv)

    print(f"dsn: {db.dsn()}")
    inputs = planner.load_inputs()
    print(
        f"PI {inputs.pi_id}: {inputs.sprint_count} спринтов × {inputs.fte_hours_per_sprint} ЧЧ; "
        f"живых задач {len(inputs.tasks)}, инженеров {len(inputs.engineers)}, "
        f"зависимостей {len(inputs.deps)}"
    )
    print(
        f"строгий режим: активных правил замещения {inputs.active_substitutions}; "
        f"расхождений источников часов {inputs.estimate_conflicts} "
        f"(авторитетен столбец матрицы сметы, ADR-002)"
    )

    baseline_starts = planner.load_baseline_starts() if args.as_of_sprint > 0 else {}
    plan = planner.build_plan(
        inputs, as_of_sprint=args.as_of_sprint, baseline_starts=baseline_starts
    )

    print(f"статус: {plan.status}")
    print(f"итог: {plan.note}")
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
    print(f"приёмка: SELECT * FROM v_plan_violations WHERE run_id = {run_id};   -- пусто = план корректен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
