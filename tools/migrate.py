"""Apply checked, transactional PostgreSQL migrations.

Run in deployment after the database is healthy:
    docker compose run --rm migrate

The initial schema and seed remain Docker-init files for a fresh demo volume.
All changes made after that bootstrap belong in db/migrations/NNNN_description.sql.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "db" / "migrations"
MIGRATION_NAME = re.compile(r"^(\d{4}_[a-z0-9_]+)\.sql$")
LOCK_KEY = 5_427_001


def discover() -> list[tuple[str, Path]]:
    migrations: list[tuple[str, Path]] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise RuntimeError(f"invalid migration name: {path.name}")
        migrations.append((match.group(1), path))
    if not migrations:
        raise RuntimeError(f"no migrations found in {MIGRATIONS}")
    return migrations


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    dsn = os.environ.get("PI_PLANNER_DSN", "").strip()
    if not dsn:
        raise RuntimeError("PI_PLANNER_DSN is required for migrations")

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
            try:
                # Bootstrap permits adoption of the current Docker-init database.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        checksum TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                for version, path in discover():
                    digest = checksum(path)
                    cur.execute(
                        "SELECT checksum FROM schema_migrations WHERE version = %s", (version,)
                    )
                    applied = cur.fetchone()
                    if applied:
                        if applied[0] != digest:
                            raise RuntimeError(
                                f"migration {version} was changed after application; "
                                "create a new migration instead"
                            )
                        print(f"[migrate] already applied: {version}")
                        continue

                    sql = path.read_text(encoding="utf-8")
                    with conn.transaction():
                        cur.execute(sql)
                        cur.execute(
                            "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                            (version, digest),
                        )
                    print(f"[migrate] applied: {version}")
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # deployment logs need one actionable error line
        print(f"[migrate] FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
