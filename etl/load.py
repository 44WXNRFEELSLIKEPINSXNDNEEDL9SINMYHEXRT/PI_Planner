#!/usr/bin/env python3
"""
ETL: Excel-лист ПочтаТеха -> нормализованное ядро (вариант B).

  python3 etl/load.py                      # -> build/seed.sql
  python3 etl/load.py --dsn postgresql://…  # + залить напрямую (нужен psycopg2)

Идемпотентен: seed.sql начинается с TRUNCATE, гонять можно сколько угодно.
Блоки на листе ищутся по маркерам из config.BLOCK_MARKERS, а не по номерам
строк — датасет уже приезжал «с правками», строки поедут снова.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ===================================================================== #
#  утилиты                                                              #
# ===================================================================== #
def norm_text(v) -> str:
    """Схлопнуть пробелы и неразрывные пробелы, обрезать края."""
    if v is None:
        return ""
    s = unicodedata.normalize("NFKC", str(v)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, str):
        v = v.replace(",", ".").strip()
        if not v:
            return 0.0
    return float(v)


def as_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(v).strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def sql(v) -> str:
    """Литерал для INSERT."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(round(v, 2) if isinstance(v, float) else v)
    if isinstance(v, (date, datetime)):
        return f"'{v.isoformat()[:10]}'"
    return "'" + str(v).replace("'", "''") + "'"


class DQ:
    """Журнал находок. Ничего не роняет — копит для dq_issues."""

    def __init__(self):
        self.rows: list[tuple] = []

    def add(self, entity, entity_id, rule_code, severity, detail):
        self.rows.append((entity, entity_id, rule_code, severity, detail))

    def count(self, severity=None):
        return sum(1 for r in self.rows if severity is None or r[3] == severity)


dq = DQ()


# ===================================================================== #
#  поиск блоков на листе                                                #
# ===================================================================== #
class Sheet:
    def __init__(self, ws):
        self.ws = ws
        self.max_row = ws.max_row
        self.max_col = ws.max_column

    def cell(self, r, c):
        return self.ws.cell(r, c).value

    def a(self, r) -> str:
        return norm_text(self.cell(r, 1))

    def find_block(self, marker: str) -> int:
        """Строка заголовка блока (та, где лежит маркер)."""
        m = norm_text(marker).lower()
        for r in range(1, self.max_row + 1):
            if self.a(r).lower().startswith(m):
                return r
        raise SystemExit(
            f"[ОШИБКА] Блок '{marker}' не найден на листе.\n"
            f"         Датасет изменился — поправь config.BLOCK_MARKERS."
        )

    def header_row(self, title_row: int) -> int:
        """Первая строка ниже заголовка, где колонка A не пуста."""
        for r in range(title_row + 1, self.max_row + 1):
            if self.a(r):
                return r
        raise SystemExit(f"[ОШИБКА] Не найдена шапка блока после строки {title_row}.")

    def data_rows(self, header_row: int, stop_prefixes=()):
        """Строки данных до первой пустой колонки A (или до стоп-префикса)."""
        stops = tuple(s.lower() for s in stop_prefixes)
        for r in range(header_row + 1, self.max_row + 1):
            v = self.a(r)
            if not v:
                return
            if stops and v.lower().startswith(stops):
                return
            yield r

    def columns(self, header_row: int) -> dict[str, int]:
        out = {}
        for c in range(1, self.max_col + 1):
            h = norm_text(self.cell(header_row, c))
            if h:
                out[h] = c
        return out

    def ref_list(self, title_row: int) -> list[str]:
        vals = []
        for r in range(title_row + 1, self.max_row + 1):
            v = self.a(r)
            if not v:
                break
            vals.append(v)
        return vals


