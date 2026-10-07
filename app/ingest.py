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
import json
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app import db, planner
from app.assistant import generations

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
    "completed_sp": "completed_sp", "выполнено sp": "completed_sp",
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
def baseline_modes() -> dict[str, str]:
    """Режимы базового прогона: пересчёты наследуют их, а не сбрасываются к умолчанию."""
    row = db.query_one(
        "SELECT params FROM plan_runs WHERE as_of_sprint = 0 AND status IN ('ok', 'infeasible') "
        "ORDER BY run_id LIMIT 1"
    )
    params = (row or {}).get("params") or {}
    return {
        "dependency_mode": params.get("dependency_mode", planner.DEFAULT_DEPENDENCY_MODE),
        "initiative_mode": params.get("initiative_mode", planner.INITIATIVE_MODE_GREEDY),
        "priority_strategy": params.get("priority_strategy", planner.DEFAULT_PRIORITY_STRATEGY),
    }


def run_plan(as_of_sprint: int) -> dict[str, Any]:
    """Один прогон: чтение → чистая функция → запись. Возвращает краткую сводку.

    Базовый прогон (`as_of_sprint = 0`) идёт с режимами по умолчанию; пересчёты — с теми же
    режимами, с которыми построена база сравнения (зависимости, инициативы, стратегия приоритета).
    """
    inputs = planner.load_inputs()
    baseline_starts = planner.load_baseline_starts() if as_of_sprint > 0 else {}
    modes = baseline_modes() if as_of_sprint > 0 else {}
    options = {"as_of_sprint": as_of_sprint, "dependency_mode": modes.get(
        "dependency_mode", planner.DEFAULT_DEPENDENCY_MODE),
        "initiative_mode": modes.get("initiative_mode", planner.INITIATIVE_MODE_GREEDY),
        "priority_strategy": modes.get("priority_strategy", planner.DEFAULT_PRIORITY_STRATEGY),
        "simulate_next_pi": True}
    plan = planner.build_plan(inputs, baseline_starts=baseline_starts, **options)
    run_id = planner.write_plan(plan, inputs=inputs, baseline_starts=baseline_starts, options=options)
    return {
        "run_id": run_id,
        "as_of_sprint": as_of_sprint,
        "status": plan.status,
        "note": plan.note,
        "in_quarter": len(plan.in_quarter),
        "not_in_quarter": len(plan.deferred),
        "alerts": len(plan.alerts),
        "violations_error": 0,
    }


def compare_algorithms(as_of_sprint: int = 0, search_time_seconds: float = 10.0) -> dict[str, Any]:
    """Build and publish greedy and LNS plans from one identical input snapshot."""
    if not 0 <= as_of_sprint <= 12:
        raise UploadError("номер среза должен быть от 0 до 12")
    if not 1 <= search_time_seconds <= 60:
        raise UploadError("лимит поиска должен быть от 1 до 60 секунд")

    with WRITE_LOCK:
        inputs = planner.load_inputs()
        if as_of_sprint > inputs.sprint_count:
            raise UploadError(f"в этом PI только {inputs.sprint_count} спринтов")
        baseline_starts = planner.load_baseline_starts() if as_of_sprint > 0 else {}
        modes = baseline_modes() if as_of_sprint > 0 else {}
        dependency_mode = modes.get("dependency_mode", planner.DEFAULT_DEPENDENCY_MODE)
        initiative_mode = modes.get("initiative_mode", planner.INITIATIVE_MODE_GREEDY)
        priority_strategy = modes.get("priority_strategy", planner.DEFAULT_PRIORITY_STRATEGY)
        if priority_strategy == "task":
            raise UploadError(
                "двойной прогон пока недоступен при priority_strategy='task': "
                "lns-cpsat сравнивает приоритеты инициатив"
            )

        algorithms = (planner.ALGORITHM, planner.ALGORITHM_LOCAL_SEARCH)
        plans = []
        for algorithm in algorithms:
            options = {
                "as_of_sprint": as_of_sprint,
                "dependency_mode": dependency_mode,
                "initiative_mode": initiative_mode,
                "priority_strategy": priority_strategy,
                "simulate_next_pi": algorithm != planner.ALGORITHM_LOCAL_SEARCH,
                "algorithm": algorithm,
                "max_time_seconds": search_time_seconds,
                "random_seed": 0,
            }
            plan = planner.build_plan(inputs, baseline_starts=baseline_starts, **options)
            plans.append((plan, options))

        published: list[dict[str, Any]] = []
        for plan, options in plans:
            run_id = planner.write_plan(
                plan, inputs=inputs, baseline_starts=baseline_starts, options=options
            )
            optimization = plan.params.get("optimization") or {}
            published.append({
                "run_id": run_id,
                "algorithm": plan.params["algorithm"],
                "status": plan.status,
                "note": plan.note,
                "in_quarter": len(plan.in_quarter),
                "deferred": len(plan.deferred),
                "initiatives_complete": plan.params.get("initiatives_complete", 0),
                "loan_hh": plan.params.get("loan_hh", "0"),
                "solver_status": optimization.get("solver_status"),
                "schedule": [
                    {"task_id": row.task_id, "decision": row.decision,
                     "start_sprint": row.start_sprint, "end_sprint": row.end_sprint}
                    for row in plan.schedule
                ],
                "assignments": [
                    {"task_id": row.task_id, "sprint_no": row.sprint_no,
                     "engineer_id": row.engineer_id, "role_id": row.role_id,
                     "home_team_id": row.home_team_id, "hours": str(row.hours)}
                    for row in plan.assignments
                ],
                "story_points": [
                    {"task_id": task_id, "sprint_no": sprint_no, "sp": str(sp)}
                    for task_id, sprint_no, sp in plan.sp_shares
                ],
            })

    left, right = published
    left_schedule = {row["task_id"]: row for row in left["schedule"]}
    right_schedule = {row["task_id"]: row for row in right["schedule"]}
    left_assignments: dict[str, list[tuple[Any, ...]]] = {}
    right_assignments: dict[str, list[tuple[Any, ...]]] = {}
    for target, rows in ((left_assignments, left["assignments"]), (right_assignments, right["assignments"])):
        for row in rows:
            target.setdefault(row["task_id"], []).append(tuple(row[key] for key in (
                "sprint_no", "engineer_id", "role_id", "home_team_id", "hours"
            )))
    left_sp: dict[str, list[tuple[Any, ...]]] = {}
    right_sp: dict[str, list[tuple[Any, ...]]] = {}
    for target, rows in ((left_sp, left["story_points"]), (right_sp, right["story_points"])):
        for row in rows:
            target.setdefault(row["task_id"], []).append((row["sprint_no"], row["sp"]))

    differences = []
    for task_id in sorted(left_schedule.keys() | right_schedule.keys()):
        old = left_schedule.get(task_id, {})
        new = right_schedule.get(task_id, {})
        assignment_changed = sorted(left_assignments.get(task_id, ())) != sorted(right_assignments.get(task_id, ()))
        sp_changed = sorted(left_sp.get(task_id, ())) != sorted(right_sp.get(task_id, ()))
        schedule_changed = any(old.get(key) != new.get(key) for key in (
            "decision", "start_sprint", "end_sprint"
        ))
        if schedule_changed or assignment_changed or sp_changed:
            differences.append({
                "task_id": task_id,
                "greedy": old,
                "lns": new,
                "assignments_changed": assignment_changed,
                "story_points_changed": sp_changed,
            })
    return {
        "as_of_sprint": as_of_sprint,
        "input_sha256": inputs.source_sha256,
        "runs": published,
        "different_tasks": len(differences),
        "differences": differences,
    }


