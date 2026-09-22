"""Планировщик квартала: читает витрины ДС, строит план, пишет контракт.

Запуск — `tools/run_planner.py`, единственное место, откуда планировщик пишет
в базу:

    uv run python tools/run_planner.py --as-of-sprint 0

Модуль разделён на три части, и это осознанно:

* `load_inputs()` — чтение (сессия `app.db` строго read-only);
* `build_plan()` — ЧИСТАЯ функция: вход → `Plan`, ни одного обращения к базе,
  поэтому проверяется юнит-тестами без PostgreSQL (`tests/test_planner.py`);
* `write_plan()` — одна транзакция на весь контракт: частично записанного
  прогона не бывает.

Алгоритм `greedy-priority-topo@1` (ADR-011):

1. инициативы по `priority_rung` DESC, внутри инициативы — по `topo_order`;
2. для задачи ищется минимальный спринт, где хватает SP у команды и часов у
   исполнителей по каждой требуемой роли; часы разрешено растягивать на
   следующие спринты (`end_sprint > start_sprint`);
3. не влезла до последнего спринта — `deferred_next_pi` с причиной `M2`
   («Отсутствие ресурсов»), а если её держит перенесённая блокирующая задача
   чужой команды — `M3` («Отсутствует готовность смежных команд»).

Правила, которые алгоритм соблюдает по построению:

* часы — только `v_task_remaining_hh.remaining_hours` (остаток сметы по роли,
  ADR-002); расхождения трёх источников проверяются и уезжают в
  `plan_runs.params` — этого требует ответ организаторов №4;
* замещения ролей отклонены организаторами (ответ №2, ADR-010), поэтому
  исполнители берутся ТОЛЬКО из родных строк `v_engineer_role_coverage`, и
  КАНДИДАТЫ на роль — тоже из этой вьюхи, а не из `engineers.role_id`
  (ADR-012): вьюха остаётся единственным источником правды о паре
  «инженер × роль», включая `efficiency`;
* `efficiency` (множитель часов замещающего) применяется к потребности:
  чтобы закрыть `remaining_hours` сметы, исполнителю нужно
  `remaining_hours × efficiency` своих часов. Сейчас в данных везде `1.00`,
  поэтому поведение не меняется, но формула уже верна (ADR-016);
* фонд часов — по орбитам: сначала своё ядро, невыбранный остаток уходит в заём
  (`is_loan` считает СУБД, ADR-001). Одна строка `plan_assignments` берёт часы
  РОВНО С ОДНОЙ орбиты: `home_team_id` не входит в первичный ключ
  `(task_id, sprint_no, engineer_id, role_id)`, поэтому размазать одно
  назначение по двум орбитам контракт не позволяет (ADR-015);
* задача с нулевым остатком (работа фактически сделана) получает символическое
  назначение `0.01` ЧЧ: иначе `CHECK (hours > 0)` и инвариант
  `IN_QUARTER_WITHOUT_ASSIGNMENTS` несовместимы друг с другом;
* в закрытые спринты план не пишется: при `as_of_sprint = k` нижняя граница
  старта — `max(1, k, earliest_start_sprint)` (ADR-014).

Режимы (по умолчанию — как в приёмке M2, оба параметра уезжают в
`plan_runs.params`):

* `dependency_mode`: `start_start` (по умолчанию) —
  `start(blocked) ≥ start(blocking) + gap`, как в предпосчитанном
  `task_sequence.earliest_start_sprint`; `finish_start` —
  `start(blocked) ≥ end(blocking) + gap` (ADR-013);
* `initiative_mode`: `greedy` (по умолчанию) — задача решается по отдельности,
  частично закрытая инициатива допустима; `atomic` — пробная упаковка всей
  инициативы с откатом: не влезла хоть одна задача, переносится вся
  инициатива (ADR-013).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app import db

ALGORITHM = "greedy-priority-topo@2"
ESTIMATE_SOURCE = "matrix_column_sum"
SUBSTITUTION_MODE = "rejected"
# Минимальное назначение: контракт требует hours > 0, а остаток может быть нулевым.
SYMBOLIC_HOURS = Decimal("0.01")
DEFERRED_REASON = "M2"  # Отсутствие ресурсов
DEFERRED_REASON_BLOCKED = "M3"  # Отсутствует готовность смежных команд
DONE_STATUS = "Done"

# Причины решений (ADR-022): код из ref_decision_reasons + старый код расхождений
# (M2/M3/M4) для совместимости контракта. Текст для задачи собирается отдельно.
REASON_PLANNED = "PLANNED"
REASON_ROLE_NOT_IN_STAFF = "ROLE_NOT_IN_STAFF"
REASON_ROLE_HOURS = "ROLE_HOURS_EXHAUSTED"
REASON_TEAM_SP = "TEAM_SP_EXHAUSTED"
REASON_BLOCKED = "BLOCKED_BY_DEFERRED"
REASON_ATOMIC = "INITIATIVE_ATOMIC"
REASON_PI_CLOSED = "PI_CLOSED"
REASON_NOT_FEASIBLE = "NOT_FEASIBLE_NEXT_PI"
CANCEL_REASON = "M4"  # «Превышение плана»: не помещается и в следующий квартал

# Семантика зависимостей (ADR-013). `start_start` — значение по умолчанию:
# именно её реализует предпосчитанный `task_sequence.earliest_start_sprint`.
DEPENDENCY_MODE_START_START = "start_start"
DEPENDENCY_MODE_FINISH_START = "finish_start"
DEPENDENCY_MODES = (DEPENDENCY_MODE_START_START, DEPENDENCY_MODE_FINISH_START)

# Атомарность инициатив (ADR-013). `greedy` — частичная инициатива допустима.
INITIATIVE_MODE_GREEDY = "greedy"
INITIATIVE_MODE_ATOMIC = "atomic"
INITIATIVE_MODES = (INITIATIVE_MODE_GREEDY, INITIATIVE_MODE_ATOMIC)

# Целевая функция (ADR-015): лексикографическая, без перестановок.
OBJECTIVE = "lexicographic: initiatives.priority_rung DESC, task_sequence.topo_order ASC, start_sprint ASC"
OBJECTIVE_NOTE = (
    "жадный обход без перестановок: deferred_next_pi значит «не влезло при уже "
    "принятых назначениях», а не «невыполнимо в принципе». Счётчики "
    "initiatives_complete/initiatives_partial в params показывают цену этого выбора"
)

# `plan_assignments.hours` — часы ИСПОЛНИТЕЛЯ, а не эквивалент работы (ADR-016).
EFFICIENCY_NOTE = (
    "hours = человеко-часы исполнителя: смету remaining_hours закрывают "
    "remaining_hours × v_engineer_role_coverage.efficiency часов (сейчас везде 1.00)"
)

KPI_TARGETS: dict[str, tuple[Decimal | None, Decimal | None]] = {
    "pi_predictability": (Decimal("80"), Decimal("100")),
    "say_do_ratio": (Decimal("90"), Decimal("105")),
    "bus_factor": (Decimal("2"), None),  # онбординг: «Bus Factor > 1»
}

# ---------------------------------------------------------------------------
#  Запросы на чтение. Все — к витринам: планировщик не знает ядро изнутри.
# ---------------------------------------------------------------------------
PI_SQL = """
SELECT p.pi_id, p.start_date, p.end_date, p.sprint_count, p.fte_hours_per_sprint,
       COALESCE(f.days_total, p.sprint_count * p.sprint_length_days) AS pi_days,
       COALESCE(f.factor, p.sprint_count)                            AS fund_factor
FROM pi_periods p
LEFT JOIN v_pi_fund_factor f ON f.pi_id = p.pi_id
ORDER BY p.pi_id
LIMIT 1
"""

# factor спринта — из вьюхи, а не «из головы»: короткий 7-й спринт даёт
# 0.5714 фонда, и планировщик обязан считать так же, как инварианты.
SPRINTS_SQL = """
SELECT s.sprint_no, s.start_date, s.end_date, s.length_days, f.factor
FROM sprints s
JOIN v_sprint_fund_factor f ON f.pi_id = s.pi_id AND f.sprint_no = s.sprint_no
WHERE s.pi_id = %s
ORDER BY s.sprint_no
"""

# Порядок обхода — правило из спеки: инициатива по скорингу, задача по топологии.
LIVE_TASKS_SQL = """
SELECT b.task_id, b.prodf_id, b.team_id, b.status, b.priority_rung,
       COALESCE(b.estimation_sp, 0)          AS estimation_sp,
       b.summary,
       COALESCE(b.earliest_start_sprint, 1)  AS earliest_start_sprint,
       COALESCE(b.topo_order, 0)             AS topo_order,
       b.estimate_disputed
FROM v_task_board b
WHERE b.status IN ('ToDo', 'InProgress')
ORDER BY b.priority_rung DESC NULLS LAST, b.topo_order, b.task_id
"""

TASK_ROLES_SQL = """
SELECT rm.task_id, rm.role_id, r.canonical_name AS role_name,
       rm.estimated_hours, rm.spent_hours, rm.remaining_hours
FROM v_task_remaining_hh rm
JOIN roles r ON r.role_id = rm.role_id
JOIN tasks t ON t.task_id = rm.task_id
WHERE t.status IN ('ToDo', 'InProgress')
ORDER BY rm.task_id, rm.role_id
"""

# Единственный источник правды о паре «инженер × роль» (ADR-012): и кандидаты
# на роль, и множитель часов берутся отсюда. Строгий режим — замещения
# отклонены организаторами (ответ №2, ADR-010), поэтому только родные строки.
COVERAGE_SQL = """
SELECT c.engineer_id, c.role_id, r.canonical_name AS role_name, c.is_native, c.efficiency
FROM v_engineer_role_coverage c
JOIN roles r ON r.role_id = c.role_id
WHERE c.is_native
ORDER BY c.role_id, c.engineer_id
"""

ENGINEERS_SQL = """
SELECT e.engineer_id, e.role_id, e.grade, e.total_capacity_rate,
       o.team_id, o.capacity_rate
FROM engineers e
JOIN engineer_orbits o ON o.engineer_id = e.engineer_id
ORDER BY e.engineer_id, o.team_id
"""

TEAM_CAPACITY_SQL = """
SELECT team_id, available_sp_per_sprint
FROM v_team_capacity_sp
ORDER BY team_id
"""

# Только живые рёбра: зазоры на задачах Done уже учтены в earliest_start_sprint.
LIVE_DEPS_SQL = """
SELECT d.blocking_task_id, d.blocked_task_id, d.min_gap_sprints
FROM task_dependencies d
JOIN tasks bt ON bt.task_id = d.blocking_task_id
JOIN tasks kt ON kt.task_id = d.blocked_task_id
WHERE bt.status IN ('ToDo', 'InProgress')
  AND kt.status IN ('ToDo', 'InProgress')
ORDER BY d.blocking_task_id, d.blocked_task_id
"""

# Слепок task_state делается по ВСЕМ задачам, включая Done: это история.
ALL_TASKS_SQL = """
SELECT t.task_id, t.status, COALESCE(t.estimation_sp, 0) AS estimation_sp,
       COALESCE(SUM(rm.remaining_hours), 0)              AS remaining_hh
