"""Загрузки через сервис: датасет и факт спринтов (ТЗ, ADR-021).

ТЗ: «Пользователь загружает предоставленный датасет и получает план…
Раз в две недели пользователь загружает фактические результаты очередного
спринта. После обновления данных сервис пересчитывает оставшуюся часть плана».

Три операции, каждая — под одной блокировкой записи (сервер многопоточный,
а две параллельные загрузки перепутали бы состояние):

* `load_dataset`   — xlsx → ETL в памяти → seed в базу → базовый план (run as_of 0);
* `actuals_template` — CSV-шаблон факта на спринт: живые задачи и колонки ролей;
* `load_actuals`   — CSV/XLSX факта спринта → проверка → журнал загрузок →
  `apply_actuals()` пересобирает состояние → пересчёт с `as_of_sprint = N + 1`.

Формат факта (шаблон отдаёт `GET /api/actuals/template?sprint=N`):

    task_id, status, actual_start, actual_end, comment, <роль 1>, <роль 2>, …

`status` — ToDo | InProgress | Done (русские синонимы тоже принимаются);
колонки ролей — часы, потраченные ЗА ЭТОТ спринт. Задачи, которых нет в
файле, остаются в прежнем состоянии.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app import db, planner

ROOT = Path(__file__).resolve().parent.parent
SUBSTITUTIONS_SQL = ROOT / "db" / "03_substitutions.sql"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Одна запись за раз: загрузка датасета и факта меняют состояние целиком.
WRITE_LOCK = threading.Lock()

STATUS_ALIASES = {
    "done": "Done", "выполнена": "Done", "выполнено": "Done", "готово": "Done",
    "закрыта": "Done", "сделано": "Done",
    "inprogress": "InProgress", "in progress": "InProgress", "в работе": "InProgress",
    "в процессе": "InProgress",
    "todo": "ToDo", "to do": "ToDo", "не начата": "ToDo", "ожидает": "ToDo",
}
FIXED_COLUMNS = {
    "task_id": "task_id", "задача": "task_id", "id задачи": "task_id",
    "status": "status", "статус": "status",
    "actual_start": "actual_start", "факт начала": "actual_start", "дата начала": "actual_start",
    "actual_end": "actual_end", "факт окончания": "actual_end", "дата окончания": "actual_end",
    "comment": "comment", "комментарий": "comment",
}


class UploadError(ValueError):
    """Файл не принят: 400 с перечнем проблем, база не тронута."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems or []

    def payload(self) -> dict[str, Any]:
        return {"error": "bad_upload", "message": self.message, "problems": self.problems[:50]}


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip()


def _safe_name(filename: str | None, default: str) -> str:
    name = Path(filename or default).name
    return re.sub(r"[^\w.\-() ]+", "_", name) or default


def _strip_transaction(sql: str) -> str:
    """seed.sql и 03_substitutions.sql сами открывают BEGIN/COMMIT, а здесь
    они выполняются внутри одной транзакции psycopg — убираем обёртку."""
    return "\n".join(
        line for line in sql.splitlines() if line.strip().upper() not in ("BEGIN;", "COMMIT;")
    )


# ---------------------------------------------------------------------------
#  прогон планировщика
# ---------------------------------------------------------------------------
def run_plan(as_of_sprint: int) -> dict[str, Any]:
    """Один прогон: чтение → чистая функция → запись. Возвращает краткую сводку."""
    inputs = planner.load_inputs()
    baseline_starts = planner.load_baseline_starts() if as_of_sprint > 0 else {}
    plan = planner.build_plan(inputs, as_of_sprint=as_of_sprint, baseline_starts=baseline_starts)
    run_id = planner.write_plan(plan)
    errors = db.scalar(
        "SELECT COUNT(*) FROM v_plan_violations WHERE run_id = %s AND severity = 'error'", (run_id,)
    )
    return {
        "run_id": run_id,
        "as_of_sprint": as_of_sprint,
        "status": plan.status,
        "note": plan.note,
        "in_quarter": len(plan.in_quarter),
        "not_in_quarter": len(plan.deferred),
        "alerts": len(plan.alerts),
        "violations_error": int(errors or 0),
    }


