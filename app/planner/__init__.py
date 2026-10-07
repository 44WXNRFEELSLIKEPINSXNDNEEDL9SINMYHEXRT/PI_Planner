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

* часы — только `v_task_remaining_hh.remaining_hours` (ETC по роли либо
  предварительная смета без факта, ADR-026); расхождения трёх источников проверяются и уезжают в
  `plan_runs.params` — этого требует ответ организаторов №4;
* замещения ролей отклонены организаторами (ответ №2, ADR-010), поэтому
  исполнители берутся ТОЛЬКО из родных строк `v_engineer_role_coverage`, и
  КАНДИДАТЫ на роль — тоже из этой вьюхи, а не из `engineers.role_id`
  (ADR-012): вьюха остаётся единственным источником правды о паре
  «инженер × роль», включая `efficiency`;
* `efficiency` (множитель часов замещающего) применяется к потребности:
  чтобы закрыть `remaining_hours` работы, исполнителю нужно
  `remaining_hours × efficiency` своих часов. Сейчас в данных везде `1.00`,
  поэтому поведение не меняется, но формула уже верна (ADR-016);
* фонд часов — по орбитам: сначала своё ядро, невыбранный остаток уходит в заём
  (`is_loan` считает СУБД, ADR-001). Одна строка `plan_assignments` берёт часы
  с одной орбиты; `home_team_id` входит в ключ, поэтому один человек может
  отдать часы с нескольких орбит на задачу в том же спринте;
* если незакрытая задача исчерпала расчётную смету, но ETC не сообщён,
  остаток считается неизвестным; фиктивные назначения не создаются;
* в закрытые спринты план не пишется: при `as_of_sprint = k` нижняя граница
  старта — `max(1, k, earliest_start_sprint)` (ADR-014).

Режимы (по умолчанию — как в приёмке M2, оба параметра уезжают в
`plan_runs.params`):

* `dependency_mode`: `finish_start` (по умолчанию, ADR-028) —
  `start(blocked) ≥ end(blocking) + gap`; `start_start` —
  `start(blocked) ≥ start(blocking) + gap` (ADR-013);
* `initiative_mode`: `greedy` (по умолчанию) — задача решается по отдельности,
  частично закрытая инициатива допустима; `atomic` — пробная упаковка всей
  инициативы с откатом: не влезла хоть одна задача, переносится вся
  инициатива (ADR-013).


Состав пакета (рефакторинг без изменения поведения, волна 3):

* `constants` — Константы планировщика: версия алгоритма, коды причин, режимы, нормы KPI.
* `queries` — SQL чтения входа планировщика. Всё — к витринам: планировщик не знает ядро изнутри.
* `model` — Вход и выход планировщика: что прочитано (`Inputs`) и что ляжет в контракт (`Plan`).
* `fmt` — Форматирование чисел и слов для текстов причин.
* `funds` — Фонд часов и SP: единственное место, где ресурсы списываются и возвращаются.
* `graph` — Живой граф зависимостей на дату прогона (DA-10).
* `loading` — Чтение входа планировщика из базы: сессия строго read-only.
* `alerts` — Алерты прогона: дефицит ролей, срыв квартала, каскадные сдвиги.
* `kpi` — KPI по формулам ТЗ и слепок состояния задач.
* `core` — Ядро: чистая функция `build_plan` (вход -> `Plan`, без обращений к базе).
* `writer` — Запись прогона и всего контракта одной транзакцией.
"""
from __future__ import annotations

from app import db  # noqa: F401  (тесты подменяют planner.db.*)

from app.planner.constants import (  # noqa: F401
    ALGORITHM,
    CANCEL_REASON,
    DEFERRED_REASON,
    DEFERRED_REASON_BLOCKED,
    DEPENDENCY_MODES,
    DEPENDENCY_MODE_FINISH_START,
    DEFAULT_DEPENDENCY_MODE,
    DEFAULT_PRIORITY_STRATEGY,
    PRIORITY_COMPLETION_FIRST,
    PRIORITY_MAX,
    PRIORITY_STRATEGIES,
    PRIORITY_TASK,
    PRIORITY_WEIGHTED,
    DEPENDENCY_MODE_START_START,
    DONE_STATUS,
    EFFICIENCY_NOTE,
    ESTIMATE_SOURCE,
    FORMULA_VERSION,
    INITIATIVE_MODES,
    INITIATIVE_MODE_ATOMIC,
    INITIATIVE_MODE_GREEDY,
    KPI_TARGETS,
    OBJECTIVE,
    OBJECTIVE_NOTE,
    REASON_ATOMIC,
    REASON_BLOCKED,
    REASON_ETC_REQUIRED,
    REASON_GRAPH_HORIZON,
    REASON_NOT_FEASIBLE,
    REASON_PI_CLOSED,
    REASON_PLANNED,
    REASON_ROLE_HOURS,
    REASON_ROLE_NOT_IN_STAFF,
    REASON_SKILL_UNAVAILABLE,
    REASON_TEAM_SP,
    SUBSTITUTION_MODE,
)
from app.planner.local_search.solver import ALGORITHM_LOCAL_SEARCH  # noqa: F401
from app.planner.queries import (  # noqa: F401
    ALL_DEPS_SQL,
    ALL_TASKS_SQL,
    BASELINE_SCHEDULE_SQL,
    BASELINE_STARTS_SQL,
    BUS_FACTOR_SQL,
    COVERAGE_SQL,
    DONE_IN_SPRINT_SQL,
    ENGINEERS_SQL,
    ENGINEER_SKILLS_SQL,
    ESTIMATE_CONFLICT_SQL,
    ESTIMATE_MISMATCH_SQL,
    LAST_UPLOAD_SQL,
    LIVE_DEPS_SQL,
    LIVE_TASKS_SQL,
    PI_SQL,
    SKILL_BUS_FACTOR_SQL,
    SPRINTS_SQL,
    SUBSTITUTION_ROWS_SQL,
    TASK_DATES_SQL,
    TASK_PRODF_SQL,
    TASK_ROLES_SQL,
    TASK_SKILL_REVIEWS_SQL,
    TEAM_CAPACITY_SQL,
)
from app.planner.model import (  # noqa: F401
    AlertRow,
    CapacityRow,
    Assignment,
    BaselineRow,
    EngineerInput,
    Inputs,
    KpiRow,
    Plan,
    ScheduleRow,
    StateRow,
    TaskInput,
)
from app.planner.fmt import (  # noqa: F401
    _q,
    _sprints_word,
)
from app.planner.funds import (  # noqa: F401
    _Funds,
    _allocate_task,
    _candidate_engineers,
    _sp_shares,
    _spend_from,
)
from app.planner.graph import (  # noqa: F401
    _refresh_live_graph,
)
from app.planner.loading import (  # noqa: F401
    load_baseline_starts,
    load_inputs,
)
from app.planner.alerts import (  # noqa: F401
    _build_alerts,
)
from app.planner.kpi import (  # noqa: F401
    _build_kpis,
    _build_states,
)
from app.planner.priority import (  # noqa: F401
    check_strategy,
    effective_priority,
    order_tasks,
)
from app.planner.capacity import (  # noqa: F401
    CENT,
    DEFAULT_FOCUS_FACTOR,
    effective_capacity,
)
from app.planner.core import (  # noqa: F401
    _assemble,
    _round_hours,
    build_plan,
)
from app.planner.writer import (  # noqa: F401
    PlanValidationError,
    write_plan,
)