FROM tasks t
LEFT JOIN v_task_remaining_hh rm ON rm.task_id = t.task_id
GROUP BY t.task_id, t.status, t.estimation_sp
ORDER BY t.task_id
"""

BUS_FACTOR_SQL = """
SELECT role_name, bus_factor, demand_hh
FROM v_bus_factor
WHERE demand_hh > 0
ORDER BY bus_factor, role_name
"""

# Bus Factor по компетенциям (ТЗ, ADR-024): навык, носителей, роль нужна бэклогу,
# единственный носитель и единственный по роли.
SKILL_BUS_FACTOR_SQL = """
SELECT skill_name, bus_factor, in_demand, sole_in_role
FROM v_bus_factor_skill
ORDER BY bus_factor, skill_name
"""

# Факт спринтов (ADR-021): последняя загрузка и какие задачи в каком спринте
# ВПЕРВЫЕ отмечены выполненными — это числитель «Выполнения плана спринта».
LAST_UPLOAD_SQL = """
SELECT upload_id, sprint_no FROM actual_uploads
WHERE pi_id = %s ORDER BY sprint_no DESC LIMIT 1
"""
DONE_IN_SPRINT_SQL = """
SELECT u.sprint_no, a.task_id
FROM task_actuals a
JOIN actual_uploads u   ON u.upload_id = a.upload_id
JOIN tasks_seed_state s ON s.task_id = a.task_id
WHERE a.status = 'Done' AND s.status <> 'Done' AND u.pi_id = %s
  AND NOT EXISTS (SELECT 1 FROM task_actuals a2
                  JOIN actual_uploads u2 ON u2.upload_id = a2.upload_id
                  WHERE a2.task_id = a.task_id AND a2.status = 'Done'
                    AND u2.sprint_no < u.sprint_no)
ORDER BY u.sprint_no, a.task_id
"""
TASK_PRODF_SQL = "SELECT task_id, prodf_id FROM tasks ORDER BY task_id"

# Первоначальный план = канонический базовый прогон целиком: решение, старт и
# конец каждой задачи Недели 0. Пересчёт базу сравнения не меняет (ТЗ).
BASELINE_SCHEDULE_SQL = """
SELECT b.task_id, s.decision, s.start_sprint, s.end_sprint
FROM plan_baseline b
JOIN plan_task_schedule s ON s.run_id = b.run_id AND s.task_id = b.task_id
WHERE b.run_id = (SELECT MIN(run_id) FROM plan_runs WHERE as_of_sprint = 0 AND status = 'ok')
ORDER BY b.task_id
"""

# Проверка ответа №4: три источника часов расходятся — сколько раз и насколько.
ESTIMATE_CONFLICT_SQL = """
SELECT COUNT(*)                                     AS issues,
       COUNT(*) FILTER (WHERE severity = 'warning') AS warnings
FROM dq_issues
WHERE rule_code = 'ESTIMATE_SOURCES_DISAGREE'
"""

SUBSTITUTION_ROWS_SQL = """
SELECT COUNT(*) AS active FROM role_substitutions WHERE status <> 'rejected'
"""

# Расписание ПЕРВОГО базового прогона (as_of_sprint = 0): это и есть обещание
# Недели 0, дальше оно не меняется (ADR-004). MIN, а не MAX: обещание фиксирует
# первый прогон, пересчёты на него не влияют.
BASELINE_STARTS_SQL = """
SELECT DISTINCT ON (s.task_id) s.task_id, s.start_sprint
FROM plan_task_schedule s
JOIN plan_runs r ON r.run_id = s.run_id
WHERE r.as_of_sprint = 0
  AND r.status = 'ok'
  AND r.run_id = (SELECT MIN(run_id) FROM plan_runs WHERE as_of_sprint = 0 AND status = 'ok')
  AND s.start_sprint IS NOT NULL