def _has_baseline() -> bool:
    return bool(
        db.scalar("SELECT COUNT(*) FROM plan_runs WHERE as_of_sprint = 0 AND status = 'ok'")
    )


# ---------------------------------------------------------------------------
#  датасет
# ---------------------------------------------------------------------------
def load_dataset(data: bytes, filename: str | None) -> dict[str, Any]:
    """xlsx датасета → база с нуля → базовый план. Старые прогоны и факт стираются."""
    if not data:
        raise UploadError("пустой файл")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    name = _safe_name(filename, "dataset.xlsx")
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise UploadError("датасет принимается в формате .xlsx — как выданный организаторами")

    from etl import load as etl_load  # тяжёлый импорт (openpyxl) — только когда нужен

    with WRITE_LOCK, tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        try:
            seed_sql, counts, dq = etl_load.build_seed_sql(path)
        except SystemExit as exc:  # ETL сообщает о битой структуре листа так
            raise UploadError("структура листа не распознана", [str(exc).strip()]) from None
        except Exception as exc:  # noqa: BLE001 — не xlsx, битый zip и т.п.
            raise UploadError("файл не прочитан как датасет", [f"{type(exc).__name__}: {exc}"]) from None

        with db.transaction() as cur:
            cur.execute(_strip_transaction(seed_sql))
            cur.execute(_strip_transaction(SUBSTITUTIONS_SQL.read_text(encoding="utf-8")))

        plan = run_plan(0)
    return {
        "dataset": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": counts,
        "data_quality": dq,
        "plan": plan,
    }


# ---------------------------------------------------------------------------
#  шаблон факта
# ---------------------------------------------------------------------------
TEMPLATE_ROLES_SQL = """
SELECT DISTINCT r.role_id, r.canonical_name
FROM task_role_estimates e JOIN roles r ON r.role_id = e.role_id
ORDER BY r.role_id
"""
TEMPLATE_TASKS_SQL = """
SELECT t.task_id, t.status, t.actual_start, t.summary
FROM tasks t WHERE t.status <> 'Done'
ORDER BY t.task_id
"""


def _pi() -> dict[str, Any]:
    pi = db.query_one("SELECT pi_id, sprint_count FROM pi_periods ORDER BY pi_id LIMIT 1")
    if not pi:
        raise UploadError("в базе нет датасета: сначала загрузите его")
    return pi


def _last_sprint(pi_id: str) -> int:
    return int(
        db.scalar("SELECT COALESCE(MAX(sprint_no), 0) FROM actual_uploads WHERE pi_id = %s", (pi_id,))
        or 0
    )


def actuals_template(sprint_no: int | None = None) -> tuple[str, bytes]:
    """CSV (UTF-8 с BOM — Excel открывает без кракозябр): живые задачи и роли."""
    pi = _pi()
    sprint = sprint_no or min(_last_sprint(pi["pi_id"]) + 1, int(pi["sprint_count"]))
    roles = db.query_dicts(TEMPLATE_ROLES_SQL)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["task_id", "status", "actual_start", "actual_end", "comment"]
        + [row["canonical_name"] for row in roles]
    )
    for row in db.query_dicts(TEMPLATE_TASKS_SQL):
        writer.writerow(
            [row["task_id"], row["status"], row["actual_start"] or "", "", ""] + [""] * len(roles)
        )
    return f"actuals_sprint_{sprint}.csv", ("﻿" + out.getvalue()).encode("utf-8")


# ---------------------------------------------------------------------------
#  факт спринта
# ---------------------------------------------------------------------------
@dataclass
class ParsedRow:
    task_id: str
    status: str
    actual_start: date | None
    actual_end: date | None
    comment: str | None
    hours: dict[int, Decimal] = field(default_factory=dict)


