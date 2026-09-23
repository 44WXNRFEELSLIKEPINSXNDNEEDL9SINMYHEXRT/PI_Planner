"""Подключение к PostgreSQL — единственное место, где живёт DSN.

Приоритет источника строки подключения:
  1. переменная окружения PI_PLANNER_DSN
  2. dsn.json в корне репозитория (в .gitignore, см. dsn.example.json)
  3. значение по умолчанию (локальная dev-база из docs/RUNBOOK.md)

По умолчанию соединение READ ONLY. Писать в базу умеет только явный
`transaction()` — это защита от того, чтобы экран случайно не переписал
контракт планировщика.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
DSN_FILE = ROOT / "dsn.json"

DEFAULT_DSN = "host=127.0.0.1 port=5432 dbname=pi_planner user=postgres password=postgres"
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000
DB_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0)


class _DbTelemetry:
    """Ограниченная по кардинальности телеметрия DB-клиента.

    `operation` задаёт вызывающий код или имя обёртки. SQL и параметры сюда
    никогда не попадают: они содержат данные и создают неограниченные серии.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connections = 0
        self.operations = 0
        self.connection_total: dict[str, int] = defaultdict(int)
        self.connection_sum = 0.0
        self.connection_count = 0
        self.connection_buckets: dict[float, int] = defaultdict(int)
        self.operation_total: dict[tuple[str, str], int] = defaultdict(int)
        self.operation_sum: dict[str, float] = defaultdict(float)
        self.operation_count: dict[str, int] = defaultdict(int)
        self.operation_buckets: dict[tuple[str, float], int] = defaultdict(int)
        self.rows_total: dict[str, int] = defaultdict(int)

    def connect(self, outcome: str, seconds: float) -> None:
        with self.lock:
            self.connection_total[outcome] += 1
            self.connection_sum += seconds
            self.connection_count += 1
            for bound in DB_DURATION_BUCKETS:
                if seconds <= bound:
                    self.connection_buckets[bound] += 1

    def operation_enter(self) -> None:
        with self.lock:
            self.operations += 1

    def operation_leave(self, operation: str, outcome: str, seconds: float, rows: int = 0) -> None:
        with self.lock:
            self.operations -= 1
            self.operation_total[(operation, outcome)] += 1
            self.operation_sum[operation] += seconds
            self.operation_count[operation] += 1
            self.rows_total[operation] += max(0, rows)
            for bound in DB_DURATION_BUCKETS:
                if seconds <= bound:
                    self.operation_buckets[(operation, bound)] += 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "connections": self.connections,
                "operations": self.operations,
                "connection_total": dict(self.connection_total),
                "connection_sum": self.connection_sum,
                "connection_count": self.connection_count,
                "connection_buckets": dict(self.connection_buckets),
                "operation_total": dict(self.operation_total),
                "operation_sum": dict(self.operation_sum),
                "operation_count": dict(self.operation_count),
                "operation_buckets": dict(self.operation_buckets),
                "rows_total": dict(self.rows_total),
            }

    def reset(self) -> None:
        with self.lock:
            self.__init_unlocked()

    def __init_unlocked(self) -> None:
        self.connections = 0
        self.operations = 0
        self.connection_total.clear()
        self.connection_sum = 0.0
        self.connection_count = 0
        self.connection_buckets.clear()
        self.operation_total.clear()
        self.operation_sum.clear()
        self.operation_count.clear()
        self.operation_buckets.clear()
        self.rows_total.clear()


_TELEMETRY = _DbTelemetry()


def metrics_snapshot() -> dict[str, Any]:
    """Снимок внутренних метрик DB-клиента для `/metrics`."""
    return _TELEMETRY.snapshot()


def reset_metrics() -> None:
    """Сброс только для изолированных тестов."""
    _TELEMETRY.reset()