ORDER BY s.task_id
"""


# ---------------------------------------------------------------------------
#  ВХОД: то же, что планировщик прочитал, но уже разложенное по смыслу
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TaskInput:
    """Живая задача: что просит по ролям и в каких рамках может стартовать."""

    task_id: str
    prodf_id: str
    team_id: str
    status: str
    priority_rung: int | None
    estimation_sp: Decimal
    summary: str | None
    earliest_start_sprint: int
    topo_order: int
    remaining: dict[int, Decimal]  # role_id -> ЧЧ; может быть 0
    role_names: dict[int, str]
    estimate_disputed: bool

    @property
    def needed(self) -> dict[int, Decimal]:
        """Роли, по которым реально остались часы (нулевые не планируем)."""
        return {role_id: hours for role_id, hours in self.remaining.items() if hours > 0}

    @property
    def demand_hh(self) -> Decimal:
        return sum(self.remaining.values(), Decimal("0"))


@dataclass(frozen=True)
class EngineerInput:
    engineer_id: str
    role_id: int
    grade: str
    total_capacity_rate: Decimal
    orbits: dict[str, Decimal]  # team_id -> ставка на орбите


@dataclass(frozen=True)
class Inputs:
    pi_id: str
    sprint_count: int
    fte_hours_per_sprint: int
    # Календарь: PI — это КАЛЕНДАРНЫЙ квартал, а не «N × 14 дней» (ADR-017).
    # `fund_factor` — сколько полных спринтов в квартале (92/14 = 6.5714),
    # `sprint_factors` — множитель фонда по каждому спринту (7-й = 0.5714).
    # Фонд ставки за PI = fte_hours_per_sprint × fund_factor = 525.71 ЧЧ.
    fund_factor: Decimal
    pi_days: int
    sprint_factors: dict[int, Decimal]
    team_sp_per_sprint: dict[str, Decimal]
    tasks: tuple[TaskInput, ...]
    engineers: tuple[EngineerInput, ...]
    coverage: dict[tuple[str, int], Decimal]  # (engineer_id, role_id) -> efficiency
    deps: tuple[tuple[str, str, int], ...]  # (blocking, blocked, min_gap)
    sprints: dict[int, tuple[date, date]]
    all_tasks: tuple[tuple[str, str, Decimal, Decimal], ...]  # id, статус, SP, остаток ЧЧ
    bus_factor: tuple[tuple[str, int, Decimal], ...]  # роль, BF, спрос ЧЧ
    estimate_conflicts: int
    estimate_conflict_warnings: int
    active_substitutions: int
    # --- факт спринтов и первоначальный план (ADR-021, ADR-023) ---------
    actuals_upload_id: int | None = None
    last_reported_sprint: int = 0  # последний спринт, по которому загружен факт
    done_in_sprint: dict[int, frozenset[str]] = field(default_factory=dict)
    task_prodf: dict[str, str] = field(default_factory=dict)  # все задачи, и Done тоже
    # task_id -> (decision, start, end) канонического базового прогона
    baseline_schedule: dict[str, tuple[str, int | None, int | None]] = field(default_factory=dict)
    # Bus Factor по компетенциям: (навык, носителей, in_demand, sole_in_role)
    skill_bus_factor: tuple[tuple[str, int, bool, bool], ...] = ()

    @property
    def fund_hours_per_fte(self) -> Decimal:
        """Фонд одной ставки за весь PI в ЧЧ: 80 × 6.5714 = 525.71."""
        return (self.fund_factor * Decimal(self.fte_hours_per_sprint)).quantize(
            Decimal("0.01")
        )

    def short_sprints_note(self) -> str:
        """Короткие спринты человеческим языком — для логов прогона и UI."""
        short = {no: f for no, f in sorted(self.sprint_factors.items()) if f < 1}
        if not short:
            return "нет"
        return ", ".join(f"спринт {no} — ×{factor}" for no, factor in short.items())


def load_inputs() -> Inputs:
    """Читает всё, что нужно для плана. Только SELECT: сессия read-only."""
    pi = db.query_one(PI_SQL)
    if not pi:
        raise RuntimeError("pi_periods пуст: сначала залейте схему, seed и витрины (docs/RUNBOOK.md)")

    # Календарь читаем один раз: из него и границы спринтов, и множители фонда.
    sprint_rows = db.query_dicts(SPRINTS_SQL, (pi["pi_id"],))
    if len(sprint_rows) != int(pi["sprint_count"]):
        raise RuntimeError(
            f"календарь PI разъехался: sprints содержит {len(sprint_rows)} строк, "
            f"а pi_periods.sprint_count = {pi['sprint_count']} (docs/RUNBOOK.md, раздел 7)"
        )

    roles_by_task: dict[str, dict[int, Decimal]] = defaultdict(dict)
    names_by_task: dict[str, dict[int, str]] = defaultdict(dict)
    for row in db.query_dicts(TASK_ROLES_SQL):
        roles_by_task[row["task_id"]][row["role_id"]] = Decimal(row["remaining_hours"])
        names_by_task[row["task_id"]][row["role_id"]] = row["role_name"]

    tasks = tuple(
        TaskInput(
            task_id=row["task_id"],
            prodf_id=row["prodf_id"],
            team_id=row["team_id"],
            status=row["status"],
            priority_rung=row["priority_rung"],
            estimation_sp=Decimal(row["estimation_sp"]),
            summary=row["summary"],
            earliest_start_sprint=int(row["earliest_start_sprint"]),
            topo_order=int(row["topo_order"]),
            remaining=dict(roles_by_task.get(row["task_id"], {})),
            role_names=dict(names_by_task.get(row["task_id"], {})),
            estimate_disputed=bool(row["estimate_disputed"]),
        )
        for row in db.query_dicts(LIVE_TASKS_SQL)
    )

    engineers: dict[str, dict[str, Any]] = {}
    for row in db.query_dicts(ENGINEERS_SQL):
        item = engineers.setdefault(
            row["engineer_id"],
            {
                "role_id": row["role_id"],
                "grade": row["grade"],
                "total_capacity_rate": Decimal(row["total_capacity_rate"]),
                "orbits": {},
            },
        )
        item["orbits"][row["team_id"]] = Decimal(row["capacity_rate"])

    # Кандидаты на роль и множитель часов — из вьюхи покрытия (ADR-012).
    coverage: dict[tuple[str, int], Decimal] = {}
    for row in db.query_dicts(COVERAGE_SQL):
        coverage[(row["engineer_id"], row["role_id"])] = Decimal(row["efficiency"])
    # Страховка: инженера нет в вьюхе — свою родную роль он всё равно закрывает.
    # Иначе человек молча выпал бы из плана, а инварианты этого не заметили бы.
    for engineer_id, item in engineers.items():
        coverage.setdefault((engineer_id, item["role_id"]), Decimal("1"))

    conflicts = db.query_one(ESTIMATE_CONFLICT_SQL) or {}
    substitutions = db.query_one(SUBSTITUTION_ROWS_SQL) or {}

    last_upload = db.query_one(LAST_UPLOAD_SQL, (pi["pi_id"],)) or {}
    done_in_sprint: dict[int, set[str]] = defaultdict(set)
    for row in db.query_dicts(DONE_IN_SPRINT_SQL, (pi["pi_id"],)):
        done_in_sprint[int(row["sprint_no"])].add(row["task_id"])

    return Inputs(
        pi_id=pi["pi_id"],
        sprint_count=int(pi["sprint_count"]),
        fte_hours_per_sprint=int(pi["fte_hours_per_sprint"]),
        fund_factor=Decimal(pi["fund_factor"]),
        pi_days=int(pi["pi_days"]),
        sprint_factors={
            int(row["sprint_no"]): Decimal(row["factor"]) for row in sprint_rows
        },
        team_sp_per_sprint={
            row["team_id"]: Decimal(row["available_sp_per_sprint"])
            for row in db.query_dicts(TEAM_CAPACITY_SQL)
        },
        tasks=tasks,
        engineers=tuple(
            EngineerInput(
                engineer_id=engineer_id,
                role_id=item["role_id"],
                grade=item["grade"],
                total_capacity_rate=item["total_capacity_rate"],
                orbits=item["orbits"],
            )
            for engineer_id, item in sorted(engineers.items())
        ),
        coverage=coverage,
        deps=tuple(
            (row["blocking_task_id"], row["blocked_task_id"], int(row["min_gap_sprints"]))
            for row in db.query_dicts(LIVE_DEPS_SQL)
        ),
        sprints={
            row["sprint_no"]: (row["start_date"], row["end_date"]) for row in sprint_rows
        },
        all_tasks=tuple(
            (
                row["task_id"],
                row["status"],
                Decimal(row["estimation_sp"]),
                Decimal(row["remaining_hh"]),
            )
            for row in db.query_dicts(ALL_TASKS_SQL)
        ),
        bus_factor=tuple(
            (row["role_name"], int(row["bus_factor"]), Decimal(row["demand_hh"]))
            for row in db.query_dicts(BUS_FACTOR_SQL)
        ),
        estimate_conflicts=int(conflicts.get("issues") or 0),
        estimate_conflict_warnings=int(conflicts.get("warnings") or 0),
        active_substitutions=int(substitutions.get("active") or 0),
        actuals_upload_id=last_upload.get("upload_id"),
        last_reported_sprint=int(last_upload.get("sprint_no") or 0),
        done_in_sprint={no: frozenset(ids) for no, ids in done_in_sprint.items()},
        task_prodf={row["task_id"]: row["prodf_id"] for row in db.query_dicts(TASK_PRODF_SQL)},
        baseline_schedule={
            row["task_id"]: (row["decision"], row["start_sprint"], row["end_sprint"])
            for row in db.query_dicts(BASELINE_SCHEDULE_SQL)
        },
        skill_bus_factor=tuple(
            (row["skill_name"], int(row["bus_factor"]), bool(row["in_demand"]),
             bool(row["sole_in_role"]))
            for row in db.query_dicts(SKILL_BUS_FACTOR_SQL)
        ),
    )


def load_baseline_starts() -> dict[str, int]:
    """Старты базового прогона — на пересчёте по ним видно сдвиги (yellow)."""
    return {
        row["task_id"]: int(row["start_sprint"]) for row in db.query_dicts(BASELINE_STARTS_SQL)
    }


# ---------------------------------------------------------------------------
#  ВЫХОД: ровно то, что ляжет в таблицы контракта
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScheduleRow:
    task_id: str
    start_sprint: int | None
    end_sprint: int | None
    forecast_end_date: date | None
    decision: str
    decision_reason: str | None
    reason_code: str | None = None
    reason_text: str | None = None
    reason_details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Assignment:
    task_id: str
    sprint_no: int
    engineer_id: str
    role_id: int
    hours: Decimal
    home_team_id: str
    serving_team_id: str


@dataclass(frozen=True)
class AlertRow:
    sprint_no: int
    level: str
    alert_type: str
    entity_type: str
    entity_id: str
    message: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class KpiRow:
    sprint_no: int
    kpi_code: str
    value: Decimal
    target_min: Decimal | None
    target_max: Decimal | None
    details: dict[str, Any]
    kind: str = "forecast"  # forecast | actual (ТЗ: прогноз отличать от факта)


@dataclass(frozen=True)
class BaselineRow:
    task_id: str
    planned_sp: Decimal
    committed: bool


@dataclass(frozen=True)
class StateRow:
    task_id: str
    as_of_sprint: int
    status: str
    remaining_hh: Decimal
    remaining_sp: Decimal
    forecast_end_sprint: int | None


@dataclass(frozen=True)
class Plan:
    pi_id: str
    as_of_sprint: int
    status: str
    note: str
    params: dict[str, Any]
    schedule: tuple[ScheduleRow, ...]
    assignments: tuple[Assignment, ...]
    alerts: tuple[AlertRow, ...]
    kpis: tuple[KpiRow, ...]
    baseline: tuple[BaselineRow, ...]
    states: tuple[StateRow, ...]
    sp_shares: tuple[tuple[str, int, Decimal], ...] = ()  # (task_id, sprint_no, sp), ADR-020
    actuals_upload_id: int | None = None

    @property
    def in_quarter(self) -> tuple[ScheduleRow, ...]:
        return tuple(row for row in self.schedule if row.decision == "in_quarter")

    @property
    def deferred(self) -> tuple[ScheduleRow, ...]:
        return tuple(row for row in self.schedule if row.decision != "in_quarter")


# ---------------------------------------------------------------------------
#  ФОНД ЧАСОВ: единственное место, где часы считаются
# ---------------------------------------------------------------------------
class _Funds:
    """Часы по орбитам и занятость людей и команд.

    «Орбита с приоритетом» (ADR-001): сначала тратится бюджет своей орбиты,
    невыбранный остаток чужой орбиты уходит в заём. Инвариант
    `ENGINEER_OVERLOAD` проверяет СУММУ по всем орбитам, а сумма ставок равна
    `total_capacity_rate`, поэтому расход «по орбитам» заведомо не превышает
    общий фонд — но общий фонд всё равно проверяется отдельно: данные могут
    оказаться несогласованными, и падать об это не хочется.
    """

    def __init__(self, inputs: Inputs) -> None:
        self.fte = Decimal(inputs.fte_hours_per_sprint)
        # Множитель фонда по спринтам. Короткий 7-й спринт даёт 0.5714 от
        # обычного (ADR-017). Нет ключа — считаем спринт полным: безопасный
        # дефолт для тестов и для календарей без коротких спринтов.
        self.factors: dict[int, Decimal] = dict(inputs.sprint_factors)
        self.engineers: dict[str, EngineerInput] = {e.engineer_id: e for e in inputs.engineers}
        self._budget: dict[tuple[str, str], Decimal] = {
            (engineer.engineer_id, team_id): rate * self.fte
            for engineer in inputs.engineers
            for team_id, rate in engineer.orbits.items()
        }
        self._spent: dict[tuple[str, str, int], Decimal] = defaultdict(Decimal)
        self.used_sp: dict[tuple[str, int], Decimal] = defaultdict(Decimal)

    def sprint_factor(self, sprint_no: int) -> Decimal:
        """Доля фонда полного спринта, которую даёт спринт `sprint_no`."""
        return self.factors.get(sprint_no, Decimal("1"))

    # ---- часы ------------------------------------------------------------
    def orbit_left(self, engineer_id: str, team_id: str, sprint_no: int) -> Decimal:
        budget = (
            self._budget.get((engineer_id, team_id), Decimal("0"))
            * self.sprint_factor(sprint_no)
        )
        return budget - self._spent[(engineer_id, team_id, sprint_no)]

    def total_left(self, engineer_id: str, sprint_no: int) -> Decimal:
        engineer = self.engineers[engineer_id]
        spent = sum(
            (self._spent[(engineer_id, team_id, sprint_no)] for team_id in engineer.orbits),
            Decimal("0"),
        )
        return (
            engineer.total_capacity_rate * self.fte * self.sprint_factor(sprint_no) - spent
        )

    def spend(self, engineer_id: str, team_id: str, sprint_no: int, hours: Decimal) -> None:
        self._spent[(engineer_id, team_id, sprint_no)] += hours

    def free(self, assignments: list[Assignment]) -> None:
        for row in assignments:
            self._spent[(row.engineer_id, row.home_team_id, row.sprint_no)] -= row.hours

    # ---- SP --------------------------------------------------------------
    def take_sp(self, team_id: str, sprint_no: int, sp: Decimal) -> None:
        self.used_sp[(team_id, sprint_no)] += sp

    def release_sp(self, team_id: str, sprint_no: int, sp: Decimal) -> None:
        self.used_sp[(team_id, sprint_no)] -= sp


def _candidate_engineers(
    task_team: str, role_id: int, sprint_no: int, funds: _Funds, by_role: dict[int, list[str]]
) -> list[str]:
    """Кого можно поставить на роль: свои орбиты первыми, потом заёмщики.

    Порядок внутри групп — по убыванию свободных часов орбиты (у заёмщиков —
    по общему остатку), затем по `engineer_id`: без этого один и тот же вход
    давал бы разные планы. «Своя орбита» значит «у инженера есть бюджет этой
    команды в этом спринте»: бюджет орбиты заранее не резервируется, но и в заём
    не отдаётся раньше, чем свои задачи получат шанс (ADR-015).
    """
    own: list[tuple[Decimal, Decimal, str]] = []
    loans: list[tuple[Decimal, str]] = []
    for engineer_id in by_role.get(role_id, ()):
        engineer = funds.engineers[engineer_id]
        left = funds.total_left(engineer_id, sprint_no)
        if left <= 0:
            continue
        if task_team in engineer.orbits:
            own.append((funds.orbit_left(engineer_id, task_team, sprint_no), left, engineer_id))
        else:
            loans.append((left, engineer_id))
    own.sort(key=lambda item: (-item[0], -item[1], item[2]))
    loans.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in own] + [item[1] for item in loans]


def _spend_from(
    engineer: EngineerInput, task_team: str, sprint_no: int, need: Decimal, funds: _Funds
) -> tuple[Decimal, str | None]:
    """Списать до `need` часов РОВНО С ОДНОЙ орбиты. Возвращает (часы, home_team_id).

    Одна строка `plan_assignments` = одна орбита: `home_team_id` не входит в
    первичный ключ `(task_id, sprint_no, engineer_id, role_id)`, поэтому
    разложить одно назначение по двум орбитам контракт не позволяет, а указать
    первую орбиту при часах с двух — значит соврать в отчётности по орбитам.
    Не влезло в одну орбиту — остаток возьмёт следующий кандидат или следующий
    спринт (`_allocate_task`).
    """
    order = [task_team] if task_team in engineer.orbits else []  # своё ядро — первым
    order.extend(
        sorted(
            (team_id for team_id in engineer.orbits if team_id != task_team),
            key=lambda team_id: (
                -funds.orbit_left(engineer.engineer_id, team_id, sprint_no),
                team_id,
            ),
        )
    )

    for team_id in order:
        left = min(
            funds.orbit_left(engineer.engineer_id, team_id, sprint_no),
            funds.total_left(engineer.engineer_id, sprint_no),
        )
        if left <= 0:
            continue
        take = min(left, need)
        if take <= 0:
            break
        funds.spend(engineer.engineer_id, team_id, sprint_no, take)
        return take, team_id
    return Decimal("0"), None


def _allocate_task(
    task: TaskInput,
    start_sprint: int,
    funds: _Funds,
    by_role: dict[int, list[str]],
    sprint_count: int,
    coverage: dict[tuple[str, int], Decimal],
) -> tuple[list[Assignment], int, int] | None:
    """Разложить остаток задачи по спринтам и людям, начиная со `start_sprint`.

    Механика распределения часов (ADR-015, ревью M2, пункт 5):

    * роли внутри одного спринта закрываются ПАРАЛЛЕЛЬНО и независимо: цикл идёт
      по ролям, каждая берёт столько часов, сколько дают свободные исполнители;
    * одну роль в одном спринте могут закрывать НЕСКОЛЬКО человек — на каждого
      пишется своя строка `plan_assignments`;
    * часы роли, не поместившиеся в спринт, переезжают в следующий: задача
      растягивается (`end_sprint > start_sprint`);
    * окно задачи — `[min, max]` ФАКТИЧЕСКИ использованных спринтов; разрывы
      внутри окна не запрещены (задача ждёт конкретную роль), но подсвечиваются
      инвариантом `WINDOW_HAS_GAP` как warning;
    * если до конца квартала часы не нашлись, ВСЕ сделанные списания
      откатываются (`funds.free`): задача уйдёт в перенос, её часы вернутся
      в фонд — пробные назначения не «залипают»;
    * `efficiency` умножает потребность: чтобы закрыть смету `remaining_hours`,
      исполнителю нужно `remaining_hours × efficiency` СВОИХ часов (сейчас
      в данных везде `1.00`, поэтому формула вырождается в тождество).

    Возвращает (назначения, ПЕРВЫЙ использованный спринт, последний). Первый
    использованный, а не запрошенный: иначе задача «стартовала» бы в спринте,
    где по ней не сделано ни одного часа, и SP уехали бы не туда.
    """
    needed = task.needed
    if not needed:
        # Работа фактически сделана (остаток 0). Символическое назначение нужно,
        # иначе CHECK (hours > 0) и инвариант IN_QUARTER_WITHOUT_ASSIGNMENTS
        # противоречат друг другу.
        role_id = min(task.remaining) if task.remaining else None
        if role_id is None:
            return None
        for engineer_id in _candidate_engineers(task.team_id, role_id, start_sprint, funds, by_role):
            taken, home = _spend_from(
                funds.engineers[engineer_id], task.team_id, start_sprint, SYMBOLIC_HOURS, funds
            )
            if taken > 0 and home is not None:
                return (
                    [
                        Assignment(
                            task.task_id, start_sprint, engineer_id, role_id, taken, home, task.team_id
                        )
                    ],
                    start_sprint,
                    start_sprint,
                )
        return None

    assignments: list[Assignment] = []
    remaining = dict(needed)
    for sprint_no in range(start_sprint, sprint_count + 1):
        for role_id in sorted(remaining):
            need = remaining[role_id]
            if need <= 0:
                continue
            for engineer_id in _candidate_engineers(task.team_id, role_id, sprint_no, funds, by_role):
                # efficiency: смету закрывают ЧАСЫ ИСПОЛНИТЕЛЯ, а не сметы.
                efficiency = coverage.get((engineer_id, role_id), Decimal("1"))
                taken, home = _spend_from(
                    funds.engineers[engineer_id], task.team_id, sprint_no, need * efficiency, funds
                )
                if taken <= 0 or home is None:
                    continue
                assignments.append(
                    Assignment(task.task_id, sprint_no, engineer_id, role_id, taken, home, task.team_id)
                )
                need -= taken / efficiency
                remaining[role_id] = need
                if need <= 0:
                    break
        if all(hours <= 0 for hours in remaining.values()):
            used = [row.sprint_no for row in assignments]
            return assignments, min(used), max(used)

    funds.free(assignments)
    return None


def _sp_flow(
    task: TaskInput, start_sprint: int, funds: _Funds, capacity: Decimal, sprint_count: int
) -> dict[int, Decimal] | None:
    """Story Points задачи как поток по спринтам (ADR-020).

    Команда за спринт закрывает не больше `available_sp_per_sprint × factor`.
    Задача, начатая в спринте `start_sprint`, списывает SP с ёмкости команды
    начиная с него: сколько свободно в этом спринте, остаток — в следующих.
    Так задача с SP больше ёмкости одного спринта растягивается, а не
    переносится навсегда — ровно как требует пример онбординга с DB-202
    («алгоритм должен растянуть эту задачу минимум на 2 спринта»).

    В спринте старта у команды должна быть хоть какая-то свободная ёмкость:
    задача не может «начаться» там, где команде взять её не из чего.
    Возвращает {спринт: SP} или None, если SP не укладываются в квартал.
    """
    need = task.estimation_sp
    if need <= 0:
        return {}

    def free(sprint_no: int) -> Decimal:
        left = capacity * funds.sprint_factor(sprint_no) - funds.used_sp[(task.team_id, sprint_no)]
        return left.quantize(Decimal("0.01"), rounding=ROUND_DOWN) if left > 0 else Decimal("0")

    if free(start_sprint) <= 0:
        return None
    shares: dict[int, Decimal] = {}
    for sprint_no in range(start_sprint, sprint_count + 1):
        available = free(sprint_no)
        if available <= 0:
            continue
        take = min(available, need)
        shares[sprint_no] = take
        need -= take
        if need <= 0:
            return shares
    return None


def _sprints_word(n: int) -> str:
    """«2 спринта», «5 спринтов» — текст причин читает заказчик."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} спринт"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} спринта"
    return f"{n} спринтов"