# ===================================================================== #
#  справочники результатов                                              #
# ===================================================================== #
def build_ref(values, kind):
    """-> (rows, mapper). rows = [(code, ord, label)]"""
    rows, by_code = [], {}
    for i, label in enumerate(values, start=1):
        low = label.lower()
        if kind == "result":
            m = re.match(r"^(\d)\s*\.", label)
            if m:
                code = f"R{m.group(1)}"
            elif "не будет взято" in low:
                code = "NOT_IN_PI"
            elif low.startswith("отмен"):
                code = "CANCELLED"
                if len(label) < 20:
                    dq.add("ref_result_options", code, "TRUNCATED_REF_VALUE", "warning",
                           f"Значение справочника обрезано в исходнике: '{label}' "
                           f"(вероятно «Отменено заказчиком»).")
            else:
                code = f"X{i}"
        elif kind == "closure":
            if "не достигнут" in low:
                code = "NOT_ACHIEVED"
            elif "достигнут" in low:
                code = "ACHIEVED"
            elif "отмен" in low or "перенес" in low:
                code = "CANCELLED_BY_CUSTOMER"
            else:
                code = f"C{i}"
        else:
            code = f"M{i}"
        rows.append((code, i, label))
        by_code[code] = label

    def mapper(text):
        t = norm_text(text)
        if not t:
            return None
        low = t.lower()
        if kind == "result":
            m = re.match(r"^(\d)\s*\.", t)
            if m and f"R{m.group(1)}" in by_code:
                return f"R{m.group(1)}"
            if "не будет взято" in low:
                return "NOT_IN_PI"
            if low.startswith("отмен"):
                return "CANCELLED"
        elif kind == "closure":
            if "не достигнут" in low:
                return "NOT_ACHIEVED"
            if "достигнут" in low:
                return "ACHIEVED"
            if "отмен" in low or "перенес" in low:
                return "CANCELLED_BY_CUSTOMER"
        for code, label in by_code.items():
            if label.lower() == low:
                return code
        dq.add("tasks", None, "UNKNOWN_REF_VALUE", "warning",
               f"Значение '{t}' не сопоставлено со справочником ({kind}).")
        return None

    return rows, mapper