def _read_table(data: bytes, name: str) -> list[list[Any]]:
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        except Exception as exc:  # noqa: BLE001
            raise UploadError("файл не прочитан как xlsx", [str(exc)]) from None
        return [list(row) for row in wb.worksheets[0].iter_rows(values_only=True)]
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text.split("\n", 1)[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _norm(value)[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(text)


def parse_actuals(data: bytes, name: str) -> tuple[list[ParsedRow], list[str], list[str]]:
    """Разбор файла. Возвращает (строки, ошибки, предупреждения); базу не трогает."""
    table = [row for row in _read_table(data, name) if any(_norm(cell) for cell in row)]
    if len(table) < 2:
        raise UploadError("в файле нет строк с задачами")
    header = [_norm(cell) for cell in table[0]]

    role_by_name: dict[str, int] = {}
    for row in db.query_dicts("SELECT role_id, canonical_name FROM roles"):
        role_by_name[_norm(row["canonical_name"]).lower()] = row["role_id"]
    for row in db.query_dicts("SELECT alias, role_id FROM role_aliases"):
        role_by_name[_norm(row["alias"]).lower()] = row["role_id"]

    columns: dict[int, str] = {}
    role_columns: dict[int, int] = {}
    unknown: list[str] = []
    for index, title in enumerate(header):
        key = title.lower()
        if key in FIXED_COLUMNS:
            columns[index] = FIXED_COLUMNS[key]
        elif key in role_by_name:
            role_columns[index] = role_by_name[key]
        elif title:
            unknown.append(title)
    if "task_id" not in columns.values() or "status" not in columns.values():
        raise UploadError(
            "нет обязательных колонок task_id и status",
            [f"заголовок файла: {', '.join(h for h in header if h)}"],
        )

    known = {row["task_id"] for row in db.query_dicts("SELECT task_id FROM tasks")}
    errors: list[str] = []
    warnings = [f"колонка «{title}» не распознана и пропущена" for title in unknown]
    rows: list[ParsedRow] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(table[1:], start=2):
        cells = {columns[i]: raw[i] for i in columns if i < len(raw)}
        task_id = _norm(cells.get("task_id"))
        if not task_id:
            continue
        where = f"строка {line_no}, {task_id}"
        if task_id not in known:
            errors.append(f"{where}: такой задачи нет в датасете")
            continue
        if task_id in seen:
            errors.append(f"{where}: задача указана дважды")
            continue
        seen.add(task_id)
        status = STATUS_ALIASES.get(_norm(cells.get("status")).lower().replace("_", " ").replace("-", " "))
        status = status or STATUS_ALIASES.get(_norm(cells.get("status")).lower().replace(" ", ""))
        if status is None:
            errors.append(f"{where}: статус «{_norm(cells.get('status'))}» — ожидается ToDo, InProgress или Done")
            continue
        try:
            start = _parse_date(cells.get("actual_start"))
            end = _parse_date(cells.get("actual_end"))
        except ValueError as exc:
            errors.append(f"{where}: дата «{exc}» — ожидается ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")
            continue
        hours: dict[int, Decimal] = {}
        bad_hours = False
        for index, role_id in role_columns.items():
            value = _norm(raw[index] if index < len(raw) else "")
            if not value:
                continue
            try:
                amount = Decimal(value.replace(",", "."))
            except InvalidOperation:
                errors.append(f"{where}: часы «{value}» в колонке «{header[index]}» — не число")
                bad_hours = True
                continue
            if amount < 0:
                errors.append(f"{where}: отрицательные часы в колонке «{header[index]}»")
                bad_hours = True
                continue
            if amount > 0:
                hours[role_id] = hours.get(role_id, Decimal("0")) + amount
        if bad_hours:
            continue
        rows.append(ParsedRow(task_id, status, start, end, _norm(cells.get("comment")) or None, hours))
    if not rows and not errors:
        errors.append("в файле нет ни одной задачи с task_id")
    return rows, errors, warnings


PREVIOUS_STATUS_SQL = """
SELECT s.task_id,
       COALESCE((SELECT a.status FROM task_actuals a
                   JOIN actual_uploads u ON u.upload_id = a.upload_id
                  WHERE a.task_id = s.task_id AND u.pi_id = %s AND u.sprint_no < %s
                  ORDER BY u.sprint_no DESC LIMIT 1), s.status) AS status
FROM tasks_seed_state s
"""


def load_actuals(data: bytes, filename: str | None, sprint_no: int) -> dict[str, Any]:
    """Факт спринта N → журнал → пересборка состояния → пересчёт на спринт N + 1."""
    if not data:
        raise UploadError("пустой файл")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    name = _safe_name(filename, f"actuals_sprint_{sprint_no}.csv")

    with WRITE_LOCK:
        pi = _pi()
        pi_id, sprint_count = pi["pi_id"], int(pi["sprint_count"])
        last = _last_sprint(pi_id)
        if not 1 <= sprint_no <= sprint_count:
            raise UploadError(f"спринт {sprint_no} вне квартала: допустимо 1..{sprint_count}")
        if sprint_no > last + 1:
            raise UploadError(
                f"факт загружается по порядку: сейчас загружен спринт {last}, "
                f"следующим может быть только {last + 1}"
            )

        rows, errors, warnings = parse_actuals(data, name)
        previous = {row["task_id"]: row["status"] for row in db.query_dicts(PREVIOUS_STATUS_SQL, (pi_id, sprint_no))}
        for row in rows:
            if previous.get(row.task_id) == "Done" and row.status != "Done":
                errors.append(f"{row.task_id}: была выполнена раньше, а в факте — {row.status}")
        if errors:
            raise UploadError(f"факт спринта {sprint_no} не принят: {len(errors)} ошибок", errors)

        sprint_end = db.scalar(
            "SELECT end_date FROM sprints WHERE pi_id = %s AND sprint_no = %s", (pi_id, sprint_no)
        )
        for row in rows:
            if row.status == "Done" and row.actual_end is None:
                row.actual_end = sprint_end
                warnings.append(f"{row.task_id}: дата окончания не указана — взят конец спринта {sprint_end}")
            if row.status != "Done" and row.actual_end is not None:
                warnings.append(f"{row.task_id}: дата окончания у невыполненной задачи пропущена")
                row.actual_end = None

        # Факт планом до загрузки нужен как база сравнения: если базового
        # прогона ещё нет (база залита из CLI), строим его ДО изменения состояния.
        baseline_created = None
        if not _has_baseline():
            baseline_created = run_plan(0)["run_id"]

        summary = {
            "tasks": len(rows),
            "done": sum(1 for row in rows if row.status == "Done"),
            "in_progress": sum(1 for row in rows if row.status == "InProgress"),
            "hours": str(sum((sum(row.hours.values(), Decimal("0")) for row in rows), Decimal("0"))),
            "warnings": warnings,
        }
        replaced = db.query_dicts(
            "SELECT sprint_no FROM actual_uploads WHERE pi_id = %s AND sprint_no >= %s ORDER BY 1",
            (pi_id, sprint_no),
        )
        import json

        with db.transaction() as cur:
            # Факт спринта N заменяет прежний факт N и все более поздние, а
            # прогоны, построенные на них, теряют смысл — удаляем вместе с ними.
            cur.execute(
                """DELETE FROM plan_runs WHERE actuals_upload_id IN
                   (SELECT upload_id FROM actual_uploads WHERE pi_id = %s AND sprint_no >= %s)""",
                (pi_id, sprint_no),
            )
            cur.execute(
                "DELETE FROM actual_uploads WHERE pi_id = %s AND sprint_no >= %s", (pi_id, sprint_no)
            )
            cur.execute(
                """INSERT INTO actual_uploads (pi_id, sprint_no, source_file, source_sha256, summary)
                   VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING upload_id""",
                (pi_id, sprint_no, name, hashlib.sha256(data).hexdigest(),
                 json.dumps(summary, ensure_ascii=False)),
            )
            upload_id = cur.fetchone()["upload_id"]
            cur.executemany(
                """INSERT INTO task_actuals (upload_id, task_id, status, actual_start, actual_end, comment)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [(upload_id, r.task_id, r.status, r.actual_start, r.actual_end, r.comment) for r in rows],
            )
            spent = [(upload_id, r.task_id, role_id, h) for r in rows for role_id, h in r.hours.items()]
            if spent:
                cur.executemany(
                    "INSERT INTO task_actual_spent (upload_id, task_id, role_id, hours) VALUES (%s, %s, %s, %s)",
                    spent,
                )
            cur.execute("SELECT apply_actuals()")

        plan = run_plan(sprint_no + 1)
    return {
        "upload_id": upload_id,
        "sprint_no": sprint_no,
        "file": name,
        "replaced_sprints": [row["sprint_no"] for row in replaced],
        "baseline_created_run_id": baseline_created,
        "summary": summary,
        "plan": plan,
    }