def _q(value: Decimal) -> str:
    """Два знака без хвостовых нулей: 40.00 -> 40, 7.20 -> 7.2."""
    text = f"{value.quantize(Decimal('0.01'))}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------------------
#  ЧИСТАЯ ЛОГИКА: вход → план
# ---------------------------------------------------------------------------
def build_plan(
    inputs: Inputs,
    as_of_sprint: int = 0,
    baseline_starts: dict[str, int] | None = None,
    dependency_mode: str = DEPENDENCY_MODE_START_START,
    initiative_mode: str = INITIATIVE_MODE_GREEDY,
    simulate_next_pi: bool = True,
) -> Plan:
    """Строит план. Ни одного обращения к базе: всё, что нужно, уже во `Inputs`.

    `simulate_next_pi` — проверка для рекомендации отмены (ADR-022): задачи,
    перенесённые из-за нехватки часов или ёмкости, пробно раскладываются в
    «следующий квартал» с тем же штатом; не влезли и туда — `cancelled`.
    Внутренний вызов симуляции идёт с False, чтобы не уйти в рекурсию.

    `dependency_mode` и `initiative_mode` — решения ADR-013; оба уезжают
    в `plan_runs.params`, поэтому любой прогон сам объясняет, по каким правилам
    он построен. Значения по умолчанию — те, на которых прошла приёмка M2.
    """
    if not 0 <= as_of_sprint <= 12:
        raise ValueError(f"as_of_sprint={as_of_sprint} вне диапазона 0..12 (CHECK в plan_runs)")
    if dependency_mode not in DEPENDENCY_MODES:
        raise ValueError(f"dependency_mode={dependency_mode!r} не из {DEPENDENCY_MODES}")
    if initiative_mode not in INITIATIVE_MODES:
        raise ValueError(f"initiative_mode={initiative_mode!r} не из {INITIATIVE_MODES}")

    # В закрытые спринты план не пишется (ADR-014): при `as_of_sprint = k` спринт
    # k начинается «сегодня», всё до него — история. Инвариант
    # `ASSIGNMENT_IN_CLOSED_SPRINT` проверяет это независимо от алгоритма.
    replan_floor = max(1, as_of_sprint)

    by_id = {task.task_id: task for task in inputs.tasks}
    # Кандидаты на роль — из покрытия (ADR-012), а не из `engineers.role_id`:
    # вьюха — единственный источник правды о паре «инженер × роль».
    by_role: dict[int, list[str]] = defaultdict(list)
    for engineer_id, role_id in inputs.coverage:
        by_role[role_id].append(engineer_id)
    for ids in by_role.values():
        ids.sort()

    deps_by_blocked: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for blocking, blocked, gap in inputs.deps:
        deps_by_blocked[blocked].append((blocking, gap))

    # Порядок обхода: инициатива по скорингу (NULL — в конец), внутри — топология.
    ordered = sorted(
        inputs.tasks,
        key=lambda t: (t.priority_rung is None, -(t.priority_rung or 0), t.topo_order, t.task_id),
    )

    funds = _Funds(inputs)
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    placed: dict[str, list[Assignment]] = {}
    sp_shares: dict[str, dict[int, Decimal]] = {}
    deferred: dict[str, str] = {}
    atomic_deferred: set[str] = set()
    # Итог квартала: факт загружен за последний спринт — планировать некуда (ADR-021).
    pi_closed = replan_floor > inputs.sprint_count

    def release(task_id: str) -> None:
        """Снять задачу с плана: вернуть в фонд её SP (по всем спринтам) и часы."""
        task = by_id[task_id]
        funds.free(placed.pop(task_id, []))
        for sprint_no, sp in sp_shares.pop(task_id, {}).items():
            funds.release_sp(task.team_id, sprint_no, sp)
        starts.pop(task_id, None)
        ends.pop(task_id, None)

    def release_initiative(prodf_id: str) -> list[str]:
        """Откат ВСЕЙ инициативы: вернуть в фонд её SP и часы (ADR-013)."""
        rolled = [
            task.task_id for task in ordered if task.prodf_id == prodf_id and task.task_id in starts
        ]
        for task_id in rolled:
            release(task_id)
        return rolled

    def defer_initiative(prodf_id: str) -> None:
        """Атомарный режим: откатанные задачи инициативы — тоже переносы.

        Раньше откат снимал задачи с плана, но не помечал их перенесёнными,
        и сборка расписания падала бы на `ends[task_id]`.
        """
        for rolled in release_initiative(prodf_id):
            deferred[rolled] = blocked_reason(by_id[rolled])
            atomic_deferred.add(rolled)

    def ready_from(blocking: str, gap: int) -> int:
        """С какого спринта блокируемая задача вправе стартовать (ADR-013).

        `start_start`: старт блокирующей + зазор. `finish_start`: КОНЕЦ
        блокирующей + зазор. Блокирующая могла быть снята с плана между
        проходами — тогда ограничение не действует и берётся нижняя граница
        пересчёта.
        """
        if dependency_mode == DEPENDENCY_MODE_FINISH_START:
            return (ends.get(blocking) or starts.get(blocking, replan_floor)) + gap
        return starts.get(blocking, replan_floor) + gap

    def blocked_reason(task: TaskInput) -> str:
        """M3 — если задачу держит перенесённая блокирующая чужая команда."""
        for blocking, _gap in deps_by_blocked.get(task.task_id, ()):
            if blocking in deferred and by_id[blocking].team_id != task.team_id:
                return DEFERRED_REASON_BLOCKED
        return DEFERRED_REASON

    def lower_bound(task: TaskInput) -> int:
        """Нижняя граница старта: закрытые спринты, граф и уже принятые зазоры."""
        lower = max(replan_floor, task.earliest_start_sprint)
        for blocking, gap in deps_by_blocked.get(task.task_id, ()):
            if blocking in starts:  # блокирующая уже поставлена — держим зазор
                lower = max(lower, ready_from(blocking, gap))
        return lower

    def missing_roles(task: TaskInput) -> list[int]:
        """Роли задачи, на которые в штате нет НИ ОДНОГО инженера."""
        return [role_id for role_id in sorted(task.needed) if not by_role.get(role_id)]

    def place(task: TaskInput, lower: int) -> bool:
        """Поставить задачу в минимальный подходящий спринт. False — не влезла.

        Часы раскладываются первыми, затем SP текут по спринтам начиная со
        спринта ФАКТИЧЕСКОГО старта (ADR-020): в каждом спринте команда берёт не
        больше свободной ёмкости, остаток — в следующих. Окно задачи —
        от первого спринта с часами до последнего спринта с часами или с SP.
        """
        capacity = inputs.team_sp_per_sprint.get(task.team_id, Decimal("0"))
        for candidate in range(max(replan_floor, lower), inputs.sprint_count + 1):
            result = _allocate_task(
                task, candidate, funds, by_role, inputs.sprint_count, inputs.coverage
            )
            if result is None:
                continue
            rows, start_used, end_hours = result
            shares = _sp_flow(task, start_used, funds, capacity, inputs.sprint_count)
            if shares is None:
                funds.free(rows)  # SP не укладываются от этого старта — пробуем позже
                continue
            for sprint_no, sp in shares.items():
                funds.take_sp(task.team_id, sprint_no, sp)
            starts[task.task_id] = start_used
            ends[task.task_id] = max([end_hours, *shares])
            placed[task.task_id] = rows
            sp_shares[task.task_id] = shares
            deferred.pop(task.task_id, None)
            atomic_deferred.discard(task.task_id)
            return True
        return False

    def propagate_deferrals() -> None:
        """Перенесённая блокирующая тянет за собой зависимые (инвариант
        `DEPENDENCY_BLOCKER_DEFERRED`). Зовётся после КАЖДОГО прохода, который
        может что-то перенести: раньше переносы третьего прохода не протягивались."""
        changed = True
        while changed:
            changed = False
            for task in ordered:
                if task.task_id in deferred:
                    continue
                if any(b in deferred for b, _gap in deps_by_blocked.get(task.task_id, ())):
                    release(task.task_id)
                    deferred[task.task_id] = blocked_reason(task)
                    if initiative_mode == INITIATIVE_MODE_ATOMIC:
                        defer_initiative(task.prodf_id)
                    changed = True

    def fix_gaps() -> None:
        """Догон зазоров, если блокирующая уехала позже, чем стояла зависимая."""
        for _ in range(len(ordered) + 1):
            violations = [
                (blocking, blocked, gap)
                for blocking, blocked, gap in inputs.deps
                if blocking in starts
                and blocked in starts
                and starts[blocked] < ready_from(blocking, gap)
            ]
            if not violations:
                return
            for blocking, blocked, gap in violations:
                if blocked not in starts:  # уже снята выше по этому же циклу
                    continue
                task = by_id[blocked]
                release(blocked)
                if not place(task, max(lower_bound(task), ready_from(blocking, gap))):
                    deferred[blocked] = blocked_reason(task)
                    if initiative_mode == INITIATIVE_MODE_ATOMIC:
                        defer_initiative(task.prodf_id)

    # ---- проход 1: обход в порядке приоритетов ----------------------------
    if pi_closed:
        for task in ordered:
            deferred[task.task_id] = DEFERRED_REASON
    elif initiative_mode == INITIATIVE_MODE_ATOMIC:
        # Пробная упаковка инициативы целиком (ADR-013): не влезла хоть одна
        # задача — откат всех. Иначе дефицитный исполнитель занят инициативой,
        # которая всё равно не завершится, а KPI считает только завершённые.
        groups: dict[str, list[TaskInput]] = defaultdict(list)
        for task in ordered:  # `ordered` уже отсортирован — порядок инициатив сохранён
            groups[task.prodf_id].append(task)
        for prodf_id, members in groups.items():
            for task in members:
                if not place(task, lower_bound(task)):
                    break
            else:
                continue
            release_initiative(prodf_id)
            for task in members:
                deferred[task.task_id] = blocked_reason(task)
                atomic_deferred.add(task.task_id)
    else:
        for task in ordered:
            if not place(task, lower_bound(task)):
                deferred[task.task_id] = DEFERRED_REASON

    if not pi_closed:
        # ---- проходы 2–3: перенос тянет зависимые, догон зазоров -----------
        propagate_deferrals()
        fix_gaps()
        propagate_deferrals()

        # ---- проход 4: повторная упаковка (ADR-020) ------------------------
        # Переносы проходов 2–3 возвращают часы и SP в фонд. Без этого прохода
        # освободившийся ресурс пропадал, а причина «не хватило ресурсов» у
        # перенесённой задачи была бы неправдой. Атомарный режим не трогаем:
        # там упаковка — по инициативам целиком.
        if initiative_mode == INITIATIVE_MODE_GREEDY:
            for _ in range(len(ordered) + 1):
                placed_now = False
                for task in ordered:
                    if task.task_id not in deferred or missing_roles(task):
                        continue
                    if any(b in deferred for b, _gap in deps_by_blocked.get(task.task_id, ())):
                        continue
                    if place(task, lower_bound(task)):
                        placed_now = True
                if not placed_now:
                    break
                fix_gaps()
                propagate_deferrals()

    # ---- причины решений (ADR-022) ---------------------------------------
    rank = {task.task_id: index for index, task in enumerate(ordered, start=1)}

    def taken_by(role_id: int, limit: int = 5) -> list[str]:
        """Какие поставленные задачи больше всех заняли часы этой роли."""
        hours: dict[str, Decimal] = defaultdict(Decimal)
        for task_id, rows in placed.items():
            for row in rows:
                if row.role_id == role_id:
                    hours[task_id] += row.hours
        ranked = sorted(hours.items(), key=lambda item: (-item[1], item[0]))
        return [task_id for task_id, _hours in ranked[:limit]]

    def diagnose(task: TaskInput) -> tuple[str, str, dict[str, Any]]:
        """Почему задача не в квартале — по фактическому состоянию фонда."""
        task_id = task.task_id
        if pi_closed:
            return (
                REASON_PI_CLOSED,
                f"Перенесена: квартал завершён (факт загружен за спринт "
                f"{inputs.last_reported_sprint}), остаток {_q(task.demand_hh)} ЧЧ уходит "
                f"в следующий PI",
                {"remaining_hh": str(task.demand_hh)},
            )
        missing = missing_roles(task)
        if missing:
            items = [
                {"role": task.role_names.get(role_id, str(role_id)), "hours": str(task.needed[role_id])}
                for role_id in missing
            ]
            listed = ", ".join(f"«{item['role']}» ({_q(Decimal(item['hours']))} ЧЧ)" for item in items)
            return (
                REASON_ROLE_NOT_IN_STAFF,
                f"Перенесена: в штате нет роли {listed} — закрыть эту часть работы некому. "
                f"Нужен наём или дообучение; замещения ролей запрещены организаторами",
                {"missing_roles": items},
            )
        blockers = sorted(b for b, _gap in deps_by_blocked.get(task_id, ()) if b in deferred)
        if blockers:
            return (
                REASON_BLOCKED,
                f"Перенесена: ждёт {', '.join(blockers)} — блокирующая задача сама не попала "
                f"в квартал, а начинать раньше неё нельзя",
                {
                    "blocked_by": blockers,
                    "other_team": any(by_id[b].team_id != task.team_id for b in blockers),
                },
            )
        lower = lower_bound(task)
        trial = _allocate_task(task, lower, funds, by_role, inputs.sprint_count, inputs.coverage)
        if trial is not None:
            funds.free(trial[0])  # пробное распределение не должно залипнуть в фонде
            if task_id in atomic_deferred:
                return (
                    REASON_ATOMIC,
                    f"Перенесена вместе с инициативой {task.prodf_id}: сама задача помещается, "
                    f"но другая задача инициативы — нет, а режим atomic частичных инициатив "
                    f"не допускает",
                    {"prodf_id": task.prodf_id},
                )
            capacity = inputs.team_sp_per_sprint.get(task.team_id, Decimal("0"))
            free_sp = sum(
                (
                    max(capacity * funds.sprint_factor(n) - funds.used_sp[(task.team_id, n)], Decimal("0"))
                    for n in range(lower, inputs.sprint_count + 1)
                ),
                Decimal("0"),
            )
            return (
                REASON_TEAM_SP,
                f"Перенесена: часов специалистов хватает, но у {task.team_id} не осталось "
                f"ёмкости — нужно {_q(task.estimation_sp)} SP, свободно {_q(free_sp)} SP со "
                f"спринта {lower} до конца квартала; ёмкость заняли задачи с более высоким "
                f"приоритетом",
                {
                    "team_id": task.team_id,
                    "need_sp": str(task.estimation_sp),
                    "free_sp": str(free_sp.quantize(Decimal("0.01"))),
                    "from_sprint": lower,
                },
            )
        shortages = []
        for role_id, need in sorted(task.needed.items()):
            free = sum(
                (
                    max(funds.total_left(engineer_id, n), Decimal("0"))
                    for engineer_id in by_role.get(role_id, ())
                    for n in range(lower, inputs.sprint_count + 1)
                ),
                Decimal("0"),
            )
            if free < need:
                shortages.append(
                    {
                        "role": task.role_names.get(role_id, str(role_id)),
                        "need_hh": str(need),
                        "free_hh": str(free.quantize(Decimal("0.01"))),
                        "taken_by": taken_by(role_id),
                    }
                )
        if shortages:
            listed = "; ".join(
                f"«{item['role']}»: нужно {_q(Decimal(item['need_hh']))} ЧЧ, свободно "
                f"{_q(Decimal(item['free_hh']))} ЧЧ"
                + (f" (часы заняты {', '.join(item['taken_by'])})" if item["taken_by"] else "")
                for item in shortages
            )
            text = (
                f"Перенесена: не хватает часов специалистов со спринта {lower} до конца квартала — "
                f"{listed}. Часы отданы задачам с более высоким приоритетом"
            )
        else:
            text = (
                "Перенесена: часов по ролям в сумме хватает, но их не собрать в нужные спринты — "
                "свободные часы у людей разнесены по разным командам и спринтам"
            )
        return (REASON_ROLE_HOURS, text, {"shortages": shortages, "from_sprint": lower})

    reasons = {task.task_id: diagnose(task) for task in ordered if task.task_id in deferred}

    # ---- рекомендация отмены: не влезает и в следующий квартал (ADR-022) ---
    cancelled: dict[str, tuple[str, str, dict[str, Any]]] = {}
    next_pi_check: dict[str, Any] = {"simulated": [], "fits_next_pi": []}
    if simulate_next_pi and not pi_closed and initiative_mode == INITIATIVE_MODE_GREEDY:
        pool = {
            task_id for task_id, (code, _text, _details) in reasons.items()
            if code in (REASON_ROLE_HOURS, REASON_TEAM_SP)
        }
        grew = True
        while grew:  # зависимые едут в симуляцию, если их держат только такие задачи
            grew = False
            for task_id, (code, _text, details) in reasons.items():
                if task_id in pool or code != REASON_BLOCKED:
                    continue
                if all(b in pool for b in details.get("blocked_by", [])):
                    pool.add(task_id)
                    grew = True
        if pool:
            simulated = replace(
                inputs,
                tasks=tuple(
                    replace(task, earliest_start_sprint=1) for task in ordered if task.task_id in pool
                ),
                deps=tuple(dep for dep in inputs.deps if dep[0] in pool and dep[1] in pool),
                baseline_schedule={},
            )
            trial_plan = build_plan(
                simulated,
                as_of_sprint=0,
                dependency_mode=dependency_mode,
                initiative_mode=initiative_mode,
                simulate_next_pi=False,
            )
            fits_next = {row.task_id for row in trial_plan.schedule if row.decision == "in_quarter"}
            next_pi_check = {"simulated": sorted(pool), "fits_next_pi": sorted(fits_next)}
            for task_id in sorted(pool - fits_next):
                code, text, details = reasons[task_id]
                cancelled[task_id] = (
                    REASON_NOT_FEASIBLE,
                    "Рекомендуем отменить или пересогласовать: задача не помещается ни в этот "
                    "квартал, ни в следующий при текущем штате. " + text,
                    {**details, "cause_code": code},
                )

    def legacy_reason(code: str, details: dict[str, Any]) -> str:
        """Старый код расхождений для колонки decision_reason (совместимость)."""
        if code == REASON_BLOCKED and details.get("other_team"):
            return DEFERRED_REASON_BLOCKED
        if code == REASON_NOT_FEASIBLE:
            return CANCEL_REASON
        return DEFERRED_REASON

    def explain_planned(task: TaskInput) -> tuple[str, dict[str, Any]]:
        """Почему задача ВКЛЮЧЕНА — ТЗ требует объяснять и это."""
        task_id = task.task_id
        rows = placed.get(task_id, [])
        roles: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            roles[task.role_names.get(row.role_id, str(row.role_id))].add(row.engineer_id)
        loan_hh = sum((row.hours for row in rows if row.home_team_id != row.serving_team_id), Decimal("0"))
        shares = sp_shares.get(task_id, {})
        window = (
            f"спринт {starts[task_id]}"
            if starts[task_id] == ends[task_id]
            else f"спринты {starts[task_id]}–{ends[task_id]}"
        )
        if not task.needed:
            text = (
                f"Включена: работа по смете фактически выполнена (остаток 0 ЧЧ), "
                f"задача закрывается в {window}"
            )
        else:
            parts = [
                f"Включена: приоритет инициативы {task.priority_rung} — {rank[task_id]}-я в очереди "
                f"из {len(ordered)}, {window}",
                "Роли закрыты: "
                + "; ".join(f"{name} — {', '.join(sorted(people))}" for name, people in sorted(roles.items())),
            ]
            if loan_hh > 0:
                parts.append(f"{_q(loan_hh)} ЧЧ взяты в заём у других команд")
            if len(shares) > 1:
                parts.append(
                    f"{_q(task.estimation_sp)} SP растянуты на {_sprints_word(len(shares))}: "
                    f"это больше свободной ёмкости команды за один спринт"
                )
            text = ". ".join(parts)
        return text, {
            "priority_rung": task.priority_rung,
            "queue_rank": rank[task_id],
            "queue_size": len(ordered),
            "roles": {name: sorted(people) for name, people in sorted(roles.items())},
            "loan_hh": str(loan_hh),
            "sp_by_sprint": {str(n): str(sp) for n, sp in sorted(shares.items())},
        }

    # ---- расписание -------------------------------------------------------
    schedule: list[ScheduleRow] = []
    for task in ordered:
        task_id = task.task_id
        if task_id in cancelled:
            code, text, details = cancelled[task_id]
            schedule.append(
                ScheduleRow(task_id, None, None, None, "cancelled", CANCEL_REASON, code, text, details)
            )
        elif task_id in deferred:
            code, text, details = reasons[task_id]
            schedule.append(
                ScheduleRow(
                    task_id, None, None, None, "deferred_next_pi",
                    legacy_reason(code, details), code, text, details,
                )
            )
        else:
            end_sprint = ends[task_id]
            end_date = inputs.sprints.get(end_sprint, (None, None))[1]
            text, details = explain_planned(task)
            schedule.append(
                ScheduleRow(
                    task_id, starts[task_id], end_sprint, end_date, "in_quarter", None,
                    REASON_PLANNED, text, details,
                )
            )

    assignments = [_round_hours(row) for task in ordered for row in placed.get(task.task_id, [])]
    return _assemble(
        inputs,
        as_of_sprint,
        schedule,
        assignments,
        baseline_starts or {},
        {
            "dependency_mode": dependency_mode,
            "initiative_mode": initiative_mode,
            "replan_floor": replan_floor,
            "sp_model": "flow",
            "repack": initiative_mode == INITIATIVE_MODE_GREEDY,
            "next_pi_check": next_pi_check,
            "pi_closed": pi_closed,
        },
        sp_shares=tuple(
            (task.task_id, n, sp)
            for task in ordered
            for n, sp in sorted(sp_shares.get(task.task_id, {}).items())
        ),
        lower_bounds={
            task.task_id: lower_bound(task) for task in ordered if task.task_id not in starts
        },
        pi_closed=pi_closed,
    )


