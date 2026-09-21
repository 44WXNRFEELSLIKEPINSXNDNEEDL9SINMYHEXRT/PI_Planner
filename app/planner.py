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
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app import db

ALGORITHM = "greedy-priority-topo@1"
ESTIMATE_SOURCE = "matrix_column_sum"
SUBSTITUTION_MODE = "rejected"
# Минимальное назначение: контракт требует hours > 0, а остаток может быть нулевым.
SYMBOLIC_HOURS = Decimal("0.01")
DEFERRED_REASON = "M2"  # Отсутствие ресурсов
DEFERRED_REASON_BLOCKED = "M3"  # Отсутствует готовность смежных команд
DONE_STATUS = "Done"

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
    "bus_factor": (Decimal("1"), None),
}

# ---------------------------------------------------------------------------
#  Запросы на чтение. Все — к витринам: планировщик не знает ядро изнутри.
# ---------------------------------------------------------------------------
PI_SQL = """
SELECT pi_id, sprint_count, fte_hours_per_sprint
FROM pi_periods
ORDER BY pi_id
LIMIT 1
"""

SPRINTS_SQL = """
SELECT sprint_no, start_date, end_date
FROM sprints
WHERE pi_id = %s
ORDER BY sprint_no
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


def load_inputs() -> Inputs:
    """Читает всё, что нужно для плана. Только SELECT: сессия read-only."""
    pi = db.query_one(PI_SQL)
    if not pi:
        raise RuntimeError("pi_periods пуст: сначала залейте схему, seed и витрины (docs/RUNBOOK.md)")

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

    return Inputs(
        pi_id=pi["pi_id"],
        sprint_count=int(pi["sprint_count"]),
        fte_hours_per_sprint=int(pi["fte_hours_per_sprint"]),
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
            row["sprint_no"]: (row["start_date"], row["end_date"])
            for row in db.query_dicts(SPRINTS_SQL, (pi["pi_id"],))
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
        self.engineers: dict[str, EngineerInput] = {e.engineer_id: e for e in inputs.engineers}
        self._budget: dict[tuple[str, str], Decimal] = {
            (engineer.engineer_id, team_id): rate * self.fte
            for engineer in inputs.engineers
            for team_id, rate in engineer.orbits.items()
        }
        self._spent: dict[tuple[str, str, int], Decimal] = defaultdict(Decimal)
        self.used_sp: dict[tuple[str, int], Decimal] = defaultdict(Decimal)

    # ---- часы ------------------------------------------------------------
    def orbit_left(self, engineer_id: str, team_id: str, sprint_no: int) -> Decimal:
        budget = self._budget.get((engineer_id, team_id), Decimal("0"))
        return budget - self._spent[(engineer_id, team_id, sprint_no)]

    def total_left(self, engineer_id: str, sprint_no: int) -> Decimal:
        engineer = self.engineers[engineer_id]
        spent = sum(
            (self._spent[(engineer_id, team_id, sprint_no)] for team_id in engineer.orbits),
            Decimal("0"),
        )
        return engineer.total_capacity_rate * self.fte - spent

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


# ---------------------------------------------------------------------------
#  ЧИСТАЯ ЛОГИКА: вход → план
# ---------------------------------------------------------------------------
def build_plan(
    inputs: Inputs,
    as_of_sprint: int = 0,
    baseline_starts: dict[str, int] | None = None,
    dependency_mode: str = DEPENDENCY_MODE_START_START,
    initiative_mode: str = INITIATIVE_MODE_GREEDY,
) -> Plan:
    """Строит план. Ни одного обращения к базе: всё, что нужно, уже во `Inputs`.

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
    deferred: dict[str, str] = {}

    def release(task_id: str) -> None:
        """Снять задачу с плана: вернуть в фонд её SP и часы."""
        task = by_id[task_id]
        funds.free(placed.pop(task_id, []))
        if task_id in starts:
            funds.release_sp(task.team_id, starts.pop(task_id), task.estimation_sp)
        ends.pop(task_id, None)

    def release_initiative(prodf_id: str) -> list[str]:
        """Откат ВСЕЙ инициативы: вернуть в фонд её SP и часы (ADR-013)."""
        rolled = [
            task.task_id for task in ordered if task.prodf_id == prodf_id and task.task_id in starts
        ]
        for task_id in rolled:
            release(task_id)
        return rolled

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

    def place(task: TaskInput, lower: int) -> bool:
        """Поставить задачу в минимальный подходящий спринт. False — не влезла.

        Часы раскладываем первыми, SP проверяем по ФАКТИЧЕСКОМУ спринту старта:
        инвариант `SP_OVERFLOW` группирует SP по `start_sprint`, поэтому если
        задача начала работать в спринте 3, ёмкость нужна именно там.
        """
        capacity = inputs.team_sp_per_sprint.get(task.team_id, Decimal("0"))
        for candidate in range(max(replan_floor, lower), inputs.sprint_count + 1):
            result = _allocate_task(
                task, candidate, funds, by_role, inputs.sprint_count, inputs.coverage
            )
            if result is None:
                continue
            rows, start_used, end_sprint = result
            if funds.used_sp[(task.team_id, start_used)] + task.estimation_sp > capacity:
                funds.free(rows)  # SP не влезли в спринт старта — откат и пробуем позже
                continue
            funds.take_sp(task.team_id, start_used, task.estimation_sp)
            starts[task.task_id] = start_used
            ends[task.task_id] = end_sprint
            placed[task.task_id] = rows
            deferred.pop(task.task_id, None)
            return True
        return False

    # ---- проход 1: обход в порядке приоритетов ----------------------------
    if initiative_mode == INITIATIVE_MODE_ATOMIC:
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
    else:
        for task in ordered:
            if not place(task, lower_bound(task)):
                deferred[task.task_id] = DEFERRED_REASON

    # ---- проход 2: перенесённая блокирующая тянет за собой ----------------
    # Ставить зависимую задачу в квартал нельзя: блокирующая в него не попала.
    # Инвариант `DEPENDENCY_BLOCKER_DEFERRED` проверяет это независимо от кода.
    changed = True
    while changed:
        changed = False
        for task in ordered:
            if task.task_id in deferred:
                continue
            for blocking, _gap in deps_by_blocked.get(task.task_id, ()):
                if blocking not in deferred:
                    continue
                release(task.task_id)
                deferred[task.task_id] = blocked_reason(task)
                if initiative_mode == INITIATIVE_MODE_ATOMIC:
                    release_initiative(task.prodf_id)  # инициатива — целиком
                changed = True
                break

    # ---- проход 3: догон зазоров, если блокирующая уехала позже ------------
    for _ in range(len(ordered) + 1):
        violations = [
            (blocking, blocked, gap)
            for blocking, blocked, gap in inputs.deps
            if blocking in starts
            and blocked in starts
            and starts[blocked] < ready_from(blocking, gap)
        ]
        if not violations:
            break
        for _blocking, blocked, gap in violations:
            task = by_id[blocked]
            release(blocked)
            if not place(task, max(lower_bound(task), ready_from(_blocking, gap))):
                deferred[blocked] = blocked_reason(task)
                if initiative_mode == INITIATIVE_MODE_ATOMIC:
                    release_initiative(task.prodf_id)

    # ---- расписание -------------------------------------------------------
    schedule: list[ScheduleRow] = []
    for task in ordered:
        if task.task_id in deferred:
            schedule.append(
                ScheduleRow(task.task_id, None, None, None, "deferred_next_pi", deferred[task.task_id])
            )
            continue
        end_sprint = ends[task.task_id]
        end_date = inputs.sprints.get(end_sprint, (None, None))[1]
        schedule.append(
            ScheduleRow(task.task_id, starts[task.task_id], end_sprint, end_date, "in_quarter", None)
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
        },
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
) -> Plan:
    """Собирает `Plan`: алерты, KPI, базовая линия, слепок состояния, params.

    `modes` — режимы прогона (ADR-013/014): уезжают в `plan_runs.params`, чтобы
    у каждого результата было объяснение, по каким правилам он получен.
    """
    by_id = {task.task_id: task for task in inputs.tasks}
    in_quarter = [row for row in schedule if row.decision == "in_quarter"]
    deferred = [row for row in schedule if row.decision != "in_quarter"]

    alerts = _build_alerts(inputs, schedule, baseline_starts)
    kpis = _build_kpis(inputs, schedule, baseline_starts)
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

    params: dict[str, Any] = {
        "algorithm": ALGORITHM,
        "estimate_source": ESTIMATE_SOURCE,
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
    }
    params.update(modes or {})
    note = (
        f"{len(in_quarter)} из {len(inputs.tasks)} живых задач в квартале, "
        f"{len(deferred)} перенесено (M2/M3); инициатив целиком "
        f"{len(complete_initiatives)} из {len(by_initiative)}"
        f"{f', частично {len(partial_initiatives)}' if partial_initiatives else ''}; "
        f"алертов {len(alerts)}; займов {loan_hh} ЧЧ; замещения отклонены (ответ №2, ADR-010)"
    )
    return Plan(
        pi_id=inputs.pi_id,
        as_of_sprint=as_of_sprint,
        status="ok" if in_quarter else "infeasible",
        note=note,
        params=params,
        schedule=tuple(schedule),
        assignments=tuple(assignments),
        alerts=tuple(alerts),
        kpis=tuple(kpis),
        baseline=tuple(baseline),
        states=tuple(states),
    )


