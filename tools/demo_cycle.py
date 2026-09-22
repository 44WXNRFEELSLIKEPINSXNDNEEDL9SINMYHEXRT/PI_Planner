#!/usr/bin/env python3
"""Демо-цикл квартала: факт спринтов 1–3 и пересчёт после каждого.

    uv run python tools/demo_cycle.py            # сгенерировать demo/*.csv и прогнать цикл
    uv run python tools/demo_cycle.py --files-only   # только файлы, базу не трогать

Зачем. ТЗ требует показать пересчёт по фактическим результатам, но в датасете
факта нет — его загружает пользователь. Чтобы жюри могло проверить пересчёт, а
не верить на слово, факт генерируется ИЗ ДЕЙСТВУЮЩЕГО ПЛАНА: часы берутся из
назначений прогона, а не выдумываются. Сценарий отклонений минимальный и
осмысленный: по одной задаче в спринте не закрывается в срок, остальные идут по
плану. Файлы кладутся в `demo/` — их же можно загрузить в интерфейсе.

Планировщик детерминирован, поэтому у жюри получится ровно тот же результат.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, ingest  # noqa: E402

DEMO_DIR = ROOT / "demo"

# Кто не закрывается в срок. MP-102 выбран намеренно: у него есть зависимая
# MP-103, поэтому виден каскадный сдвиг (жёлтый алерт), а не просто задержка.
SCENARIO: dict[int, dict[str, list[str]]] = {
    1: {"slip": ["MP-102"]},
    2: {"slip": ["DB-203"]},
    3: {"slip": []},
}
SLIP_RATIO = Decimal("0.6")  # сколько часов спринта успели по «просевшей» задаче

PLAN_SQL = """
SELECT s.task_id, s.start_sprint, s.end_sprint
FROM plan_task_schedule s
WHERE s.run_id = %s AND s.decision = 'in_quarter'
ORDER BY s.task_id
"""
HOURS_SQL = """
SELECT a.task_id, r.canonical_name AS role_name, SUM(a.hours) AS hours
FROM plan_assignments a JOIN roles r ON r.role_id = a.role_id
WHERE a.run_id = %s AND a.sprint_no = %s
GROUP BY a.task_id, r.canonical_name
"""


def current_run() -> int:
    run_id = db.scalar("SELECT MAX(run_id) FROM plan_runs WHERE status = 'ok'")
    if not run_id:
        raise SystemExit("[демо] нет ни одного успешного прогона: сначала загрузите датасет")
    return int(run_id)


def build_csv(sprint_no: int, run_id: int) -> tuple[str, bytes]:
    """CSV факта спринта по плану, действующему в этом спринте."""
    _name, template = ingest.actuals_template(sprint_no)
    header = next(csv.reader(io.StringIO(template.decode("utf-8-sig"))))
    roles = header[5:]

    plan = {row["task_id"]: row for row in db.query_dicts(PLAN_SQL, (run_id,))}
    hours: dict[tuple[str, str], Decimal] = {
        (row["task_id"], row["role_name"]): Decimal(row["hours"])
        for row in db.query_dicts(HOURS_SQL, (run_id, sprint_no))
    }
    dates = db.query_one(
        "SELECT start_date, end_date FROM sprints WHERE sprint_no = %s ORDER BY pi_id LIMIT 1",
        (sprint_no,),
    )
    slip = set(SCENARIO.get(sprint_no, {}).get("slip", []))

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    rows = 0
    for task_id, row in sorted(plan.items()):
        if not (row["start_sprint"] <= sprint_no <= row["end_sprint"]):
            continue
        finishing = row["end_sprint"] == sprint_no
        slipped = task_id in slip
        status = "Done" if finishing and not slipped else "InProgress"
        share = SLIP_RATIO if slipped else Decimal("1")
        spent = [
            str((hours.get((task_id, role), Decimal("0")) * share).quantize(Decimal("0.01")))
            if hours.get((task_id, role))
            else ""
            for role in roles
        ]
        comment = (
            "не успели закрыть в спринте — остаток переносится"
            if slipped
            else ("закрыта по плану" if finishing else "в работе по плану")
        )
        writer.writerow(
            [
                task_id,
                status,
                dates["start_date"] if row["start_sprint"] == sprint_no else "",
                dates["end_date"] if status == "Done" else "",
                comment,
                *spent,
            ]
        )
        rows += 1
    if not rows:
        raise SystemExit(f"[демо] в спринте {sprint_no} по плану нет активных задач")
    return f"actuals_sprint_{sprint_no}.csv", ("﻿" + out.getvalue()).encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Демо-цикл: факт спринтов и пересчёт.")
    parser.add_argument("--sprints", type=int, default=3, help="сколько спринтов отыграть (по умолчанию 3)")
    parser.add_argument("--files-only", action="store_true", help="только сгенерировать файлы")
    args = parser.parse_args(argv)

    DEMO_DIR.mkdir(exist_ok=True)
    for sprint_no in range(1, args.sprints + 1):
        name, body = build_csv(sprint_no, current_run())
        (DEMO_DIR / name).write_bytes(body)
        print(f"✓ demo/{name} ({len(body.splitlines()) - 1} задач)")
        if args.files_only:
            continue
        result = ingest.load_actuals(body, name, sprint_no)
        plan = result["plan"]
        print(
            f"  факт спринта {sprint_no}: выполнено {result['summary']['done']}, "
            f"в работе {result['summary']['in_progress']}, часов {result['summary']['hours']}"
        )
        print(
            f"  пересчёт → прогон {plan['run_id']} (as_of {plan['as_of_sprint']}): "
            f"в квартале {plan['in_quarter']}, вне {plan['not_in_quarter']}, "
            f"алертов {plan['alerts']}, ошибок приёмки {plan['violations_error']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