def _round_hours(row: Assignment) -> Assignment:
    """Часы — ровно два знака: контракт хранит NUMERIC(8,2), округляем заранее."""
    return Assignment(
        task_id=row.task_id,
        sprint_no=row.sprint_no,
        engineer_id=row.engineer_id,
        role_id=row.role_id,
        hours=Decimal(row.hours).quantize(Decimal("0.01")),
        home_team_id=row.home_team_id,
        serving_team_id=row.serving_team_id,
    )


def _assemble(
    inputs: Inputs,
    as_of_sprint: int,
    schedule: list[ScheduleRow],
    assignments: list[Assignment],
    baseline_starts: dict[str, int],
    modes: dict[str, Any] | None = None,
    *,
    sp_shares: tuple[tuple[str, int, Decimal], ...] = (),
    lower_bounds: dict[str, int] | None = None,
    pi_closed: bool = False,
) -> Plan:
    """Собирает `Plan`: алерты, KPI, базовая линия, слепок состояния, params.

    `modes` — режимы прогона (ADR-013/014): уезжают в `plan_runs.params`, чтобы
    у каждого результата было объяснение, по каким правилам он получен.
    """
    by_id = {task.task_id: task for task in inputs.tasks}
    in_quarter = [row for row in schedule if row.decision == "in_quarter"]
    deferred = [row for row in schedule if row.decision != "in_quarter"]

    alerts = _build_alerts(
        inputs, schedule, baseline_starts,
        assignments=assignments, lower_bounds=lower_bounds or {}, as_of_sprint=as_of_sprint,
    )
    kpis = _build_kpis(inputs, schedule, baseline_starts, as_of_sprint=as_of_sprint)
    states = _build_states(inputs, schedule, as_of_sprint)
    baseline = (
        [
            BaselineRow(
                task_id=task.task_id,
                planned_sp=task.estimation_sp,
                committed=task.task_id in {row.task_id for row in in_quarter},
            )
            for task in inputs.tasks
        ]
        if as_of_sprint == 0
        else []
    )

    in_quarter_hh = sum((by_id[row.task_id].demand_hh for row in in_quarter), Decimal("0"))
    deferred_hh = sum((by_id[row.task_id].demand_hh for row in deferred), Decimal("0"))
    loan_hh = sum(
        (row.hours for row in assignments if row.home_team_id != row.serving_team_id), Decimal("0")
    )

    # Видимость цены целевой функции (ADR-015): сколько инициатив закрыто целиком,
    # сколько осталось частично. В `greedy`-режиме частичные — норма, но заказчик
    # должен видеть их число, а не только агрегат KPI.
    by_initiative: dict[str, list[str]] = defaultdict(list)
    for task in inputs.tasks:
        by_initiative[task.prodf_id].append(task.task_id)
    in_quarter_ids = {row.task_id for row in in_quarter}
    complete_initiatives = [
        prodf_id for prodf_id, ids in by_initiative.items() if set(ids) <= in_quarter_ids
    ]
    partial_initiatives = sorted(
        prodf_id
        for prodf_id, ids in by_initiative.items()
        if 0 < len(set(ids) & in_quarter_ids) < len(ids)
    )

    # Календарь уезжает в `plan_runs.params`: прогон без границ PI невозможно
    # сопоставить с кварталом, а фонд 525.71 ЧЧ выглядит «взятым с потолка»,
    # если рядом нет 92 дней и множителя 6.5714 (ADR-017).
    pi_start = min((pair[0] for pair in inputs.sprints.values()), default=None)
    pi_end = max((pair[1] for pair in inputs.sprints.values()), default=None)
    short_sprints = {
        str(no): str(factor)
        for no, factor in sorted(inputs.sprint_factors.items())
        if factor < 1
    }

    params: dict[str, Any] = {
        "algorithm": ALGORITHM,
        "estimate_source": ESTIMATE_SOURCE,
        "calendar": {
            "pi_start": str(pi_start) if pi_start else None,
            "pi_end": str(pi_end) if pi_end else None,
            "sprint_count": inputs.sprint_count,
            "pi_days": inputs.pi_days,
            "fund_factor": str(inputs.fund_factor),
            "fund_hh_per_fte": str(inputs.fund_hours_per_fte),
            "short_sprints": short_sprints,
        },
        "estimate_validated": True,
        "estimate_conflicts": inputs.estimate_conflicts,
        "estimate_conflicts_note": (
            "три источника часов расходятся; авторитетен столбец матрицы сметы "
            "(ADR-002, ответ организаторов №4), расхождения — в dq_issues"
        ),
        "substitution_mode": SUBSTITUTION_MODE,
        "active_substitutions": inputs.active_substitutions,
        "live_tasks": len(inputs.tasks),
        "in_quarter": len(in_quarter),
        "deferred": len(deferred),
        "in_quarter_hh": str(in_quarter_hh),
        "deferred_hh": str(deferred_hh),
        "loan_hh": str(loan_hh),
        "baseline_starts_used": bool(baseline_starts),
        "objective": OBJECTIVE,
        "objective_note": OBJECTIVE_NOTE,
        "efficiency_note": EFFICIENCY_NOTE,
        "initiatives_planned": len(by_initiative),
        "initiatives_complete": len(complete_initiatives),
        "initiatives_partial": partial_initiatives,
        "cancelled": sum(1 for row in schedule if row.decision == "cancelled"),
        "reasons": dict(sorted(Counter(row.reason_code for row in schedule if row.reason_code).items())),
        "actuals_upload_id": inputs.actuals_upload_id,
        "last_reported_sprint": inputs.last_reported_sprint,
    }
    params.update(modes or {})
    reason_summary = ", ".join(
        f"{code} {count}" for code, count in params["reasons"].items() if code != "PLANNED"
    )
    note = (
        f"{len(in_quarter)} из {len(inputs.tasks)} живых задач в квартале, "
        f"{len(deferred)} не в квартале ({reason_summary or 'нет'}); инициатив целиком "
        f"{len(complete_initiatives)} из {len(by_initiative)}"
        f"{f', частично {len(partial_initiatives)}' if partial_initiatives else ''}; "
        f"алертов {len(alerts)}; займов {loan_hh} ЧЧ; замещения отклонены (ответ №2, ADR-010)"
    )
    return Plan(
        pi_id=inputs.pi_id,
        as_of_sprint=as_of_sprint,
        # Итог квартала (факт за последний спринт) — законный прогон, а не сбой.
        status="ok" if in_quarter or pi_closed else "infeasible",
        note=note,
        params=params,
        schedule=tuple(schedule),
        assignments=tuple(assignments),
        alerts=tuple(alerts),
        kpis=tuple(kpis),
        baseline=tuple(baseline),
        states=tuple(states),
        sp_shares=sp_shares,
        actuals_upload_id=inputs.actuals_upload_id,
    )