def _build_alerts(
    inputs: Inputs, schedule: list[ScheduleRow], baseline_starts: dict[str, int]
) -> list[AlertRow]:
    """Алерты трёх уровней из онбординга.

    red — инициатива не укладывается в квартал; orange — по роли не хватает
    людей; yellow — каскадный сдвиг на пересчёте (появляется, когда есть с чем
    сравнивать: расписание базового прогона).
    """
    by_id = {task.task_id: task for task in inputs.tasks}
    fte = Decimal(inputs.fte_hours_per_sprint)
    alerts: list[AlertRow] = []

    # --- orange: роли не хватает людей (замещений нет — строгий режим) -----
    demand: dict[int, Decimal] = defaultdict(Decimal)
    tasks_of_role: dict[int, list[str]] = defaultdict(list)
    names: dict[int, str] = {}
    first_sprint: dict[int, int] = {}
    for task in inputs.tasks:
        for role_id, hours in task.remaining.items():
            if hours <= 0:
                continue
            demand[role_id] += hours
            tasks_of_role[role_id].append(task.task_id)
            names[role_id] = task.role_names.get(role_id, str(role_id))
            first_sprint[role_id] = min(
                first_sprint.get(role_id, task.earliest_start_sprint), task.earliest_start_sprint
            )

    supply: dict[int, Decimal] = defaultdict(Decimal)
    for engineer in inputs.engineers:
        supply[engineer.role_id] += engineer.total_capacity_rate * fte * inputs.sprint_count

    for role_id in sorted(demand):
        need = demand[role_id]
        have = supply.get(role_id, Decimal("0"))
        if need <= have:
            continue
        alerts.append(
            AlertRow(
                sprint_no=max(1, first_sprint[role_id]),
                level="orange",
                alert_type="role_deficit",
                entity_type="role",
                entity_id=names[role_id],
                message=(
                    f"роль «{names[role_id]}»: {need} ЧЧ на {len(tasks_of_role[role_id])} задачах, "
                    f"фонд {have} ЧЧ — нужен наём или дообучение"
                ),
                payload={
                    "role_id": role_id,
                    "role_name": names[role_id],
                    "demand_hh": str(need),
                    "supply_hh": str(have),
                    "tasks": sorted(tasks_of_role[role_id]),
                    "verdict": "НАЙМ: закрыть некем" if have == 0 else "НАЙМ: не хватает часов",
                    "reason": "замещения ролей отклонены организаторами (ответ №2, ADR-010)",
                },
            )
        )

    # --- red: инициатива не укладывается в квартал -------------------------
    deferred_of: dict[str, list[str]] = defaultdict(list)
    for row in schedule:
        if row.decision != "in_quarter":
            deferred_of[by_id[row.task_id].prodf_id].append(row.task_id)
    for prodf_id in sorted(deferred_of):
        task_ids = sorted(deferred_of[prodf_id])
        hh = sum((by_id[task_id].demand_hh for task_id in task_ids), Decimal("0"))
        alerts.append(
            AlertRow(
                sprint_no=inputs.sprint_count,
                level="red",
                alert_type="deadline_miss",
                entity_type="initiative",
                entity_id=prodf_id,
                message=(
                    f"{prodf_id}: {len(task_ids)} задач перенесено в следующий PI, "
                    f"{hh} ЧЧ не закрыто — инициатива не уложится в квартал"
                ),
                payload={"deferred_tasks": task_ids, "deferred_hh": str(hh)},
            )
        )

    # --- yellow: сдвиг задачи, у которой есть зависимые ---------------------
    dependents: dict[str, list[str]] = defaultdict(list)
    for blocking, blocked, _gap in inputs.deps:
        dependents[blocking].append(blocked)
    for row in schedule:
        base = baseline_starts.get(row.task_id)
        if row.decision != "in_quarter" or base is None or row.start_sprint is None:
            continue
        if row.start_sprint <= base or not dependents.get(row.task_id):
            continue
        alerts.append(
            AlertRow(
                sprint_no=row.start_sprint,
                level="yellow",
                alert_type="cascade_shift",
                entity_type="task",
                entity_id=row.task_id,
                message=(
                    f"{row.task_id} сдвинулась со спринта {base} на {row.start_sprint} "
                    f"и тянет {len(dependents[row.task_id])} зависимых задач"
                ),
                payload={
                    "baseline_start_sprint": base,
                    "new_start_sprint": row.start_sprint,
                    "dependents": sorted(dependents[row.task_id]),
                },
            )
        )
    return alerts