def _has_baseline() -> bool:
    return bool(
        db.scalar("SELECT COUNT(*) FROM plan_runs WHERE as_of_sprint = 0 AND status IN ('ok', 'infeasible')")
    )


# ---------------------------------------------------------------------------
#  датасет
# ---------------------------------------------------------------------------
def _snapshot_active_uploads(cur: Any, pi_id: str, from_sprint: int) -> None:
    """Freeze the KPI and schedule attached to reports before their runs are removed."""
    cur.execute(
        """UPDATE upload_revisions rev SET plan_snapshot = jsonb_build_object(
               'upload', to_jsonb(u),
               'runs', COALESCE((SELECT jsonb_agg(to_jsonb(r)) FROM plan_runs r
                   WHERE r.actuals_upload_id = u.upload_id), '[]'::jsonb),
               'kpis', COALESCE((SELECT jsonb_agg(to_jsonb(k)) FROM kpi_snapshots k
                   JOIN plan_runs r ON r.run_id = k.run_id
                   WHERE r.actuals_upload_id = u.upload_id), '[]'::jsonb),
               'schedule', COALESCE((SELECT jsonb_agg(to_jsonb(s)) FROM plan_task_schedule s
                   JOIN plan_runs r ON r.run_id = s.run_id
                   WHERE r.actuals_upload_id = u.upload_id), '[]'::jsonb)
           )
           FROM actual_uploads u
           WHERE u.pi_id = %s AND u.sprint_no >= %s
             AND rev.kind = 'actuals' AND rev.pi_id = u.pi_id
             AND rev.sprint_no = u.sprint_no AND rev.source_sha256 = u.source_sha256
             AND rev.superseded_by IS NULL""",
        (pi_id, from_sprint),
    )


def _replay_revision(kind: str, key: str | None, sha: str, pi_id: str,
                     sprint_no: int | None = None) -> dict[str, Any] | None:
    if not key:
        return None
    if len(key) > 120 or not key.isascii() or not key.isprintable():
        raise UploadError("Idempotency-Key должен быть печатной ASCII-строкой до 120 символов")
    row = db.query_one(
        "SELECT pi_id, sprint_no, source_sha256, response FROM upload_revisions "
        "WHERE kind = %s AND idempotency_key = %s", (kind, key),
    )
    if row is None:
        return None
    if row["pi_id"] != pi_id or row["sprint_no"] != sprint_no or row["source_sha256"] != sha:
        raise UploadError("Idempotency-Key уже использован для другого файла или периода")
    if row["response"] is None:
        raise UploadError("операция с этим Idempotency-Key ещё не завершена")
    return {**row["response"], "replayed": True}