def _build_alerts(
    inputs: Inputs,
    schedule: list[ScheduleRow],
    baseline_starts: dict[str, int],
    *,
    assignments: list[Assignment] | tuple[Assignment, ...] = (),
    lower_bounds: dict[str, int] | None = None,
    as_of_sprint: int = 0,
) -> list[AlertRow]:
    """Три типа рисков из ТЗ.

    * orange — «дефицит специалистов на следующий спринт»: потребность роли на
      спринт, который начинается сейчас (часы, уже поставленные на него, плюс
      остаток задач, которые могли бы в нём стартовать, но не получили
      специалиста), больше фонда этой роли в спринте;
    * red — «выход прогнозной даты завершения за пределы квартала»: инициатива,
      у которой есть задачи вне квартала; для целей первоначального плана это
      «цель квартала под угрозой»;
    * yellow — «сдвиг цепочки зависимых задач»: задача с зависимыми стартует
      позже, чем в первоначальном плане; в payload — причина сдвига.
    """
    by_id = {task.task_id: task for task in inputs.tasks}
    fte = Decimal(inputs.fte_hours_per_sprint)
    lower_bounds = lower_bounds or {}
    staffed = {role_id for (_engineer_id, role_id) in inputs.coverage}
    alerts: list[AlertRow] = []

    # --- orange: дефицит специалистов на следующий спринт -------------------
    next_sprint = max(1, as_of_sprint)
    if next_sprint <= inputs.sprint_count:
        factor = inputs.sprint_factors.get(next_sprint, Decimal("1"))
        supply: dict[int, Decimal] = defaultdict(Decimal)
        for engineer in inputs.engineers:
            supply[engineer.role_id] += engineer.total_capacity_rate * fte * factor
        planned: dict[int, Decimal] = defaultdict(Decimal)
        for row in assignments:
            if row.sprint_no == next_sprint:
                planned[row.role_id] += row.hours
        unmet: dict[int, Decimal] = defaultdict(Decimal)
        unmet_tasks: dict[int, list[str]] = defaultdict(list)
        names: dict[int, str] = {}
        for row in schedule:
            task = by_id.get(row.task_id)
            if task is None or row.decision == "in_quarter":
                continue
            if lower_bounds.get(row.task_id, next_sprint + 1) > next_sprint:
                continue  # задача и так не могла стартовать в этом спринте
            cause = row.reason_details.get("cause_code", row.reason_code)
            if cause == REASON_ROLE_NOT_IN_STAFF:
                roles = [role_id for role_id in task.needed if role_id not in staffed]
            elif cause == REASON_ROLE_HOURS:
                by_name = {name: role_id for role_id, name in task.role_names.items()}
                roles = [
                    by_name[item["role"]]
                    for item in row.reason_details.get("shortages", [])
                    if item["role"] in by_name
                ]
            else:
                continue
            for role_id in roles:
                unmet[role_id] += task.needed.get(role_id, Decimal("0"))
                unmet_tasks[role_id].append(task.task_id)
                names[role_id] = task.role_names.get(role_id, str(role_id))
        for role_id in sorted(unmet):
            need = planned[role_id] + unmet[role_id]
            have = supply.get(role_id, Decimal("0"))
            if need <= have:
                continue
            tail = (
                "в штате нет ни одного специалиста — нужен наём или дообучение"
                if have == 0
                else f"не хватает {_q(need - have)} ЧЧ"
            )
            alerts.append(
                AlertRow(
                    sprint_no=next_sprint,
                    level="orange",
                    alert_type="role_deficit",
                    entity_type="role",
                    entity_id=names[role_id],
                    message=(
                        f"спринт {next_sprint}: роли «{names[role_id]}» нужно {_q(need)} ЧЧ "
                        f"({_q(planned[role_id])} уже в плане + {_q(unmet[role_id])} на задачах, "
                        f"которые ждут этот ресурс), доступно {_q(have)} ЧЧ — {tail}"
                    ),
                    payload={
                        "role_id": role_id,
                        "role_name": names[role_id],
                        "sprint_no": next_sprint,
                        "demand_hh": str(need),
                        "planned_hh": str(planned[role_id]),
                        "unmet_hh": str(unmet[role_id]),
                        "supply_hh": str(have),
                        "tasks": sorted(unmet_tasks[role_id]),
                        "verdict": "НАЙМ: закрыть некем" if have == 0 else "НАЙМ: не хватает часов",
                        "reason": "замещения ролей отклонены организаторами (ответ №2, ADR-010)",
                    },
                )
            )

    # --- red: прогноз выходит за квартал, цель инициативы под угрозой -------
    base = inputs.baseline_schedule
    committed: set[str] = set()
    if base:
        tasks_of: dict[str, list[str]] = defaultdict(list)
        for task_id in base:
            tasks_of[inputs.task_prodf.get(task_id, by_id[task_id].prodf_id if task_id in by_id else "?")].append(task_id)
        committed = {
            prodf_id for prodf_id, ids in tasks_of.items()
            if all(base[task_id][0] == "in_quarter" for task_id in ids)
        }
    outside: dict[str, list[ScheduleRow]] = defaultdict(list)
    for row in schedule:
        if row.decision != "in_quarter" and row.task_id in by_id:
            outside[by_id[row.task_id].prodf_id].append(row)
    for prodf_id in sorted(outside):
        rows = sorted(outside[prodf_id], key=lambda item: item.task_id)
        task_ids = [row.task_id for row in rows]
        hh = sum((by_id[task_id].demand_hh for task_id in task_ids), Decimal("0"))
        threatened = prodf_id in committed and as_of_sprint > 0
        message = (
            f"{prodf_id}: цель квартала под угрозой — {len(task_ids)} задач из первоначального "
            f"плана больше не укладываются в 12 недель ({_q(hh)} ЧЧ)"
            if threatened
            else f"{prodf_id}: {len(task_ids)} задач перенесено в следующий PI, {hh} ЧЧ "
            f"не закрыто — инициатива не уложится в квартал"
        )
        alerts.append(
            AlertRow(
                sprint_no=inputs.sprint_count,
                level="red",
                alert_type="deadline_miss",
                entity_type="initiative",
                entity_id=prodf_id,
                message=message,
                payload={
                    "deferred_tasks": task_ids,
                    "deferred_hh": str(hh),
                    "baseline_committed": prodf_id in committed,
                    "threatened_goal": threatened,
                    "reasons": {row.task_id: row.reason_code for row in rows},
                    "cancelled": [row.task_id for row in rows if row.decision == "cancelled"],
                },
            )
        )

    # --- yellow: сдвиг задачи, у которой есть зависимые ---------------------
    dependents: dict[str, list[str]] = defaultdict(list)
    blockers_of: dict[str, list[str]] = defaultdict(list)
    for blocking, blocked, _gap in inputs.deps:
        dependents[blocking].append(blocked)
        blockers_of[blocked].append(blocking)
    base_starts = dict(baseline_starts) or {
        task_id: start for task_id, (_d, start, _e) in base.items() if start is not None
    }
    current = {row.task_id: row for row in schedule}
    for row in schedule:
        base_start = base_starts.get(row.task_id)
        if row.decision != "in_quarter" or base_start is None or row.start_sprint is None:
            continue
        if row.start_sprint <= base_start or not dependents.get(row.task_id):
            continue
        base_end = base.get(row.task_id, (None, None, None))[2]
        shifted_blockers = [
            b for b in blockers_of.get(row.task_id, ())
            if b in current and b in base_starts
            and (current[b].start_sprint or 0) > base_starts[b]
        ]
        if inputs.last_reported_sprint and base_end is not None and base_end <= inputs.last_reported_sprint:
            cause, cause_text = "own_slip", (
                f"не закрыта к концу спринта {inputs.last_reported_sprint}, как было в плане"
            )
        elif shifted_blockers:
            cause, cause_text = "dependency", f"сдвинулась блокирующая {', '.join(shifted_blockers)}"
        else:
            cause, cause_text = "capacity", "ресурс перераспределён после отклонений других задач"
        alerts.append(
            AlertRow(
                sprint_no=row.start_sprint,
                level="yellow",
                alert_type="cascade_shift",
                entity_type="task",
                entity_id=row.task_id,
                message=(
                    f"{row.task_id} сдвинулась со спринта {base_start} на {row.start_sprint} "
                    f"и тянет {len(dependents[row.task_id])} зависимых задач — {cause_text}"
                ),
                payload={
                    "baseline_start_sprint": base_start,
                    "new_start_sprint": row.start_sprint,
                    "dependents": sorted(dependents[row.task_id]),
                    "cause": cause,
                    "cause_text": cause_text,
                    "reported_sprint": inputs.last_reported_sprint,
                },
            )
        )
    return alerts