# ===================================================================== #
#  разбор                                                               #
# ===================================================================== #
def parse(path: Path):
    wb = openpyxl.load_workbook(path, data_only=True)
    sh = Sheet(wb.worksheets[0])
    D: dict = {}

    # ---------- справочники ----------
    D["ref_results"], map_result = build_ref(
        sh.ref_list(sh.find_block(C.BLOCK_MARKERS["ref_results"])), "result")
    D["ref_mismatch"], _ = build_ref(
        sh.ref_list(sh.find_block(C.BLOCK_MARKERS["ref_mismatch"])), "mismatch")
    D["ref_closure"], map_closure = build_ref(
        sh.ref_list(sh.find_block(C.BLOCK_MARKERS["ref_closure"])), "closure")

    # ---------- роли (канон = строки матрицы сметы) ----------
    est_title = sh.find_block(C.BLOCK_MARKERS["estimates"])
    est_hdr = sh.header_row(est_title)
    est_cols = sh.columns(est_hdr)          # task_id -> колонка
    est_cols.pop("Роль", None)

    canon_roles: list[str] = []
    role_rows: dict[str, list[int]] = {}    # каноническое имя -> строки матрицы
    for r in sh.data_rows(est_hdr, stop_prefixes=("Итого",)):
        raw = sh.a(r)
        canon = C.ROLE_ALIASES.get(raw, raw)
        if canon != raw:
            dq.add("roles", raw, "ROLE_ALIAS_MERGED", "warning",
                   f"Строка матрицы '{raw}' слита с канонической ролью '{canon}'.")
        if canon not in role_rows:
            role_rows[canon] = []
            canon_roles.append(canon)
        role_rows[canon].append(r)

    role_id = {name: i for i, name in enumerate(canon_roles, start=1)}
    D["roles"] = [(role_id[n], n, C.ROLE_GROUPS.get(n, "other")) for n in canon_roles]
    D["role_aliases"] = [(a, role_id[c]) for a, c in C.ROLE_ALIASES.items() if c in role_id]

    # ---------- задачи ----------
    t_title = sh.find_block(C.BLOCK_MARKERS["tasks"])
    t_hdr = sh.header_row(t_title)
    tc = sh.columns(t_hdr)

    def col(row, name):
        c = tc.get(name)
        return sh.cell(row, c) if c else None

    tasks, initiatives = [], {}
    for r in sh.data_rows(t_hdr):
        tid = norm_text(col(r, "task_id"))
        prodf = norm_text(col(r, "Номер инициативы"))
        br = norm_text(col(r, "parent_id"))
        summary = norm_text(col(r, "summary"))
        rung = int(num(col(r, "rung"))) or None

        ini = initiatives.setdefault(prodf, {"br": br, "titles": [], "rungs": [], "sps": []})
        if ini["br"] != br:
            dq.add("initiatives", prodf, "PRODF_BR_NOT_1TO1", "error",
                   f"У инициативы {prodf} два разных parent_id: {ini['br']} и {br}.")
        ini["titles"].append(summary)
        ini["rungs"].append(rung or 0)
        ini["sps"].append(num(col(r, "estimation_sp")))

        tasks.append({
            "task_id": tid, "prodf_id": prodf, "team_id": norm_text(col(r, "team_id")),
            "summary": summary, "status": norm_text(col(r, "status")), "rung": rung,
            "estimation_sp": int(num(col(r, "estimation_sp"))),
            "declared": num(col(r, "estimated_hh")),
            "spent_declared": (None if col(r, "spent_time") is None else num(col(r, "spent_time"))),
            "created_at": as_date(col(r, "created_at")),
            "planned_start": as_date(col(r, "planned_start")),
            "planned_end": as_date(col(r, "planned_end")),
            "actual_start": as_date(col(r, "actual_start")),
            "actual_end": as_date(col(r, "actual_end")),
            "result_planned": map_result(col(r, "плановый результат")),
            "result_customer": map_result(col(r, "заказчик")),
            "result_executor": map_result(col(r, "исполнитель")),
            "result_final": map_closure(col(r, "итоговый результат")),
            "committed_week0": norm_text(col(r, "будет включено в спринт")).lower() == "да",
        })
    task_ids = {t["task_id"] for t in tasks}

    # ---------- смета: матрица -> long ----------
    est_total_row = None
    for r in range(est_hdr + 1, sh.max_row + 1):
        if sh.a(r).lower().startswith("итого"):
            est_total_row = r
            break

    estimates, col_sum, matrix_total = [], {}, {}
    for tid, c in est_cols.items():
        if tid not in task_ids:
            dq.add("task_role_estimates", tid, "ESTIMATE_COLUMN_ORPHAN", "error",
                   f"В матрице сметы есть столбец '{tid}', которого нет в таблице Tasks.")
            continue
        total = 0.0
        for canon, rows in role_rows.items():
            h = sum(num(sh.cell(rr, c)) for rr in rows)
            if h > 0:
                estimates.append((tid, role_id[canon], h))
                total += h
        col_sum[tid] = total
        matrix_total[tid] = num(sh.cell(est_total_row, c)) if est_total_row else None
    for tid in task_ids - set(est_cols):
        dq.add("tasks", tid, "TASK_WITHOUT_ESTIMATE", "error", f"У задачи {tid} нет столбца в матрице сметы.")

    # ---------- факт по ролям ----------
    sp_title = sh.find_block(C.BLOCK_MARKERS["spent"])
    sp_hdr = sh.header_row(sp_title)
    sp_cols = sh.columns(sp_hdr)
    spent = []
    for r in sh.data_rows(sp_hdr):
        tid = norm_text(sh.cell(r, sp_cols["task_id"]))
        if tid not in task_ids:
            dq.add("task_role_spent", tid, "SPENT_ORPHAN", "error", f"Факт по ролям для неизвестной задачи {tid}.")
            continue
        for h, c in sp_cols.items():
            if h in ("task_id", "Исходное время"):
                continue
            canon = C.ROLE_ALIASES.get(h, h)
            if canon not in role_id:
                dq.add("task_role_spent", tid, "SPENT_UNKNOWN_ROLE", "warning",
                       f"Роль '{h}' из блока факта отсутствует в матрице сметы.")
                continue
            v = num(sh.cell(r, c))
            if v > 0:
                spent.append((tid, role_id[canon], v))

    # ---------- трудозатраты: выбор истины (ADR-002) ----------
    src = C.ESTIMATE_SOURCE
    for t in tasks:
        tid = t["task_id"]
        cand = {"matrix_column_sum": col_sum.get(tid, 0.0),
                "declared": t["declared"],
                "matrix_total": matrix_total.get(tid) or 0.0}
        eff = cand.get(src, 0.0)
        if not eff:
            eff = t["declared"]
            dq.add("tasks", tid, "ESTIMATE_SOURCE_FALLBACK", "warning",
                   f"Источник '{src}' дал 0 ЧЧ, взята declared-оценка {eff}.")
        vals = {k: round(v, 2) for k, v in cand.items() if v}
        if len(set(vals.values())) > 1:
            dq.add("tasks", tid, "ESTIMATE_SOURCES_DISAGREE", "warning",
                   "Расходятся оценки ЧЧ: " + ", ".join(f"{k}={v:g}" for k, v in vals.items()) +
                   f". Выбрано {src}={eff:g}.")
        t["effective"] = eff
        t["matrix_total"] = matrix_total.get(tid)

    # ---------- зависимости ----------
    d_title = sh.find_block(C.BLOCK_MARKERS["dependencies"])
    d_hdr = sh.header_row(d_title)
    dc = sh.columns(d_hdr)
    deps, seen = [], set()
    status = {t["task_id"]: t["status"] for t in tasks}
    for r in sh.data_rows(d_hdr):
        a = norm_text(sh.cell(r, dc["blocking_task_id"]))
        b = norm_text(sh.cell(r, dc["blocked_task_id"]))
        typ = norm_text(sh.cell(r, dc["dependency_type"]))
        if a not in task_ids or b not in task_ids:
            dq.add("task_dependencies", f"{a}->{b}", "DEP_ORPHAN", "error", "Связь ссылается на неизвестную задачу.")
            continue
        if a == b or (a, b) in seen:
            dq.add("task_dependencies", f"{a}->{b}", "DEP_DUPLICATE_OR_SELF", "error", "Петля или дубль связи.")
            continue
        seen.add((a, b))
        if status.get(a) != "Done" and status.get(b) == "Done":
            dq.add("task_dependencies", f"{a}->{b}", "DEP_VIOLATED_IN_SOURCE", "warning",
                   f"{b} уже Done, хотя блокирующая {a} в статусе {status.get(a)}.")
        deps.append((a, b, typ, C.MIN_GAP_SPRINTS))

    # ---------- инженеры ----------
    e_title = sh.find_block(C.BLOCK_MARKERS["engineers"])
    e_hdr = sh.header_row(e_title)
    ec = sh.columns(e_hdr)
    engineers, orbits, skills_seen, eng_skills = {}, [], {}, set()
    for r in sh.data_rows(e_hdr):
        eid = norm_text(sh.cell(r, ec["engineer_id"]))
        team = norm_text(sh.cell(r, ec["team_id"]))
        raw_role = norm_text(sh.cell(r, ec["role"]))
        canon = C.ROLE_ALIASES.get(raw_role, raw_role)
        if canon not in role_id:
            dq.add("engineers", eid, "ENGINEER_ROLE_UNKNOWN", "error",
                   f"Роль '{raw_role}' отсутствует в матрице сметы — добавь алиас в config.ROLE_ALIASES.")
            continue
        if canon != raw_role:
            dq.add("engineers", eid, "ROLE_ALIAS_APPLIED", "info", f"Роль '{raw_role}' -> '{canon}'.")
        grade = norm_text(sh.cell(r, ec["grade"]))
        rate = num(sh.cell(r, ec["capacity_rate"]))

        if eid in engineers:
            prev = engineers[eid]
            if (prev["role"], prev["grade"]) != (canon, grade):
                dq.add("engineers", eid, "PARTTIME_ATTRS_DIFFER", "error",
                       f"У парттаймера {eid} атрибуты различаются между орбитами — беру первую строку.")
            prev["total"] += rate
        else:
            engineers[eid] = {"role": canon, "grade": grade, "total": rate}
        orbits.append((eid, team, rate))

        for raw_skill in str(sh.cell(r, ec["skills_declared"]) or "").split(","):
            s = norm_text(raw_skill)
            if not s:
                continue
            key = s.lower()
            if key not in skills_seen:
                skills_seen[key] = (len(skills_seen) + 1, s)
            eng_skills.add((eid, skills_seen[key][0]))

    for eid, e in engineers.items():
        if round(e["total"], 2) > 1.0:
            dq.add("engineers", eid, "CAPACITY_OVER_100", "error",
                   f"Сумма ставок по орбитам = {e['total']} > 1.0.")

    # ---------- история команд ----------
    h_title = sh.find_block(C.BLOCK_MARKERS["team_history"])
    h_hdr = sh.header_row(h_title)
    hc = sh.columns(h_hdr)
    history, teams = [], set()
    for r in sh.data_rows(h_hdr):
        tm = norm_text(sh.cell(r, hc["team_id"]))
        teams.add(tm)
        history.append((tm, as_date(sh.cell(r, hc["snapshot_date"])),
                        num(sh.cell(r, hc["velocity_achieved"])), num(sh.cell(r, hc["planned_sp"]))))

    teams |= {t["team_id"] for t in tasks} | {o[1] for o in orbits}
    for tm in sorted(teams):
        if not any(h[0] == tm for h in history):
            dq.add("teams", tm, "TEAM_WITHOUT_HISTORY", "error",
                   f"У команды {tm} нет истории velocity — ёмкость в SP посчитать нельзя.")

    # ---------- инициативы ----------
    agg = C.RUNG_AGGREGATION
    ini_rows = []
    for prodf, v in initiatives.items():
        rungs = [x for x in v["rungs"] if x]
        if agg == "min":
            pr = min(rungs) if rungs else None
        elif agg == "sp_weighted" and sum(v["sps"]):
            pr = round(sum(r * s for r, s in zip(v["rungs"], v["sps"])) / sum(v["sps"]))
        else:
            pr = max(rungs) if rungs else None
        if len(set(rungs)) > 1:
            dq.add("initiatives", prodf, "RUNG_NOT_UNIFORM", "warning",
                   f"rung внутри инициативы неоднороден: {sorted(set(rungs))}. Свёрнут через '{agg}' -> {pr}.")
        title = max(set(v["titles"]), key=v["titles"].count) if v["titles"] else None
        ini_rows.append((prodf, v["br"], title, pr))

    D.update(tasks=tasks, initiatives=ini_rows, estimates=estimates, spent=spent,
             deps=deps, engineers=engineers, orbits=orbits,
             skills=[(i, s, k) for k, (i, s) in skills_seen.items()],
             eng_skills=sorted(eng_skills), history=history, teams=sorted(teams),
             role_id=role_id)
    return D