def _build_kpis(
    inputs: Inputs, schedule: list[ScheduleRow], baseline_starts: dict[str, int]
) -> list[KpiRow]:
    """Три KPI с нормами из онбординга.

    `pi_predictability` — доля инициатив, которые попадут в квартал целиком:
    знаменатель — инициативы с живыми задачами (все они были в плане Недели 0),
    числитель — те, где ВСЕ живые задачи получили `in_quarter`.

    `say_do_ratio` — по каждому спринту: сколько SP из обещанного на этот спринт
    в нём действительно стартует. Обещание — расписание базового прогона
    (`plan_baseline`, ADR-004). На первом прогоне обещание и есть план, поэтому
    100%; на пересчёте задача, уехавшая из своего спринта, роняет показатель
    именно того спринта, который был обещан.
    """
    by_id = {task.task_id: task for task in inputs.tasks}
    in_quarter = {row.task_id: row.start_sprint for row in schedule if row.decision == "in_quarter"}

    by_initiative: dict[str, list[str]] = defaultdict(list)
    for task in inputs.tasks:
        by_initiative[task.prodf_id].append(task.task_id)
    completed = {
        prodf_id
        for prodf_id, task_ids in by_initiative.items()
        if all(task_id in in_quarter for task_id in task_ids)
    }
    predictability = (
        (Decimal(len(completed)) / Decimal(len(by_initiative)) * 100).quantize(Decimal("0.01"))
        if by_initiative
        else Decimal("0")
    )

    kpis: list[KpiRow] = [
        KpiRow(
            sprint_no=inputs.sprint_count,
            kpi_code="pi_predictability",
            value=predictability,
            target_min=KPI_TARGETS["pi_predictability"][0],
            target_max=KPI_TARGETS["pi_predictability"][1],
            details={
                "planned_initiatives": sorted(by_initiative),
                "completed_initiatives": sorted(completed),
                "planned_n": len(by_initiative),
                "completed_n": len(completed),
                "deferred_tasks_n": len(schedule) - len(in_quarter),
                "note": "инициатива выполнена, только если ВСЕ её живые задачи попали в квартал",
            },
        )
    ]

    current = {
        row.task_id: row.start_sprint
        for row in schedule
        if row.decision == "in_quarter" and row.start_sprint is not None
    }
    # Обещание: расписание базового прогона; задачи, которых там не было
    # (добавились в квартал позже), считаем обещанными на их текущий спринт.
    promise = {**current, **baseline_starts}
    promised: dict[int, Decimal] = defaultdict(Decimal)
    for task_id, start_sprint in promise.items():
        if task_id in by_id:
            promised[start_sprint] += by_id[task_id].estimation_sp

    for sprint_no in range(1, inputs.sprint_count + 1):
        need = promised.get(sprint_no, Decimal("0"))
        done = sum(
            (
                by_id[task_id].estimation_sp
                for task_id, start_sprint in promise.items()
                if start_sprint == sprint_no
                and task_id in by_id
                and current.get(task_id) == sprint_no
            ),
            Decimal("0"),
        )
        kpis.append(
            KpiRow(
                sprint_no=sprint_no,
                kpi_code="say_do_ratio",
                value=(
                    (done / need * 100).quantize(Decimal("0.01"))
                    if need > 0
                    else Decimal("100.00")  # нечего было обещать — нечего и проваливать
                ),
                target_min=KPI_TARGETS["say_do_ratio"][0],
                target_max=KPI_TARGETS["say_do_ratio"][1],
                details={
                    "promised_sp": str(need),
                    "delivered_sp": str(done),
                    "note": (
                        "сравниваем с обещанием Недели 0 (plan_baseline): задача, "
                        "уехавшая из своего спринта, роняет показатель этого спринта"
                    ),
                },
            )
        )

    kpis.append(
        KpiRow(
            sprint_no=inputs.sprint_count,
            kpi_code="bus_factor",
            value=Decimal(min((bf for _name, bf, _demand in inputs.bus_factor), default=0)),
            target_min=KPI_TARGETS["bus_factor"][0],
            target_max=KPI_TARGETS["bus_factor"][1],
            details={
                "roles": [
                    {"role_name": name, "bus_factor": bf, "demand_hh": str(demand)}
                    for name, bf, demand in inputs.bus_factor
                ],
                "critical": [name for name, bf, _demand in inputs.bus_factor if bf <= 1],
                "note": "значение — минимум по ролям со спросом, разбивка рядом",
            },
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
            INSERT INTO plan_runs (pi_id, as_of_sprint, algorithm, params, status, note)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s)
            RETURNING run_id
            """,
            (
                plan.pi_id,
                plan.as_of_sprint,
                ALGORITHM,
                json.dumps(plan.params, ensure_ascii=False),
                plan.status,
                plan.note,
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
                 decision, decision_reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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
                )
                for row in plan.schedule
            ],
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
                (run_id, sprint_no, kpi_code, value, target_min, target_max, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
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
                )
                for row in plan.kpis
            ],
        )
    return run_id