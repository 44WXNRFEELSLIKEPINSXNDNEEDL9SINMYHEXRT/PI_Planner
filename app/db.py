"""Подключение к PostgreSQL — единственное место, где живёт DSN.

Приоритет источника строки подключения:
  1. переменная окружения PI_PLANNER_DSN
  2. dsn.json в корне репозитория (в .gitignore, см. dsn.example.json)
  3. значение по умолчанию (локальная dev-база из docs/RUNBOOK.md)

По умолчанию соединение READ ONLY. Писать в базу умеют только явные вызовы
`execute(..., read_only=False)` и `transaction()` — это защита от того, чтобы
экран случайно не переписал контракт планировщика.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
DSN_FILE = ROOT / "dsn.json"

DEFAULT_DSN = "host=127.0.0.1 port=5432 dbname=pi_planner user=postgres password=postgres"
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000


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

    with psycopg.connect(
        cfg["dsn"],
        options=" ".join(f"-c {opt}" for opt in opts),
        row_factory=dict_row,
    ) as conn:
        yield conn


def query_dicts(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """SELECT → список словарей (имена колонок как есть из БД)."""
    with connection(read_only=True) as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params or ()))
        return list(cur.fetchall())


def query_one(sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    rows = query_dicts(sql, params)
    return rows[0] if rows else None


def scalar(sql: str, params: Sequence[Any] | None = None) -> Any:
    """Первое значение первой строки — для COUNT/SUM в проверках."""
    with connection(read_only=True) as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params or ()))
        row = cur.fetchone()
    if not row:
        return None
    return next(iter(row.values()))


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Запись, уважающая конфигурацию: при `read_only=true` упадёт.

    Так защита работает по умолчанию: чтобы писать, нужно либо явно вызвать
    `execute_write()`, либо поставить `"read_only": false` в dsn.json.
    """
    with connection(read_only=None) as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params or ()))
        return cur.rowcount


def execute_write(sql: str, params: Sequence[Any] | None = None) -> int:
    """Запись в обход конфигурации. Вызывать осознанно."""
    with connection(read_only=False) as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params or ()))
        return cur.rowcount


@contextmanager
def transaction() -> Iterator[psycopg.Cursor]:
    """Одна транзакция на много запросов — планировщик пишет контракт целиком.

    При исключении psycopg откатит всё: частично записанного прогона не бывает.
    """
    with connection(read_only=False) as conn:
        with conn.cursor() as cur:
            yield cur


def health() -> dict[str, Any]:
    """Быстрая проверка живости базы — используется сервером и run.bat."""
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
