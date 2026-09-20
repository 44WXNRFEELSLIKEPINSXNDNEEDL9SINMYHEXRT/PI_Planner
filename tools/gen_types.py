"""Генератор TypeScript-типов из живой схемы PostgreSQL.

Зачем: экраны не хардкодят колонки ДС — типы приходят из той же базы, что и данные.
После любой правки схемы/вьюх:

    uv run python tools/gen_types.py

Результат — `web/src/types/db.ts`, файл коммитим: демо-машина собирает фронт
без доступа к базе (и вообще без Node, см. docs/RUNBOOK.md).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402  (путь добавляем выше — иначе импорт не найдётся)

DEFAULT_OUT = ROOT / "web" / "src" / "types" / "db.ts"

HEADER = """/**
 * СГЕНЕРИРОВАНО: tools/gen_types.py — руками не правим, правки затрёт перегенерация.
 * Источник: схема public живой базы PostgreSQL 17.
 * Перегенерация: uv run python tools/gen_types.py
 */

/** Значение json/jsonb из ДС. */
export type Json = string | number | boolean | null | Json[] | { [key: string]: Json }
"""

SCALAR_MAP: dict[str, str] = {
    "int2": "number",
    "int4": "number",
    "int8": "number",
    "float4": "number",
    "float8": "number",
    "numeric": "number",
    "money": "number",
    "bool": "boolean",
    "text": "string",
    "varchar": "string",
    "bpchar": "string",
    "name": "string",
    "citext": "string",
    "uuid": "string",
    "date": "string",
    "time": "string",
    "timetz": "string",
    "timestamp": "string",
    "timestamptz": "string",
    "interval": "string",
    "json": "Json",
    "jsonb": "Json",
}

RELATIONS_SQL = """
SELECT c.relname                AS relname,
       c.relkind                AS relkind,
       obj_description(c.oid)   AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.relname
"""

COLUMNS_SQL = """
SELECT table_name,
       column_name,
       udt_name,
       data_type,
       is_nullable,
       is_generated
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""

CHECKS_SQL = """
SELECT c.relname AS relname,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND con.contype = 'c'
ORDER BY c.relname
"""

RELKIND_LABEL = {"r": "таблица", "p": "таблица (partitioned)", "v": "вьюха", "m": "матвьюха"}

# `CHECK (((status)::text = ANY ((ARRAY['ToDo'::character varying, ...])::text[])))`
ANY_COL = re.compile(r"\(*([a-z_][a-z0-9_]*)\)*(?:::[a-z_ ]+)?\s*=\s*ANY\s*\(\s*ARRAY", re.I)
# `CHECK (status IN ('a'::text, 'b'::text))`
IN_COL = re.compile(r"\(*([a-z_][a-z0-9_]*)\)*(?:::[a-z_ ]+)?\s+IN\s*\(", re.I)
LITERAL = re.compile(r"'((?:[^']|'')*)'(?:::[^,)\]]+)?")


def enum_unions() -> dict[tuple[str, str], list[str]]:
    """Вытаскивает перечисления из CHECK-ов: колонка со списком допустимых значений.

    Так `status: 'ToDo' | 'InProgress' | 'Done'` приезжает в TS автоматически —
    и в UI не появляется опечаток в статусах, которые база всё равно отвергнет.
    """
    unions: dict[tuple[str, str], list[str]] = {}
    for row in db.query_dicts(CHECKS_SQL):
        definition: str = row["definition"] or ""
        match = ANY_COL.search(definition) or IN_COL.search(definition)
        if not match:
            continue
        values = [value.replace("''", "'") for value in LITERAL.findall(definition)]
        # Больше 40 значений — это уже не перечисление, а следствие хитрого CHECK.
        if 1 <= len(values) <= 40:
            unions[(row["relname"], match.group(1))] = values
    return unions


def ts_type(column: dict[str, str], union: list[str] | None) -> str:
    udt: str = column["udt_name"]
    if union:
        base = " | ".join(f"'{value}'" for value in union)
    elif column["data_type"] == "ARRAY" or udt.startswith("_"):
        base = f"{SCALAR_MAP.get(udt[1:], 'unknown')}[]"
    else:
        base = SCALAR_MAP.get(udt, "unknown")
    if column["is_nullable"] == "YES":
        base = f"{base} | null"
    return base


def render() -> tuple[str, list[str]]:
    relations = db.query_dicts(RELATIONS_SQL)
    columns = db.query_dicts(COLUMNS_SQL)
    unions = enum_unions()

    by_relation: dict[str, list[dict[str, str]]] = {}
    for column in columns:
        by_relation.setdefault(column["table_name"], []).append(column)

    unmapped: set[str] = set()
    chunks: list[str] = [HEADER]
    names: list[str] = []

    for relation in relations:
        name = relation["relname"]
        names.append(name)
        kind = RELKIND_LABEL.get(relation["relkind"], relation["relkind"])
        rel_columns = by_relation.get(name, [])
        title = f"{kind} public.{name} — {len(rel_columns)} кол."
        comment = relation["comment"]
        if comment:
            title = f"{title} · {comment.splitlines()[0]}"
        lines = [f"/** {title} */", f"export interface {name} {{"]
        for column in rel_columns:
            declared = ts_type(column, unions.get((name, column["column_name"])))
            if "unknown" in declared:
                unmapped.add(column["udt_name"])
            generated = "  // generated: значение считает база" if column["is_generated"] == "YES" else ""
            lines.append(f"  {column['column_name']}: {declared};{generated}")
        lines.append("}")
        chunks.append("\n".join(lines))

    relation_union = "\n".join(f"  | '{name}'" for name in names)
    chunks.append(f"/** Все отношения схемы public — таблицы и вьюхи. */\nexport type RelationName =\n{relation_union}")
    return "\n\n".join(chunks) + "\n", sorted(unmapped)


def main() -> int:
    parser = argparse.ArgumentParser(description="TS-типы из схемы pi_planner")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="куда писать файл типов")
    args = parser.parse_args()

    text, unmapped = render()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")

    print(f"dsn: {db.dsn()}")
    print(f"записано: {args.out}")
    print(f"строк: {text.count(chr(10))}, интерфейсов: {text.count('export interface ')}")
    if unmapped:
        print(f"ВНИМАНИЕ: не отображены типы {', '.join(unmapped)} — в TS попали как unknown", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