# ===================================================================== #
#  календарь и граф                                                     #
# ===================================================================== #
def build_sprints():
    rows = []
    for n in range(1, C.SPRINT_COUNT + 1):
        s = C.PI_START + timedelta(days=(n - 1) * C.SPRINT_LENGTH_DAYS)
        rows.append((C.PI_ID, n, s, s + timedelta(days=C.SPRINT_LENGTH_DAYS - 1)))
    pi_end = C.PI_START + timedelta(days=C.SPRINT_COUNT * C.SPRINT_LENGTH_DAYS - 1)
    return (C.PI_ID, C.PI_START, pi_end, C.SPRINT_COUNT, C.SPRINT_LENGTH_DAYS,
            C.HOURS_PER_SPRINT_FTE), rows


def build_sequence(tasks, deps):
    """Топологический порядок + самый ранний старт по живому подграфу."""
    status = {t["task_id"]: t["status"] for t in tasks}
    ids = [t["task_id"] for t in tasks]
    live = {i for i in ids if status[i] != "Done"}

    succ_all = {i: [] for i in ids}
    indeg_all = {i: 0 for i in ids}
    for a, b, *_ in deps:
        succ_all[a].append(b)
        indeg_all[b] += 1

    # Кан: топологический порядок по всему графу
    queue = sorted(i for i in ids if indeg_all[i] == 0)
    order, indeg = [], dict(indeg_all)
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in sorted(succ_all[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
        queue.sort()
    if len(order) != len(ids):
        cyc = sorted(set(ids) - set(order))
        dq.add("task_dependencies", None, "DEPENDENCY_CYCLE", "error",
               f"В графе зависимостей цикл, затронуты: {cyc}. Порядок посчитан частично.")
        order += cyc
    topo = {t: i + 1 for i, t in enumerate(order)}

    # живой подграф
    lsucc = {i: [] for i in live}
    lpred = {i: [] for i in live}
    for a, b, *_ in deps:
        if a in live and b in live:
            lsucc[a].append(b)
            lpred[b].append(a)

    gap = C.MIN_GAP_SPRINTS
    est, height = {}, {}
    for n in order:
        if n in live:
            est[n] = 1 if not lpred[n] else max(est[p] + gap for p in lpred[n])
    for n in reversed(order):
        if n in live:
            height[n] = 0 if not lsucc[n] else max(height[s] + gap for s in lsucc[n])

    longest = max((est[n] + height[n] for n in live), default=0)
    rows = []
    for t in ids:
        if t in live:
            rows.append((t, topo[t], height[t] // gap if gap else 0, est[t],
                         est[t] + height[t] == longest and longest > 1))
        else:
            rows.append((t, topo[t], 0, None, False))

    over = [t for t in live if est[t] > C.SPRINT_COUNT]
    if over:
        dq.add("task_sequence", None, "DEPENDENCY_PUSHES_OUT_OF_PI", "warning",
               f"Зависимости выталкивают за {C.SPRINT_COUNT} спринтов: {sorted(over)}.")
    return rows


# ===================================================================== #
#  генерация seed.sql                                                   #
# ===================================================================== #
def emit(D, src_path: Path) -> str:
    pi, sprints = build_sprints()
    seq = build_sequence(D["tasks"], D["deps"])
    sha = hashlib.sha256(src_path.read_bytes()).hexdigest()

    counts = {
        "roles": len(D["roles"]), "skills": len(D["skills"]), "teams": len(D["teams"]),
        "engineers": len(D["engineers"]), "engineer_orbits": len(D["orbits"]),
        "engineer_skills": len(D["eng_skills"]), "initiatives": len(D["initiatives"]),
        "tasks": len(D["tasks"]), "task_role_estimates": len(D["estimates"]),
        "task_role_spent": len(D["spent"]), "task_dependencies": len(D["deps"]),
        "team_history": len(D["history"]), "sprints": len(sprints), "dq_issues": len(dq.rows),
    }

    o: list[str] = []
    w = o.append
    w("-- СГЕНЕРИРОВАНО etl/load.py — РУКАМИ НЕ ПРАВИТЬ.")
    w(f"-- Источник: {src_path.name}")
    w(f"-- sha256:   {sha}")
    w(f"-- ETL:      v{C.ETL_VERSION}   PI_START={C.PI_START}   оценка={C.ESTIMATE_SOURCE}")
    w("BEGIN;")
    w("TRUNCATE kpi_snapshots, alerts, task_state, plan_assignments, plan_task_schedule,")
    w("         plan_baseline, plan_runs, dq_issues, task_sequence, sprints, pi_periods,")
    w("         team_history, task_dependencies, task_role_spent, task_role_estimates,")
    w("         tasks, initiatives, engineer_skills, engineer_orbits, engineers, teams,")
    w("         ref_closure_results, ref_mismatch_reasons, ref_result_options,")
    w("         skills, role_aliases, roles, load_batches RESTART IDENTITY CASCADE;")

    def block(title, table, cols, rows):
        w(f"\n-- {title}: {len(rows)}")
        if not rows:
            return
        w(f"INSERT INTO {table} ({', '.join(cols)}) VALUES")
        w(",\n".join("  (" + ", ".join(sql(v) for v in r) + ")" for r in rows) + ";")

    import json
    block("прогон ETL", "load_batches",
          ["batch_id", "source_file", "source_sha256", "etl_version", "pi_start", "row_counts"],
          [(1, src_path.name, sha, C.ETL_VERSION, C.PI_START, json.dumps(counts, ensure_ascii=False))])

    block("роли", "roles", ["role_id", "canonical_name", "role_group"], D["roles"])
    block("алиасы ролей", "role_aliases", ["alias", "role_id"], D["role_aliases"])
    block("навыки", "skills", ["skill_id", "name", "normalized_name"], D["skills"])
    block("справочник результатов", "ref_result_options", ["code", "ord", "label"], D["ref_results"])
    block("причины расхождений", "ref_mismatch_reasons", ["code", "ord", "label"], D["ref_mismatch"])
    block("результаты закрытия", "ref_closure_results", ["code", "ord", "label"], D["ref_closure"])
    block("команды (ядра)", "teams", ["team_id", "focus_factor"],
          [(t, C.FOCUS_FACTOR) for t in D["teams"]])
    block("инженеры (спутники)", "engineers",
          ["engineer_id", "role_id", "grade", "total_capacity_rate"],
          [(e, D["role_id"][v["role"]], v["grade"], round(v["total"], 2))
           for e, v in sorted(D["engineers"].items())])
    block("орбиты", "engineer_orbits", ["engineer_id", "team_id", "capacity_rate"], sorted(D["orbits"]))
    block("стек инженеров", "engineer_skills", ["engineer_id", "skill_id"], D["eng_skills"])
    block("инициативы", "initiatives", ["prodf_id", "br_id", "title", "priority_rung"],
          sorted(D["initiatives"]))
    block("задачи", "tasks",
          ["task_id", "prodf_id", "team_id", "summary", "status", "rung", "estimation_sp",
           "estimated_hh_effective", "estimated_hh_declared", "estimated_hh_matrix_total",
           "spent_time_declared", "created_at", "planned_start", "planned_end",
           "actual_start", "actual_end", "result_planned", "result_customer",
           "result_executor", "result_final", "committed_week0"],
          [(t["task_id"], t["prodf_id"], t["team_id"], t["summary"], t["status"], t["rung"],
            t["estimation_sp"], t["effective"], t["declared"], t["matrix_total"],
            t["spent_declared"], t["created_at"], t["planned_start"], t["planned_end"],
            t["actual_start"], t["actual_end"], t["result_planned"], t["result_customer"],
            t["result_executor"], t["result_final"], t["committed_week0"]) for t in D["tasks"]])
    block("смета по ролям", "task_role_estimates", ["task_id", "role_id", "hours"], D["estimates"])
    block("факт по ролям", "task_role_spent", ["task_id", "role_id", "hours"], D["spent"])
    block("зависимости", "task_dependencies",
          ["blocking_task_id", "blocked_task_id", "raw_type", "min_gap_sprints"], D["deps"])
    block("история команд", "team_history",
          ["team_id", "snapshot_date", "velocity_achieved", "planned_sp"], D["history"])
    block("период PI", "pi_periods",
          ["pi_id", "start_date", "end_date", "sprint_count", "sprint_length_days",
           "fte_hours_per_sprint"], [pi])
    block("спринты", "sprints", ["pi_id", "sprint_no", "start_date", "end_date"], sprints)
    block("порядок задач", "task_sequence",
          ["task_id", "topo_order", "depth", "earliest_start_sprint", "on_critical_path"], seq)
    block("качество данных", "dq_issues",
          ["batch_id", "entity", "entity_id", "rule_code", "severity", "detail"],
          [(1, *r) for r in dq.rows])

    w("\n-- синхронизация счётчиков")
    for tbl, col in (("roles", "role_id"), ("skills", "skill_id"),
                     ("load_batches", "batch_id"), ("dq_issues", "issue_id")):
        w(f"SELECT setval(pg_get_serial_sequence('{tbl}','{col}'), "
          f"COALESCE((SELECT MAX({col}) FROM {tbl}), 1), true);")
    w("COMMIT;")
    return "\n".join(o) + "\n", counts


# ===================================================================== #
def main():
    ap = argparse.ArgumentParser(description="ETL датасета ПочтаТеха в нормализованное ядро.")
    ap.add_argument("--src", default=str(ROOT / C.SOURCE_XLSX))
    ap.add_argument("--out", default=str(ROOT / C.OUTPUT_SQL))
    ap.add_argument("--dsn", help="строка подключения PostgreSQL; без неё только генерируется SQL")
    a = ap.parse_args()

    src = Path(a.src)
    if not src.exists():
        raise SystemExit(f"[ОШИБКА] Не найден исходник: {src}")

    D = parse(src)
    text, counts = emit(D, src)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    print(f"✓ {out.relative_to(ROOT)}  ({len(text.splitlines())} строк SQL)")
    print("  строк по таблицам: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"  качество данных: {dq.count('error')} error, "
          f"{dq.count('warning')} warning, {dq.count('info')} info")
    if dq.count("error"):
        print("  ВНИМАНИЕ: есть error-находки, смотри таблицу dq_issues после загрузки.")

    if a.dsn:
        try:
            import psycopg2
        except ImportError:
            raise SystemExit("[ОШИБКА] Нужен psycopg2: pip install psycopg2-binary")
        with psycopg2.connect(a.dsn) as conn, conn.cursor() as cur:
            cur.execute(text)
        print(f"✓ залито в {a.dsn.split('@')[-1]}")


if __name__ == "__main__":
    main()