def _build_kpis(
    inputs: Inputs,
    schedule: list[ScheduleRow],
    baseline_starts: dict[str, int],
    *,
    as_of_sprint: int = 0,
) -> list[KpiRow]:
    """KPI по формулам ТЗ (ADR-023). Прогноз и факт — разные строки (`kind`).

    * «Процент выполнения квартального плана» = инициативы, завершённые в
      течение 12 недель / инициативы, включённые в первоначальный план × 100%.
      Включена в план = ВСЕ её живые задачи Недели 0 стоят в квартале: только
      такую инициативу план обещал завершить. Прогноз — по текущему прогону,
      факт — по загруженным результатам спринтов.
    * «Выполнение плана спринта» = фактически выполненные SP / первоначально
      запланированные SP × 100%. Запланировано на спринт = SP задач, которые
      первоначальный план закрывает в этом спринте. Для спринтов с загруженным
      фактом — факт, для остальных — прогноз текущего прогона.
    * Bus Factor — по компетенциям (ТЗ): минимум носителей по навыкам,
      чья роль нужна бэклогу. Это состояние, а не прогноз: kind = actual.

    Первоначальный план — канонический базовый прогон; пересчёт его не меняет.
    """
    current = {row.task_id: row for row in schedule}
    sp_of: dict[str, Decimal] = {task_id: sp for task_id, _st, sp, _rem in inputs.all_tasks}
    sp_of.update({task.task_id: task.estimation_sp for task in inputs.tasks})
    status_of = {task_id: status for task_id, status, _sp, _rem in inputs.all_tasks}
    prodf_of = dict(inputs.task_prodf)
    prodf_of.update({task.task_id: task.prodf_id for task in inputs.tasks})

    if inputs.baseline_schedule:
        base = dict(inputs.baseline_schedule)
        base_source = "канонический базовый прогон"
    else:
        base = {row.task_id: (row.decision, row.start_sprint, row.end_sprint) for row in schedule}
        base_source = "этот прогон" if as_of_sprint == 0 else "этот прогон (базового ещё нет)"

    def pct(numerator: Decimal | int, denominator: Decimal | int) -> Decimal:
        if not denominator:
            return Decimal("0.00")
        return (Decimal(numerator) / Decimal(denominator) * 100).quantize(Decimal("0.01"))

    # --- процент выполнения квартального плана ----------------------------
    tasks_of: dict[str, list[str]] = defaultdict(list)
    for task_id in base:
        tasks_of[prodf_of.get(task_id, "?")].append(task_id)
    committed = sorted(
        prodf_id for prodf_id, ids in tasks_of.items() if all(base[t][0] == "in_quarter" for t in ids)
    )
    partial = sorted(
        prodf_id for prodf_id, ids in tasks_of.items()
        if prodf_id not in committed and any(base[t][0] == "in_quarter" for t in ids)
    )

    def done(task_id: str) -> bool:
        return status_of.get(task_id) == DONE_STATUS

    on_track = sorted(
        prodf_id for prodf_id in committed
        if all(done(t) or (t in current and current[t].decision == "in_quarter") for t in tasks_of[prodf_id])
    )
    completed = sorted(prodf_id for prodf_id in committed if all(done(t) for t in tasks_of[prodf_id]))
    low, high = KPI_TARGETS["pi_predictability"]
    common = {
        "formula": "инициативы, завершённые в течение 12 недель / инициативы, включённые в "
        "первоначальный план × 100%",
        "committed_initiatives": committed,
        "committed_n": len(committed),
        "partial_initiatives": partial,
        "baseline_source": base_source,
    }
    kpis: list[KpiRow] = [
        KpiRow(
            sprint_no=inputs.sprint_count,
            kpi_code="pi_predictability",
            value=pct(len(on_track), len(committed)),
            target_min=low,
            target_max=high,
            details={
                **common,
                "on_track_initiatives": on_track,
                "note": "прогноз: все задачи инициативы выполнены или стоят в квартале в этом "
                "прогоне. Частично включённые в план инициативы в знаменатель не входят — "
                "план не обещал их завершить",
            },
            kind="forecast",
        )
    ]
    if inputs.last_reported_sprint > 0:
        kpis.append(
            KpiRow(
                sprint_no=inputs.sprint_count,
                kpi_code="pi_predictability",
                value=pct(len(completed), len(committed)),
                target_min=low,
                target_max=high,
                details={
                    **common,
                    "completed_initiatives": completed,
                    "reported_through_sprint": inputs.last_reported_sprint,
                    "note": f"факт по загруженным спринтам 1–{inputs.last_reported_sprint}: "
                    f"выполнены все задачи инициативы. До конца квартала значение промежуточное",
                },
                kind="actual",
            )
        )

    # --- выполнение плана спринта ----------------------------------------
    planned_sp: dict[int, Decimal] = defaultdict(Decimal)
    planned_ids: dict[int, list[str]] = defaultdict(list)
    for task_id, (decision, _start, end) in base.items():
        if decision == "in_quarter" and end is not None:
            planned_sp[end] += sp_of.get(task_id, Decimal("0"))
            planned_ids[end].append(task_id)
    low, high = KPI_TARGETS["say_do_ratio"]
    for sprint_no in range(1, inputs.sprint_count + 1):
        need = planned_sp.get(sprint_no, Decimal("0"))
        if sprint_no <= inputs.last_reported_sprint:
            ids = sorted(inputs.done_in_sprint.get(sprint_no, frozenset()))
            kind, note = "actual", "факт: SP задач, отмеченных выполненными в загрузке за этот спринт"
        else:
            ids = sorted(
                task_id for task_id, row in current.items()
                if row.decision == "in_quarter" and row.end_sprint == sprint_no
            )
            kind, note = "forecast", "прогноз: SP задач, которые этот прогон закрывает в этом спринте"
        got = sum((sp_of.get(task_id, Decimal("0")) for task_id in ids), Decimal("0"))
        if need <= 0:
            note += "; на спринт первоначально ничего не планировали — показатель 100%"
        kpis.append(
            KpiRow(
                sprint_no=sprint_no,
                kpi_code="say_do_ratio",
                value=pct(got, need) if need > 0 else Decimal("100.00"),
                target_min=low,
                target_max=high,
                details={
                    "formula": "фактически выполненные SP / первоначально запланированные SP × 100%",
                    "planned_sp": str(need),
                    "done_sp": str(got),
                    "planned_tasks": sorted(planned_ids.get(sprint_no, [])),
                    "done_tasks": ids,
                    "note": note,
                },
                kind=kind,
            )
        )

    # --- Bus Factor по компетенциям ---------------------------------------
    in_demand = [(name, bf) for name, bf, used, _sole in inputs.skill_bus_factor if used]
    low, high = KPI_TARGETS["bus_factor"]
    kpis.append(
        KpiRow(
            sprint_no=inputs.sprint_count,
            kpi_code="bus_factor",
            value=Decimal(min((bf for _name, bf in in_demand), default=0)),
            target_min=low,
            target_max=high,
            details={
                "method": "по компетенциям: для каждого заявленного навыка — число инженеров, "
                "которые им владеют; значение — минимум по навыкам, чья роль нужна бэклогу",
                "competencies_n": len(inputs.skill_bus_factor),
                "single_holder_n": sum(1 for _n, bf, _u, _s in inputs.skill_bus_factor if bf == 1),
                "critical_n": sum(1 for _n, _bf, _u, sole in inputs.skill_bus_factor if sole),
                "critical": sorted(name for name, _bf, _u, sole in inputs.skill_bus_factor if sole),
                "roles_without_staff": [name for name, bf, _demand in inputs.bus_factor if bf == 0],
                "note": "«критично» — единственный носитель навыка и единственный специалист "
                "своей роли: выпал — работу не подхватит никто. Роли без людей в штате — "
                "отдельная проблема найма",
            },
            kind="actual",
        )
    )
    return kpis