def load_dataset(data: bytes, filename: str | None, *, actor: str | None = None,
                 idempotency_key: str | None = None) -> dict[str, Any]:
    """xlsx датасета → база с нуля → базовый план. Старые прогоны и факт стираются."""
    if not data:
        raise UploadError("пустой файл")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    name = _safe_name(filename, "dataset.xlsx")
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise UploadError("датасет принимается в формате .xlsx — как выданный организаторами")

    from etl import load as etl_load  # тяжёлый импорт (openpyxl) — только когда нужен

    current_pi = {"pi_id": etl_load.C.PI_ID, "start_date": etl_load.C.PI_START}
    if db.current_schema() != "public":
        current_pi = db.query_one(
            "SELECT pi_id, start_date FROM pi_periods ORDER BY pi_id LIMIT 1"
        ) or current_pi
    sha = hashlib.sha256(data).hexdigest()

    with WRITE_LOCK, tempfile.TemporaryDirectory() as tmp:
        replay = _replay_revision("dataset", idempotency_key, sha, current_pi["pi_id"])
        if replay is not None:
            return replay
        path = Path(tmp) / name
        path.write_bytes(data)
        try:
            if current_pi["pi_id"] == etl_load.C.PI_ID and current_pi["start_date"] == etl_load.C.PI_START:
                seed_sql, counts, dq = etl_load.build_seed_sql(path)
            else:
                seed_sql, counts, dq = etl_load.build_seed_sql(
                    path, pi_id=current_pi["pi_id"], pi_start=current_pi["start_date"],
                )
        except etl_load.DataQualityError as exc:
            raise UploadError(str(exc), exc.problems) from None
        except etl_load.DependencyGraphError as exc:
            raise UploadError("ошибка графа зависимостей", [str(exc)]) from None
        except SystemExit as exc:  # ETL сообщает о битой структуре листа так
            raise UploadError("структура листа не распознана", [str(exc).strip()]) from None
        except Exception as exc:  # noqa: BLE001 — не xlsx, битый zip и т.п.
            raise UploadError("файл не прочитан как датасет", [f"{type(exc).__name__}: {exc}"]) from None

        with db.atomic_transaction():
            replay = _replay_revision("dataset", idempotency_key, sha, current_pi["pi_id"])
            if replay is not None:
                return replay
            with db.transaction() as cur:
                _snapshot_active_uploads(cur, current_pi["pi_id"], 1)
                cur.execute(_strip_transaction(seed_sql))
                cur.execute(_strip_transaction(SUBSTITUTIONS_SQL.read_text(encoding="utf-8")))
                cur.execute(
                    "UPDATE load_batches SET loaded_by = %s "
                    "WHERE batch_id = (SELECT MAX(batch_id) FROM load_batches)", (actor,)
                )

            generations.activate(current_pi["pi_id"], sha)
            plan = run_plan(0)
            result = {"dataset": name, "sha256": sha, "rows": counts,
                      "data_quality": dq, "plan": plan}
            with db.transaction() as cur:
                cur.execute(
                    """INSERT INTO upload_revisions
                           (kind, pi_id, source_file, source_sha256, content, idempotency_key, response)
                       VALUES ('dataset', %s, %s, %s, %s, %s, %s::jsonb) RETURNING revision_id""",
                    (current_pi["pi_id"], name, sha, data, idempotency_key,
                     json.dumps(result, ensure_ascii=False, default=str)),
                )
                revision_id = cur.fetchone()["revision_id"]
                result["revision_id"] = revision_id
                cur.execute("UPDATE upload_revisions SET response = %s::jsonb WHERE revision_id = %s",
                            (json.dumps(result, ensure_ascii=False, default=str), revision_id))
                cur.execute(
                    "UPDATE upload_revisions SET superseded_by = %s "
                    "WHERE revision_id <> %s AND superseded_by IS NULL",
                    (revision_id, revision_id),
                )
                cur.execute(
                    "UPDATE public.pi_contexts SET dataset_version = %s "
                    "WHERE schema_name = current_schema()",
                    (sha,),
                )
    return result


# ---------------------------------------------------------------------------
#  шаблон факта
# ---------------------------------------------------------------------------
TEMPLATE_ROLES_SQL = """
SELECT DISTINCT r.role_id, r.canonical_name
FROM task_role_estimates e JOIN roles r ON r.role_id = e.role_id
ORDER BY r.role_id
"""
TEMPLATE_TASKS_SQL = """
SELECT t.task_id, COALESCE(previous.status, seed.status) AS status,
       CASE WHEN start_event.task_id IS NOT NULL THEN start_event.actual_start
            ELSE seed.actual_start END AS actual_start,
       t.summary
FROM tasks_seed_state seed
JOIN tasks t ON t.task_id = seed.task_id
LEFT JOIN LATERAL (
    SELECT a.status FROM task_actuals a
    JOIN actual_uploads u ON u.upload_id = a.upload_id
    WHERE a.task_id = seed.task_id AND u.pi_id = %s AND u.sprint_no < %s
    ORDER BY u.sprint_no DESC LIMIT 1
) previous ON TRUE
LEFT JOIN LATERAL (
    SELECT a.task_id, a.actual_start FROM task_actuals a
    JOIN actual_uploads u ON u.upload_id = a.upload_id
    WHERE a.task_id = seed.task_id AND u.pi_id = %s AND u.sprint_no < %s
      AND (a.actual_start IS NOT NULL OR a.clear_actual_start)
    ORDER BY u.sprint_no DESC LIMIT 1
) start_event ON TRUE
WHERE COALESCE(previous.status, seed.status) <> 'Done'
ORDER BY t.task_id
"""


