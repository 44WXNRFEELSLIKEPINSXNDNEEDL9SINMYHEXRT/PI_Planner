#!/usr/bin/env python3
"""Заливка SQL-файлов без psql: клиента PostgreSQL на машине может не быть.

    python tools/apply_sql.py --wait 60                 # дождаться базы
    python tools/apply_sql.py db/01_schema.sql ...      # залить файлы по порядку
    python tools/apply_sql.py --check                   # есть ли уже схема

Файлы выполняются целиком, как это делает `psql -f`: их собственные
BEGIN/COMMIT работают, потому что соединение в autocommit. Ошибка в любом
файле останавливает заливку — полупустая база хуже пустой.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402


def _dsn() -> str:
    """Рабочая строка подключения. `db.dsn()` — версия БЕЗ пароля, она для логов."""
    return db.load_config()["dsn"]


def wait_for_db(seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(_dsn(), connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception as exc:  # noqa: BLE001 — ждём любую недоступность
            last = exc
            time.sleep(1)
    print(f"[sql] база не ответила за {seconds} с: {last}", file=sys.stderr)
    return False


def schema_exists() -> bool:
    try:
        with psycopg.connect(_dsn(), connect_timeout=3) as conn:
            row = conn.execute("SELECT to_regclass('public.tasks') IS NOT NULL").fetchone()
        return bool(row and row[0])
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Залить SQL-файлы в базу проекта.")
    parser.add_argument("files", nargs="*", help="пути к .sql в порядке применения")
    parser.add_argument("--wait", type=int, default=0, help="сколько секунд ждать базу")
    parser.add_argument("--check", action="store_true", help="0 — схема есть, 1 — нет")
    args = parser.parse_args(argv)

    if args.wait and not wait_for_db(args.wait):
        return 2
    if args.check:
        return 0 if schema_exists() else 1

    for name in args.files:
        path = Path(name)
        if not path.exists():
            print(f"[sql] нет файла: {path}", file=sys.stderr)
            return 2
        try:
            with psycopg.connect(_dsn(), autocommit=True) as conn:
                conn.execute(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — сообщение важнее трассировки
            print(f"[sql] ✗ {path}: {exc}", file=sys.stderr)
            return 1
        print(f"[sql] ✓ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