def load_config() -> dict[str, Any]:
    """Собирает конфигурацию подключения из трёх источников (см. модуль)."""
    cfg: dict[str, Any] = {
        "dsn": DEFAULT_DSN,
        "read_only": True,
        "statement_timeout_ms": DEFAULT_STATEMENT_TIMEOUT_MS,
    }

    if DSN_FILE.exists():
        try:
            file_cfg = json.loads(DSN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:  # пусть падает громко: молчаливый
            raise RuntimeError(f"dsn.json не парсится: {exc}") from exc  # откат к дефолту хуже
        for key in cfg:
            if key in file_cfg and file_cfg[key] not in (None, ""):
                cfg[key] = file_cfg[key]

    env_dsn = os.environ.get("PI_PLANNER_DSN", "").strip()
    if env_dsn:
        cfg["dsn"] = env_dsn

    cfg["read_only"] = bool(cfg["read_only"])
    cfg["statement_timeout_ms"] = int(cfg["statement_timeout_ms"])
    return cfg


def dsn() -> str:
    """Строка подключения без пароля — для логов и диагностики."""
    parts = [p for p in load_config()["dsn"].split() if not p.startswith("password=")]
    return " ".join(parts)


@contextmanager
def connection(read_only: bool | None = None) -> Iterator[psycopg.Connection]:
    """Соединение с гарантированным таймаутом запроса.

    `read_only=None` — берём значение из конфигурации (по умолчанию True).

    Режим и таймаут задаём опциями libpq при старте соединения, а НЕ через
    `SET` после connect: psycopg открывает транзакцию первым же запросом, и
    `SET default_transaction_read_only` её уже не меняет — `CREATE TABLE`
    внутри той же транзакции проходил (проверено на этой базе).
    """
    cfg = load_config()
    effective_read_only = cfg["read_only"] if read_only is None else read_only

    opts = [f"statement_timeout={int(cfg['statement_timeout_ms'])}"]
    if effective_read_only:
        opts.append("default_transaction_read_only=on")

    started = time.perf_counter()
    try:
        conn = psycopg.connect(
            cfg["dsn"],
            options=" ".join(f"-c {opt}" for opt in opts),
            row_factory=dict_row,
        )
    except Exception:
        _TELEMETRY.connect("error", time.perf_counter() - started)
        raise
    _TELEMETRY.connect("success", time.perf_counter() - started)
    with _TELEMETRY.lock:
        _TELEMETRY.connections += 1
    try:
        with conn:
            yield conn
    finally:
        with _TELEMETRY.lock:
            _TELEMETRY.connections -= 1


def _finish_operation(operation: str, started: float, outcome: str, rows: int = 0) -> None:
    _TELEMETRY.operation_leave(operation, outcome, time.perf_counter() - started, rows)


def query_dicts(
    sql: str,
    params: Sequence[Any] | None = None,
    *,
    operation: str = "query_dicts",
) -> list[dict[str, Any]]:
    """SELECT → список словарей (имена колонок как есть из БД)."""
    started = time.perf_counter()
    _TELEMETRY.operation_enter()
    try:
        with connection(read_only=True) as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))
            rows = list(cur.fetchall())
    except Exception:
        _finish_operation(operation, started, "error")
        raise
    _finish_operation(operation, started, "success", len(rows))
    return rows


def query_one(
    sql: str,
    params: Sequence[Any] | None = None,
    *,
    operation: str = "query_one",
) -> dict[str, Any] | None:
    rows = query_dicts(sql, params, operation=operation)
    return rows[0] if rows else None


def query_bundle(queries: Sequence[tuple[str, str, bool]]) -> dict[str, Any]:
    """Несколько SELECT в одном согласованном read-only снимке.

    Элемент: `(operation, sql, one)`. При `one=True` возвращается первая строка
    или `{}`, иначе список строк. Каждая операция измеряется отдельно, но
    соединение и repeatable-read snapshot у набора общие.
    """
    result: dict[str, Any] = {}
    with connection(read_only=True) as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for operation, sql, one in queries:
            started = time.perf_counter()
            _TELEMETRY.operation_enter()
            try:
                cur.execute(sql)
                rows = list(cur.fetchall())
            except Exception:
                _finish_operation(operation, started, "error")
                raise
            _finish_operation(operation, started, "success", len(rows))
            result[operation] = (rows[0] if rows else {}) if one else rows
    return result


def scalar(
    sql: str,
    params: Sequence[Any] | None = None,
    *,
    operation: str = "scalar",
) -> Any:
    """Первое значение первой строки — для COUNT/SUM в проверках."""
    started = time.perf_counter()
    _TELEMETRY.operation_enter()
    try:
        with connection(read_only=True) as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))
            row = cur.fetchone()
    except Exception:
        _finish_operation(operation, started, "error")
        raise
    _finish_operation(operation, started, "success", 1 if row else 0)
    if not row:
        return None
    return next(iter(row.values()))


@contextmanager
def transaction(*, operation: str = "transaction") -> Iterator[psycopg.Cursor]:
    """Одна транзакция на много запросов — планировщик пишет контракт целиком.

    При исключении psycopg откатит всё: частично записанного прогона не бывает.
    """
    started = time.perf_counter()
    _TELEMETRY.operation_enter()
    try:
        with connection(read_only=False) as conn:
            with conn.cursor() as cur:
                yield cur
    except Exception:
        _finish_operation(operation, started, "rollback")
        raise
    _finish_operation(operation, started, "commit")


def health() -> dict[str, Any]:
    """Быстрая проверка живости базы — используется сервером и run.bat."""
    started = time.perf_counter()
    _TELEMETRY.operation_enter()
    try:
        with connection(read_only=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT current_setting('server_version')  AS server_version,
                       current_database()                 AS dbname
                """
            )
            info = cur.fetchone() or {}
            cur.execute(
                """
                SELECT COUNT(*) AS tables
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                """
            )
            tables = cur.fetchone() or {}
            cur.execute(
                """
                SELECT COUNT(*) AS views
                FROM information_schema.views
                WHERE table_schema = 'public'
                """
            )
            views = cur.fetchone() or {}
    except Exception:
        _finish_operation("health", started, "error")
        raise
    _finish_operation("health", started, "success", 3)
    return {
        "dsn": dsn(),
        "server_version": info.get("server_version"),
        "dbname": info.get("dbname"),
        "tables": tables.get("tables"),
        "views": views.get("views"),
    }


if __name__ == "__main__":  # uv run python -m app.db
    for key, value in health().items():
        print(f"{key}: {value}")