def _pi() -> dict[str, Any]:
    pi = db.query_one("SELECT pi_id, sprint_count FROM pi_periods ORDER BY pi_id LIMIT 1")
    if not pi:
        raise UploadError("в базе нет датасета: сначала загрузите его")
    return pi


def _last_sprint(pi_id: str) -> int:
    return int(
        db.scalar("SELECT COALESCE(MAX(sprint_no), 0) FROM actual_uploads WHERE pi_id = %s AND coverage_status = 'complete'", (pi_id,))
        or 0
    )


def actuals_template(sprint_no: int | None = None) -> tuple[str, bytes]:
    """CSV: задачи и даты на начало выбранного спринта, до его факта."""
    pi = _pi()
    last = _last_sprint(pi["pi_id"])
    sprint = sprint_no if sprint_no is not None else min(last + 1, int(pi["sprint_count"]))
    if not 1 <= sprint <= int(pi["sprint_count"]) or sprint > last + 1:
        raise UploadError(f"шаблон доступен для спринтов 1..{min(last + 1, int(pi['sprint_count']))}")
    roles = db.query_dicts(TEMPLATE_ROLES_SQL)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["task_id", "status", "actual_start", "actual_end", "completed_sp", "comment"]
        + [row["canonical_name"] for row in roles]
    )
    for row in db.query_dicts(TEMPLATE_TASKS_SQL, (pi["pi_id"], sprint, pi["pi_id"], sprint)):
        writer.writerow(
            [row["task_id"], row["status"], row["actual_start"] or "", "", "", ""] + [""] * len(roles)
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
    completed_sp: Decimal | None = None
    clear_actual_start: bool = False
    clear_actual_end: bool = False


def normalize_actual_dates(
    rows: list[ParsedRow], *, sprint_end: date, pi_start: date, pi_end: date,
    today: date | None = None, previous: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Проверить даты события до записи факта; дата отчёта не заменяет дату Done."""
    errors: list[str] = []
    warnings = normalize_done_dates(rows, previous or {}, sprint_end)
    today = today or date.today()
    for row in rows:
        if row.actual_start is not None and row.actual_start > sprint_end:
            errors.append(f"{row.task_id}: дата начала {row.actual_start} позже конца отчётного спринта {sprint_end}")
        if row.actual_start is not None and row.actual_start > today:
            errors.append(f"{row.task_id}: дата начала {row.actual_start} ещё не наступила")
        if row.status == "Done" and row.actual_end is not None:
            if row.actual_end > today:
                errors.append(f"{row.task_id}: дата окончания {row.actual_end} ещё не наступила")
            if not pi_start <= row.actual_end <= pi_end:
                errors.append(f"{row.task_id}: дата окончания {row.actual_end} вне PI {pi_start}..{pi_end}")
            elif row.actual_end > sprint_end:
                errors.append(f"{row.task_id}: дата окончания {row.actual_end} позже конца отчётного спринта {sprint_end}")
            if row.actual_start is not None and row.actual_start > row.actual_end:
                errors.append(f"{row.task_id}: дата начала {row.actual_start} позже даты окончания {row.actual_end}")
    return errors, warnings


def _read_table(data: bytes, name: str) -> list[list[Any]]:
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        except Exception as exc:  # noqa: BLE001
            raise UploadError("файл не прочитан как xlsx", [str(exc)]) from None
        return [list(row) for row in wb.worksheets[0].iter_rows(values_only=True)]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadError("CSV должен быть в кодировке UTF-8", [str(exc)]) from None
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
    text = _norm(value)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(text)


def _parse_date_event(value: Any) -> tuple[date | None, bool]:
    """Пусто = сохранить, CLEAR = очистить, дата = установить или исправить."""
    if _norm(value).lower() in ("clear", "очистить"):
        return None, True
    return _parse_date(value), False


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
    unknown: dict[int, str] = {}
    canonical_headers: dict[str, int] = {}
    for index, title in enumerate(header):
        key = title.lower()
        if key in FIXED_COLUMNS:
            canonical = FIXED_COLUMNS[key]
            columns[index] = canonical
        elif key in role_by_name:
            role_id = role_by_name[key]
            canonical = f"role:{role_id}"
            role_columns[index] = role_id
        else:
            unknown[index] = title
            continue
        if canonical in canonical_headers:
            first = canonical_headers[canonical]
            raise UploadError("повторяются колонки факта", [
                f"колонки {first + 1} «{header[first]}» и {index + 1} «{title}» обозначают одно поле"
            ])
        canonical_headers[canonical] = index
    if "task_id" not in columns.values() or "status" not in columns.values():
        raise UploadError(
            "нет обязательных колонок task_id и status",
            [f"заголовок файла: {', '.join(h for h in header if h)}"],
        )

    known = {row["task_id"] for row in db.query_dicts("SELECT task_id FROM tasks")}
    errors: list[str] = []
    warnings = [f"колонка «{title}» не распознана и пропущена" for title in unknown.values() if title]
    rows: list[ParsedRow] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(table[1:], start=2):
        for index, title in unknown.items():
            if index < len(raw) and _norm(raw[index]):
                errors.append(
                    f"строка {line_no}: данные в неизвестной колонке {index + 1} «{title}»"
                )
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
            start, clear_start = _parse_date_event(cells.get("actual_start"))
            end, clear_end = _parse_date_event(cells.get("actual_end"))
        except ValueError as exc:
            errors.append(f"{where}: дата «{exc}» — ожидается ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")
            continue
        if status == "Done" and clear_end:
            errors.append(f"{where}: нельзя очистить дату окончания выполненной задачи")
            continue
        if start is not None and end is not None and start > end:
            errors.append(f"{where}: дата начала {start} позже даты окончания {end}")
            continue
        completed_sp: Decimal | None = None
        raw_sp = _norm(cells.get("completed_sp"))
        if raw_sp:
            try:
                completed_sp = Decimal(raw_sp.replace(",", "."))
            except InvalidOperation:
                errors.append(f"{where}: выполнено SP «{raw_sp}» — не число")
                continue
            if (not completed_sp.is_finite() or completed_sp < 0
                    or completed_sp > Decimal("9999.99")
                    or completed_sp % Decimal("0.01") != 0):
                errors.append(f"{where}: выполнено SP должно быть конечным числом от 0 до 9999,99 с точностью 0,01")
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
            if (not amount.is_finite() or amount < 0 or amount > Decimal("999999.99")
                    or amount % Decimal("0.01") != 0):
                errors.append(f"{where}: часы в колонке «{header[index]}» должны быть конечным числом от 0 до 999999,99 с точностью 0,01")
                bad_hours = True
                continue
            hours[role_id] = amount  # Explicit zero is different from a blank cell.
        if bad_hours:
            continue
        rows.append(ParsedRow(task_id, status, start, end, _norm(cells.get("comment")) or None,
                              hours, completed_sp, clear_start, clear_end))
    if not rows and not errors:
        errors.append("в файле нет ни одной задачи с task_id")
    return rows, errors, warnings


def validate_completed_sp(
    rows: list[ParsedRow], limits: dict[str, Decimal], prior: dict[str, Decimal],
) -> list[str]:
    errors: list[str] = []
    for row in rows:
        if row.completed_sp is None:
            continue
        limit = limits[row.task_id]
        if prior.get(row.task_id, Decimal(0)) + row.completed_sp > limit:
            errors.append(f"{row.task_id}: подтверждённые SP превышают исходную оценку {limit}")
    return errors


def normalize_done_dates(
    rows: list[ParsedRow], previous: dict[str, str], sprint_end: date,
) -> list[str]:
    """Только новый Done без даты получает конец отчётного спринта."""
    warnings: list[str] = []
    for row in rows:
        if row.status == "Done" and row.actual_end is None and previous.get(row.task_id) != "Done":
            row.actual_end = sprint_end
            warnings.append(f"{row.task_id}: дата окончания не указана — взят конец спринта {sprint_end}")
        if row.status != "Done" and row.actual_end is not None:
            warnings.append(f"{row.task_id}: дата окончания у невыполненной задачи пропущена")
            row.actual_end = None
    return warnings


PREVIOUS_STATUS_SQL = """
SELECT s.task_id,
       COALESCE((SELECT a.status FROM task_actuals a
                   JOIN actual_uploads u ON u.upload_id = a.upload_id
                  WHERE a.task_id = s.task_id AND u.pi_id = %s AND u.sprint_no < %s
                  ORDER BY u.sprint_no DESC LIMIT 1), s.status) AS status
FROM tasks_seed_state s
"""


def analyze_actual_rows(
    rows: list[ParsedRow], previous: dict[str, str],
    estimates: dict[tuple[str, int], Decimal], prior_spent: dict[tuple[str, int], Decimal],
) -> tuple[list[tuple[str, int | None, str, str, str]], list[str]]:
    """Keep exceptional real work, but require a stated reason and expose it."""
    issues: list[tuple[str, int | None, str, str, str]] = []
    errors: list[str] = []
    for row in rows:
        findings: list[tuple[int | None, str, str]] = []
        positive = {role: hours for role, hours in row.hours.items() if hours > 0}
        if row.status == "ToDo" and positive:
            findings.append((None, "TODO_WITH_HOURS", "статус ToDo при новых затратах"))
        if previous.get(row.task_id) == "Done" and positive:
            findings.append((None, "DONE_WITH_NEW_HOURS", "новые часы после завершения"))
        if previous.get(row.task_id) == "InProgress" and row.status == "ToDo":
            findings.append((None, "STATUS_REGRESSION", "переход InProgress → ToDo"))
        for role_id, hours in positive.items():
            estimate = estimates.get((row.task_id, role_id))
            if estimate is None:
                findings.append((role_id, "UNPLANNED_ROLE", f"роль {role_id} не предусмотрена сметой"))
            elif prior_spent.get((row.task_id, role_id), Decimal(0)) + hours > estimate:
                findings.append((role_id, "ROLE_OVERRUN", f"роль {role_id}: часы превысили смету {estimate}"))
        if findings and not row.comment:
            errors.append(f"{row.task_id}: укажите причину исключения в comment: "
                          + ", ".join(item[2] for item in findings))
        else:
            issues.extend((row.task_id, role, code, detail, row.comment or "")
                          for role, code, detail in findings)
    return issues, errors


def load_actuals(data: bytes, filename: str | None, sprint_no: int,
                 *, confirm_complete: bool = False, actor: str | None = None,
                 idempotency_key: str | None = None, confirm_duplicate: bool = False) -> dict[str, Any]:
    """Save a report; only confirmed, fully covered reports close the sprint."""
    if not data:
        raise UploadError("пустой файл")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    name = _safe_name(filename, f"actuals_sprint_{sprint_no}.csv")
    sha = hashlib.sha256(data).hexdigest()

    with WRITE_LOCK, db.atomic_transaction():
        pi = _pi()
        pi_id, sprint_count = pi["pi_id"], int(pi["sprint_count"])
        replay = _replay_revision("actuals", idempotency_key, sha, pi_id, sprint_no)
        if replay is not None:
            return replay
        last = _last_sprint(pi_id)
        if not 1 <= sprint_no <= sprint_count:
            raise UploadError(f"спринт {sprint_no} вне квартала: допустимо 1..{sprint_count}")
        if sprint_no > last + 1:
            raise UploadError(
                f"факт загружается по порядку: сейчас загружен спринт {last}, "
                f"следующим может быть только {last + 1}"
            )

        rows, errors, warnings = parse_actuals(data, name)
        if any(row.completed_sp is not None for row in rows):
            limits = {
                row["task_id"]: Decimal(row["estimation_sp"] or 0)
                for row in db.query_dicts("SELECT task_id, estimation_sp FROM tasks")
            }
            prior_sp = {
                row["task_id"]: Decimal(row["completed_sp"])
                for row in db.query_dicts(
                    """SELECT a.task_id, COALESCE(SUM(a.completed_sp), 0) AS completed_sp
                       FROM task_actuals a JOIN actual_uploads u ON u.upload_id = a.upload_id
                       WHERE u.pi_id = %s AND u.sprint_no < %s
                       GROUP BY a.task_id""", (pi_id, sprint_no)
                )
            }
            errors.extend(validate_completed_sp(rows, limits, prior_sp))
        previous = {row["task_id"]: row["status"] for row in db.query_dicts(PREVIOUS_STATUS_SQL, (pi_id, sprint_no))}
        for row in rows:
            if previous.get(row.task_id) == "Done" and row.status != "Done":
                errors.append(f"{row.task_id}: была выполнена раньше, а в факте — {row.status}")
        if errors:
            raise UploadError(f"факт спринта {sprint_no} не принят: {len(errors)} ошибок", errors)

        calendar = db.query_one(
            """SELECT s.start_date AS sprint_start, s.end_date AS sprint_end,
                      p.start_date AS pi_start, p.end_date AS pi_end
                 FROM sprints s JOIN pi_periods p ON p.pi_id = s.pi_id
                WHERE s.pi_id = %s AND s.sprint_no = %s""",
            (pi_id, sprint_no),
        )
        if not calendar:
            raise UploadError(f"для спринта {sprint_no} нет календаря PI")
        date_errors, date_warnings = normalize_actual_dates(
            rows, sprint_end=calendar["sprint_end"],
            pi_start=calendar["pi_start"], pi_end=calendar["pi_end"],
            previous=previous,
        )
        errors.extend(date_errors)
        warnings.extend(date_warnings)
        if errors:
            raise UploadError(f"факт спринта {sprint_no} не принят: {len(errors)} ошибок", errors)

        estimates = {
            (item["task_id"], item["role_id"]): Decimal(item["hours"])
            for item in db.query_dicts("SELECT task_id, role_id, hours FROM task_role_estimates")
        }
        prior_spent = {
            (item["task_id"], item["role_id"]): Decimal(item["hours"])
            for item in db.query_dicts(
                """SELECT task_id, role_id, SUM(hours) AS hours FROM (
                       SELECT task_id, role_id, hours FROM task_role_spent_seed
                       UNION ALL
                       SELECT a.task_id, a.role_id, a.hours FROM task_actual_spent a
                       JOIN actual_uploads u ON u.upload_id = a.upload_id
                       WHERE u.pi_id = %s AND u.sprint_no < %s AND u.coverage_status = 'complete'
                   ) history GROUP BY task_id, role_id""", (pi_id, sprint_no)
            )
        }
        report_issues, issue_errors = analyze_actual_rows(rows, previous, estimates, prior_spent)
        if issue_errors:
            raise UploadError(f"факт спринта {sprint_no} не принят: нужны причины исключений", issue_errors)
        warnings.extend(f"{task_id}: {detail} (причина: {reason})"
                        for task_id, _role, _code, detail, reason in report_issues)

        expected_tasks = {
            item["task_id"] for item in db.query_dicts(TEMPLATE_TASKS_SQL, (pi_id, sprint_no, pi_id, sprint_no))
        }
        reported = {row.task_id: row for row in rows}
        missing_tasks = sorted(expected_tasks - reported.keys())
        expected_roles = db.query_dicts(
            "SELECT task_id, role_id FROM v_task_remaining_hh WHERE remaining_hours > 0"
        )
        missing_roles = sorted(
            f"{item['task_id']}/роль {item['role_id']}"
            for item in expected_roles
            if item["task_id"] in expected_tasks
            and item["task_id"] in reported
            and item["role_id"] not in reported[item["task_id"]].hours
        )
        coverage_status = (
            "incomplete" if missing_tasks or missing_roles else
            "complete" if confirm_complete else "draft"
        )
        if coverage_status == "incomplete":
            warnings.append(
                f"отчёт не закрывает спринт: нет {len(missing_tasks)} задач и "
                f"{len(missing_roles)} ячеек часов по ролям; пустая ячейка не равна 0"
            )

        duplicate = db.query_one(
            """SELECT sprint_no FROM actual_uploads WHERE pi_id = %s
                 AND sprint_no <> %s AND source_sha256 = %s ORDER BY sprint_no DESC LIMIT 1""",
            (pi_id, sprint_no, sha),
        )
        if duplicate is not None and not confirm_duplicate:
            raise UploadError(
                f"тот же файл уже загружен за спринт {duplicate['sprint_no']}; "
                "подтвердите, что это отдельный отчёт за новый период"
            )

        # Факт планом до загрузки нужен как база сравнения: если базового
        # прогона ещё нет (база залита из CLI), строим его ДО изменения состояния.
        baseline_created = None
        if not _has_baseline():
            baseline_created = run_plan(0)["run_id"]

        plan_run_id = db.scalar(
            """SELECT r.run_id FROM plan_runs r
               WHERE r.pi_id = %s AND r.status IN ('ok', 'infeasible') AND r.as_of_sprint <= %s
                 AND (r.actuals_upload_id IS NULL OR r.actuals_upload_id IN
                      (SELECT u.upload_id FROM actual_uploads u
                       WHERE u.pi_id = %s AND u.sprint_no < %s))
               ORDER BY r.as_of_sprint DESC, r.run_id DESC LIMIT 1""",
            (pi_id, sprint_no, pi_id, sprint_no),
        )

        summary = {
            "tasks": len(rows),
            "done": sum(1 for row in rows if row.status == "Done"),
            "in_progress": sum(1 for row in rows if row.status == "InProgress"),
            "hours": str(sum((sum(row.hours.values(), Decimal("0")) for row in rows), Decimal("0"))),
            "warnings": warnings,
            "coverage_status": coverage_status,
            "expected_tasks": len(expected_tasks),
            "missing_tasks": missing_tasks,
            "missing_role_cells": missing_roles,
        }
        replaced = db.query_dicts(
            "SELECT sprint_no FROM actual_uploads WHERE pi_id = %s AND sprint_no >= %s ORDER BY 1",
            (pi_id, sprint_no),
        )
        with db.transaction() as cur:
            cur.execute(
                """INSERT INTO upload_revisions
                       (kind, pi_id, sprint_no, source_file, source_sha256, content, idempotency_key)
                   VALUES ('actuals', %s, %s, %s, %s, %s, %s) RETURNING revision_id""",
                (pi_id, sprint_no, name, sha, data, idempotency_key),
            )
            revision_id = cur.fetchone()["revision_id"]
            _snapshot_active_uploads(cur, pi_id, sprint_no)
            cur.execute(
                """UPDATE upload_revisions SET superseded_by = %s
                   WHERE revision_id <> %s AND kind = 'actuals' AND pi_id = %s
                     AND sprint_no >= %s AND superseded_by IS NULL""",
                (revision_id, revision_id, pi_id, sprint_no),
            )
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
                """INSERT INTO actual_uploads
                       (pi_id, sprint_no, plan_run_id, source_file, source_sha256, summary, coverage_status,
                        uploaded_by)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING upload_id""",
                (pi_id, sprint_no, plan_run_id, name, sha,
                 json.dumps(summary, ensure_ascii=False), coverage_status, actor),
            )
            upload_id = cur.fetchone()["upload_id"]
            cur.executemany(
                """INSERT INTO task_actuals
                       (upload_id, task_id, status, actual_start, actual_end, comment,
                        completed_sp, clear_actual_start, clear_actual_end)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(upload_id, r.task_id, r.status, r.actual_start, r.actual_end, r.comment,
                  r.completed_sp, r.clear_actual_start, r.clear_actual_end) for r in rows],
            )
            spent = [(upload_id, r.task_id, role_id, h) for r in rows for role_id, h in r.hours.items()]
            if spent:
                cur.executemany(
                    "INSERT INTO task_actual_spent (upload_id, task_id, role_id, hours) VALUES (%s, %s, %s, %s)",
                    spent,
                )
            if report_issues:
                cur.executemany(
                    """INSERT INTO actual_report_issues
                       (upload_id, task_id, role_id, issue_code, detail, reason)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    [(upload_id, *issue) for issue in report_issues],
                )
            cur.execute("SELECT apply_actuals()")

        plan = run_plan(sprint_no + 1) if coverage_status == "complete" else None
        result = {
            "upload_id": upload_id,
            "sprint_no": sprint_no,
            "file": name,
            "replaced_sprints": [row["sprint_no"] for row in replaced],
            "baseline_created_run_id": baseline_created,
            "summary": summary,
            "plan": plan,
            "revision_id": revision_id,
        }
        with db.transaction() as cur:
            cur.execute("UPDATE upload_revisions SET response = %s::jsonb WHERE revision_id = %s",
                        (json.dumps(result, ensure_ascii=False, default=str), revision_id))
    return result


def confirm_role_etc(task_id: str, role_id: int, remaining_hours: Decimal,
                     reason: str, *, actor: str | None = None) -> dict[str, Any]:
    """Confirm a new/overrun role's remaining work and republish the plan."""
    if not task_id or role_id <= 0 or not remaining_hours.is_finite() or remaining_hours < 0:
        raise UploadError("некорректная задача, роль или остаток часов")
    if remaining_hours > Decimal("999999.99") or remaining_hours % Decimal("0.01") != 0:
        raise UploadError("остаток часов должен иметь точность 0,01")
    reason = reason.strip()
    if not reason:
        raise UploadError("укажите причину пересмотра оценки")
    with WRITE_LOCK, db.atomic_transaction():
        issue = db.query_one(
            """SELECT i.upload_id FROM actual_report_issues i
               JOIN actual_uploads u ON u.upload_id = i.upload_id
               WHERE i.task_id = %s AND i.role_id = %s
                 AND i.issue_code IN ('UNPLANNED_ROLE','ROLE_OVERRUN')
                 AND i.resolved_revision_id IS NULL AND u.coverage_status = 'complete'
               ORDER BY i.upload_id DESC LIMIT 1""", (task_id, role_id)
        )
        if not issue:
            raise UploadError("нет подтверждённого отчёта с неразобранной ролью задачи")
        spent = Decimal(db.scalar(
            "SELECT COALESCE(hours, 0) FROM task_role_spent WHERE task_id = %s AND role_id = %s",
            (task_id, role_id),
        ) or 0)
        with db.transaction() as cur:
            cur.execute(
                """INSERT INTO task_role_estimates (task_id, role_id, hours)
                   VALUES (%s, %s, %s) ON CONFLICT (task_id, role_id) DO NOTHING""",
                (task_id, role_id, max(spent + remaining_hours, Decimal("0.01"))),
            )
            cur.execute(
                """INSERT INTO task_role_etc (task_id, role_id, remaining_hours, reason, revised_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING revision_id""",
                (task_id, role_id, remaining_hours, reason, actor),
            )
            revision_id = cur.fetchone()["revision_id"]
            cur.execute(
                """UPDATE actual_report_issues SET resolved_revision_id = %s
                   WHERE upload_id = %s AND task_id = %s AND role_id = %s
                     AND issue_code IN ('UNPLANNED_ROLE','ROLE_OVERRUN')""",
                (revision_id, issue["upload_id"], task_id, role_id),
            )
        pi = _pi()
        plan = run_plan(min(_last_sprint(pi["pi_id"]) + 1, int(pi["sprint_count"]) + 1))
    return {"task_id": task_id, "role_id": role_id,
            "remaining_hours": str(remaining_hours), "revision_id": revision_id, "plan": plan}


def set_initiative_priority(prodf_id: str, business_priority: int | None, note: str,
                            *, actor: str | None = None) -> dict[str, Any]:
    """Задать (или снять) явный бизнес-приоритет инициативы и пересчитать план (DA-11, ADR-032)."""
    if business_priority is not None and not 0 <= business_priority <= 1000:
        raise UploadError("приоритет должен быть целым числом от 0 до 1000 в шкале rung")
    if business_priority is not None and not note.strip():
        raise UploadError("укажите основание приоритета: его потом спросят")
    with WRITE_LOCK, db.atomic_transaction():
        if db.query_one("SELECT 1 AS ok FROM initiatives WHERE prodf_id = %s", (prodf_id,)) is None:
            raise UploadError(f"нет инициативы {prodf_id}")
        with db.transaction() as cur:
            cur.execute(
                """UPDATE initiatives SET business_priority = %s, business_priority_by = %s,
                          business_priority_at = CASE WHEN %s IS NULL THEN NULL ELSE now() END,
                          business_priority_note = %s
                   WHERE prodf_id = %s""",
                (business_priority, actor if business_priority is not None else None, business_priority,
                 note.strip() or None, prodf_id),
            )
        pi = _pi()
        plan = run_plan(min(_last_sprint(pi["pi_id"]) + 1, int(pi["sprint_count"]) + 1))
    return {"prodf_id": prodf_id, "business_priority": business_priority, "plan": plan}


def confirm_task_goal(task_id: str, closure_code: str, goal_code: str | None,
                      confirmed_by: str, note: str) -> dict[str, Any]:
    """Record business acceptance separately from technical Done and proposals."""
    if closure_code not in {"ACHIEVED", "NOT_ACHIEVED", "CANCELLED_BY_CUSTOMER"}:
        raise UploadError("неизвестный результат закрытия")
    if closure_code == "ACHIEVED" and goal_code not in {f"R{n}" for n in range(1, 7)}:
        raise UploadError("для достигнутого результата укажите этап R1–R6")
    if goal_code is not None and goal_code not in {f"R{n}" for n in range(1, 7)}:
        raise UploadError("этап результата должен быть R1–R6")
    if not confirmed_by.strip() or not note.strip():
        raise UploadError("укажите подтвердившего и основание решения")
    with WRITE_LOCK, db.atomic_transaction():
        task = db.query_one("SELECT status FROM tasks WHERE task_id = %s", (task_id,))
        if task is None or task["status"] != "Done":
            raise UploadError("бизнес результат можно подтвердить после фактического завершения задачи")
        with db.transaction() as cur:
            cur.execute(
                """INSERT INTO task_goal_confirmations
                   (task_id, goal_code, closure_code, confirmed_by, note)
                   VALUES (%s, %s, %s, %s, %s) RETURNING confirmation_id""",
                (task_id, goal_code, closure_code, confirmed_by.strip(), note.strip()),
            )
            confirmation_id = cur.fetchone()["confirmation_id"]
        pi = _pi()
        plan = run_plan(min(_last_sprint(pi["pi_id"]) + 1, int(pi["sprint_count"]) + 1))
    return {"confirmation_id": confirmation_id, "task_id": task_id, "plan": plan}