def _build_states(
    inputs: Inputs, schedule: list[ScheduleRow], as_of_sprint: int
) -> list[StateRow]:
    """Слепок ВСЕХ задач (и Done тоже): это временно́й саттелит, а не план."""
    decisions = {row.task_id: row for row in schedule}
    states: list[StateRow] = []
    for task_id, status, sp, remaining in inputs.all_tasks:
        if status == DONE_STATUS:
            states.append(
                StateRow(task_id, as_of_sprint, DONE_STATUS, Decimal("0"), Decimal("0"), None)
            )
            continue
        row = decisions.get(task_id)
        if row is None:
            states.append(StateRow(task_id, as_of_sprint, status, remaining, sp, None))
        elif row.decision == "in_quarter":
            states.append(StateRow(task_id, as_of_sprint, status, remaining, sp, row.end_sprint))
        elif row.decision == "cancelled":
            states.append(StateRow(task_id, as_of_sprint, "Cancelled", remaining, sp, None))
        else:
            states.append(StateRow(task_id, as_of_sprint, "Deferred", remaining, sp, None))
    return states


# ---------------------------------------------------------------------------
#  ЗАПИСЬ: весь контракт одной транзакцией
# ---------------------------------------------------------------------------
def write_plan(plan: Plan) -> int:
    """Пишет прогон и весь контракт в одной транзакции. Возвращает `run_id`.

    `is_loan` не пишем никогда — это генерируемая колонка (см. RUNBOOK, раздел 6).
    """
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO plan_runs (pi_id, as_of_sprint, algorithm, params, status, note,
                                   actuals_upload_id)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING run_id
            """,
            (
                plan.pi_id,
                plan.as_of_sprint,
                ALGORITHM,
                json.dumps(plan.params, ensure_ascii=False),
                plan.status,
                plan.note,
                plan.actuals_upload_id,
            ),
        )
        row = cur.fetchone()
        if row is None:  # INSERT ... RETURNING без строки — такого быть не может
            raise RuntimeError("plan_runs не вернул run_id")
        run_id = int(row["run_id"])

        if plan.baseline:
            cur.executemany(
                """
                INSERT INTO plan_baseline (run_id, task_id, planned_sp, committed)
                VALUES (%s, %s, %s, %s)
                """,
                [(run_id, row.task_id, row.planned_sp, row.committed) for row in plan.baseline],
            )

        cur.executemany(
            """
            INSERT INTO plan_task_schedule
                (run_id, task_id, start_sprint, end_sprint, forecast_end_date,
                 decision, decision_reason, reason_code, reason_text, reason_details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            [
                (
                    run_id,
                    row.task_id,
                    row.start_sprint,
                    row.end_sprint,
                    row.forecast_end_date,
                    row.decision,
                    row.decision_reason,
                    row.reason_code,
                    row.reason_text,
                    json.dumps(row.reason_details, ensure_ascii=False),
                )
                for row in plan.schedule
            ],
        )

        if plan.sp_shares:
            cur.executemany(
                "INSERT INTO plan_task_sp (run_id, task_id, sprint_no, sp) VALUES (%s, %s, %s, %s)",
                [(run_id, task_id, sprint_no, sp) for task_id, sprint_no, sp in plan.sp_shares],
            )

        if plan.assignments:
            cur.executemany(
                """
                INSERT INTO plan_assignments
                    (run_id, task_id, sprint_no, engineer_id, role_id, hours,
                     home_team_id, serving_team_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id,
                        row.task_id,
                        row.sprint_no,
                        row.engineer_id,
                        row.role_id,
                        row.hours,
                        row.home_team_id,
                        row.serving_team_id,
                    )
                    for row in plan.assignments
                ],
            )

        cur.executemany(
            """
            INSERT INTO task_state
                (run_id, task_id, as_of_sprint, status, remaining_hh, remaining_sp,
                 forecast_end_sprint)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    run_id,
                    row.task_id,
                    row.as_of_sprint,
                    row.status,
                    row.remaining_hh,
                    row.remaining_sp,
                    row.forecast_end_sprint,
                )
                for row in plan.states
            ],
        )

        if plan.alerts:
            cur.executemany(
                """
                INSERT INTO alerts
                    (run_id, sprint_no, level, alert_type, entity_type, entity_id,
                     message, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                [
                    (
                        run_id,
                        row.sprint_no,
                        row.level,
                        row.alert_type,
                        row.entity_type,
                        row.entity_id,
                        row.message,
                        json.dumps(row.payload, ensure_ascii=False),
                    )
                    for row in plan.alerts
                ],
            )

        cur.executemany(
            """
            INSERT INTO kpi_snapshots
                (run_id, sprint_no, kpi_code, value, target_min, target_max, details, kind)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            [
                (
                    run_id,
                    row.sprint_no,
                    row.kpi_code,
                    row.value,
                    row.target_min,
                    row.target_max,
                    json.dumps(row.details, ensure_ascii=False),
                    row.kind,
                )
                for row in plan.kpis
            ],
        )
    return run_id