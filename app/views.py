"""Витрины для фронта: белый список источников и безопасная выборка строк.

Один маршрут `GET /api/views/{view}` вместо маршрута на каждый экран: витрин
больше, чем экранов, и они добавляются вместе с UI, а лейбл `route` в метриках
заморожен (ADR-018) — отдельный маршрут на витрину раздул бы его до числа
экранов. Витрины — параметр, а не путь.

Ответ — «строка витрины как есть» плюс конверт: `view`, `run_id`, `as_of`,
`count`, `columns`, `items`. Фронт сам собирает из строк нужные поля, а бэкенд
не переписывает одну и ту же витрину под каждый экран (ADR-019).

Что защищено:

* **имя витрины** — только из `SOURCES`: значение параметра в текст SQL не
  попадает вовсе, `?view=v_task_board;DROP TABLE` даёт 404;
* **`ORDER BY`** — только колонки из `orderable` этой витрины: имя колонки
  параметром не подставить, поэтому оно тоже берётся из белого списка;
* **`run_id`, `limit`, `offset`** — обычные параметры запроса (`%s::int`);
* **только `SELECT`** в read-only сессии (`app.db.query_dicts`) — испортить
  контракт планировщика этим маршрутом нельзя.

Чего в v1 сознательно нет: фильтров по колонкам. Данных мало (45 задач, 74
строки расписания, 43 алерта, 30 орбит) — клиентская фильтрация честнее
параметризации SQL под каждый экран. Появится нужда — добавим `where` по тому же
белому списку колонок, а не «универсальный» фильтр.

Внутренняя кухня ETL (`load_batches`, `dq_issues`, `role_aliases`,
`task_sequence`, оценки, `pi_periods`) в белый список не входит: наружу ей нечего
отдавать (docs/SCHEMA.md §3). Промежуточные вьюхи (`v_role_supply_hh`,
`v_backlog_demand`) тоже не отдаются — у экранов есть готовые витрины с вердиктом.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from app import db

# Потолок и значение по умолчанию для `limit`. 5000 — выше любой витрины
# контракта (самая большая — `v_satellite_capacity`, 238 строк): потолок нужен
# не «на вырост», а чтобы один запрос не вытащил всю базу, если фронт ошибётся.
LIMIT_DEFAULT = 500
LIMIT_MAX = 5000

# Прогон по умолчанию — последний удачный: «текущий» в терминах docs/SCHEMA.md §2.
LAST_OK_RUN_SQL = "SELECT MAX(run_id) AS run_id FROM plan_runs WHERE status = 'ok'"

# Колонки витрины — для пустого результата: у пустого списка строк нет ключей,
# а фронт должен знать форму ответа и в этом случае.
COLUMNS_SQL = """
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = %s
ORDER BY ordinal_position
"""


@dataclass(frozen=True)
class Source:
    """Разрешённый источник данных для фронта."""

    name: str  # имя в URL и в БД: v_task_board, plan_runs, …
    kind: str  # view | table
    screen: str  # экран UI, которому служит витрина
    order: str  # порядок по умолчанию; «-» перед колонкой = DESC
    orderable: tuple[str, ...]  # белый список колонок для `?order=`
    run_column: str | None = None  # колонка прогона; None — витрина не про прогон
    note: str = ""  # что важно знать фронту

    def as_dict(self) -> dict[str, Any]:
        """Описание витрины для `GET /api/views` — фронт видит контракт, а не угадывает."""
        return {
            "name": self.name,
            "kind": self.kind,
            "screen": self.screen,
            "run_column": self.run_column,
            "order": [col for col in self.order.split(",") if col],
            "orderable": list(self.orderable),
            "note": self.note,
        }


def _source(
    name: str,
    screen: str,
    order: str,
    orderable: tuple[str, ...],
    note: str = "",
    run_column: str | None = None,
) -> Source:
    """Регистрирует витрину и проверяет её на месте.

    Опечатка в колонке порядка падает при импорте, а не 500-й в бою: проверить
    белый список против живой базы тесты не могут (тесты идут без PostgreSQL).
    """
    columns = [col.strip().lstrip("-+") for col in order.split(",") if col.strip()]
    unknown = [col for col in columns if col not in orderable]
    if unknown:
        raise ValueError(f"{name}: колонки порядка вне белого списка: {unknown}")
    return Source(
        name=name,
        kind="view" if name.startswith("v_") else "table",
        screen=screen,
        # Порядок храним как задан: «-» — это DESC, а не украшение (потеря знака
        # переворачивала бы список прогонов и срез дефицита).
        order=",".join(col.strip() for col in order.split(",") if col.strip()),
        orderable=orderable,
        run_column=run_column,
        note=note,
    )
SOURCES: tuple[Source, ...] = (
    # ------------------------------------------------- справочный слой (SCHEMA.md §1)
    _source(
        "v_task_board",
        screen="Доска задач",
        order="priority_rung,task_id",
        orderable=(
            "task_id", "prodf_id", "br_id", "team_id", "summary", "status", "priority_rung",
            "rung", "estimation_sp", "estimated_hh_effective", "estimated_hh_declared",
            "estimated_hh_matrix_total", "remaining_hh", "estimate_disputed",
            "planned_start", "planned_end", "actual_start", "actual_end",
            "topo_order", "depth", "earliest_start_sprint", "on_critical_path",
        ),
        note="Денормализована: задача + инициатива + приоритет + остаток часов + место в графе.",
    ),
    _source(
        "tasks",
        screen="Доска задач",
        order="task_id",
        orderable=(
            "task_id", "prodf_id", "team_id", "status", "rung", "estimation_sp",
            "estimated_hh_effective", "estimated_hh_declared", "estimated_hh_matrix_total",
            "spent_time_declared", "created_at", "planned_start", "planned_end",
            "actual_start", "actual_end", "committed_week0",
        ),
        note="Поля, которых нет в v_task_board: результат по трём ролям и флаг committed_week0 "
        "(заполнен только у 8 задач Done — ADR-004, знаменатель KPI берётся не отсюда).",
    ),
    _source(
        "v_task_remaining_hh",
        screen="Доска задач",
        order="task_id,role_id",
        orderable=("task_id", "role_id", "estimated_hours", "spent_hours", "remaining_hours"),
        note="Строка на «задача × роль»: это не сумма часов задачи (ADR-002), часы ролей не складывать.",
    ),
    _source(
        "v_orbit_map",
        screen="Звёздная карта",
        order="engineer_id",
        orderable=(
            "engineer_id", "role_name", "role_group", "grade", "total_capacity_rate",
            "orbit_count", "bus_factor", "risk",
        ),
        note="Готовая витрина звёздной карты: `teams` и `skills` — массивы, 30 инженеров, "
        "у четырёх две орбиты (ADR-001). Раскладку графа считает фронт: координат в данных нет.",
    ),
    _source(
        "v_satellite_capacity",
        screen="Звёздная карта",
        order="engineer_id,sprint_no",
        orderable=(
            "engineer_id", "team_id", "role_id", "grade", "pi_id", "sprint_no",
            "start_date", "end_date", "capacity_rate", "is_shared_orbit", "length_days",
            "hours_own",
        ),
        note="Ёмкость инженера по спринтам: `hours_own` уже умножен на множитель своего "
        "спринта (у 7-го 0.5714), вручную фонд не пересчитывать (ADR-017).",
    ),


    _source(
        "v_engineer_role_coverage",
        screen="Роли и ёмкость",
        order="engineer_id,role_id",
        orderable=(
            "engineer_id", "role_id", "role_name", "is_native", "efficiency", "basis", "status",
        ),
        note="Какие роли закрывает инженер. Замещения отклонены организаторами (ADR-010): "
        "все 30 строк `is_native = true`.",
    ),
    _source(
        "v_team_capacity_sp",
        screen="Роли и ёмкость",
        order="team_id",
        orderable=(
            "team_id", "history_points", "avg_velocity", "focus_factor",
            "available_sp_per_sprint", "available_sp_per_pi",
        ),
        note="Ёмкость команды в SP. Для короткого спринта умножать на `v_sprint_fund_factor`, "
        "а не на число спринтов (ADR-017).",
    ),
    _source(
        "teams",
        screen="Роли и ёмкость",
        order="team_id",
        orderable=("team_id", "focus_factor"),
        note="Справочник команд: в витринах приходит только `team_id`, названия — здесь.",
    ),
    _source(
        "v_role_deficit",
        screen="Роли и ёмкость",
        order="team_id,role_name",
        orderable=("team_id", "role_name", "demand_hh", "supply_hh", "gap_hh", "verdict"),
        note="Главная аналитическая витрина: дефицит по «команда × роль» с вердиктом, 76 строк.",
    ),
    _source(
        "v_role_deficit_effective",
        screen="Роли и ёмкость",
        order="team_id,role_name",
        orderable=(
            "team_id", "role_name", "demand_hh", "supply_with_substitution_hh", "gap_hh",
            "verdict",
        ),
        note="То же с учётом замещения. Сравнивать со строгим `v_role_deficit`, "
        "а не показывать вместо него.",
    ),
    _source(
        "v_role_coverage_org",
        screen="Роли и ёмкость",
        order="-gap_hh,role_name",
        orderable=(
            "role_name", "demand_hh", "native_people", "people_incl_substitution",
            "supply_hh", "gap_hh", "verdict",
        ),
        note="Срез по компании: где нужен НАЁМ, а где хватит займов. Сейчас НАЙМ = 665 ЧЧ "
        "на 6 ролях (РП 449 + четыре 1С-роли 132 + поддержка 84).",
    ),
    _source(
        "v_bus_factor",
        screen="Роли и ёмкость",
        order="role_id",
        orderable=("role_id", "role_name", "role_group", "bus_factor", "demand_hh", "risk"),
        note="Незаменимость по ролям, включая роли, которых нет в штате: bus_factor 0 — "
        "роль есть в спросе, людей нет.",
    ),

    _source(
        "v_sprint_fund_factor",
        screen="Календарь и фонд",
        order="sprint_no",
        orderable=("pi_id", "sprint_no", "start_date", "end_date", "length_days", "factor"),
        note="Множитель фонда спринта: спринты 1–6 — 1.0000, 7-й (23–30.09, 8 дней) — 0.5714.",
    ),
    _source(
        "v_pi_fund_factor",
        screen="Календарь и фонд",
        order="pi_id",
        orderable=("pi_id", "sprint_length_days", "days_total", "factor"),
        note="Фонд всего PI: 6.5714 при 92 днях. Единственный источник правды для фонда — "
        "эта витрина и v_sprint_fund_factor, «92 / 14» не считать (ADR-017).",
    ),
    _source(
        "sprints",
        screen="Календарь и фонд",
        order="sprint_no",
        orderable=("pi_id", "sprint_no", "start_date", "end_date", "length_days"),
        note="Сетка квартала: 7 спринтов, `length_days` — генерируемая колонка (у 7-го 8 дней).",
    ),
    _source(
        "initiatives",
        screen="Справочники",
        order="priority_rung,prodf_id",
        orderable=("prodf_id", "br_id", "title", "priority_rung"),
        note="Инициативы заказчика со скорингом. PRODF ↔ BR строго 1:1 (15 инициатив).",
    ),
    _source(
        "ref_result_options",
        screen="Справочники",
        order="ord",
        orderable=("code", "ord", "label"),
        note="«Варианты выбора цели» — выпадашки в UI планирования (8 строк).",
    ),
    _source(
        "v_dq_summary",
        screen="Диагностика",
        order="-n,rule_code",
        orderable=("rule_code", "severity", "n", "example"),
        note="Сводка по качеству исходных данных: 6 правил, материал для слайда "
        "«что не так с исходными данными».",
    ),
    # ------------------------------------------- контракт прогона (SCHEMA.md §2)
    _source(
        "plan_runs",
        screen="KPI",
        order="-run_id",
        orderable=("run_id", "pi_id", "as_of_sprint", "algorithm", "status", "created_at"),
        note="Список прогонов для выбора `run_id`; `params` несёт границы календаря прогона. "
        "Витрина не фильтруется по `run_id` — она для того и нужна, чтобы выбрать прогон.",
    ),
    _source(
        "plan_task_schedule",
        screen="План квартала",
        order="task_id,start_sprint",
        orderable=(
            "task_id", "start_sprint", "end_sprint", "forecast_end_date", "decision",
            "decision_reason",
        ),
        note="Основа ганта. `start_sprint`/`end_sprint` пусты у переносов — это не ошибка: "
        "смотреть `decision` и `decision_reason`.",
        run_column="run_id",
    ),
    _source(
        "v_plan_assignment_detail",
        screen="План квартала",
        order="task_id,sprint_no,engineer_id",
        orderable=(
            "task_id", "sprint_no", "engineer_id", "hours", "home_team_id", "serving_team_id",
            "is_loan", "served_role", "native_role", "is_substitution", "grade",
        ),
        note="Назначения с флагами займа и замещения. «Всё спланировалось» без ответа «кем» "
        "на защите не проходит.",
        run_column="run_id",
    ),
    _source(
        "plan_baseline",
        screen="План квартала",
        order="task_id",
        orderable=("task_id", "planned_sp", "committed"),
        note="Базовая линия Недели 0 — знаменатель KPI. Строится нами (ADR-004), "
        "в исходных данных её нет.",
        run_column="run_id",
    ),
    _source(
        "alerts",
        screen="Алерты",
        order="sprint_no,level,alert_id",
        orderable=("alert_id", "sprint_no", "level", "alert_type", "entity_type", "entity_id"),
        note="Лента рисков: `level` = red (срыв дедлайна) | yellow (каскадный сдвиг) | "
        "orange (дефицит по роли), детали — в `payload`.",
        run_column="run_id",
    ),
    _source(
        "kpi_snapshots",
        screen="KPI",
        order="sprint_no,kpi_code",
        orderable=("sprint_no", "kpi_code", "value", "target_min", "target_max"),
        note="Плашку красить по `target_min` / `target_max` из строки, пороги во фронте "
        "не зашивать. Значение вне нормы — не ошибка данных.",
        run_column="run_id",
    ),
    _source(
        "v_plan_violations",
        screen="Алерты",
        order="severity,check_code",
        orderable=("check_code", "severity", "entity", "detail"),
        note="29 проверок контракта: `error` обязан быть 0, `warning` показывать словами. "
        "Сейчас 5–7 warning `PLANNED_END_OVERSAIL` — прогноз выходит за даты исходного плана "
        "(ADR-016: это история, а не обязательство).",
        run_column="run_id",
    ),
)

BY_NAME: dict[str, Source] = {source.name: source for source in SOURCES}
if len(BY_NAME) != len(SOURCES):  # имя витрины — ключ URL: дубликат означал бы двусмысленность
    raise ValueError("имена витрин в SOURCES повторяются")


class UnknownView(ValueError):
    """Витрины нет в белом списке: 404 и полный список разрешённых имён."""

    def __init__(self, name: str) -> None:
        super().__init__(f"нет такой витрины: {name}")
        self.name = name

    def payload(self) -> dict[str, Any]:
        return {
            "error": "not_found",
            "message": f"нет такой витрины: {self.name}",
            "known": [source.name for source in SOURCES],
            "hint": "справочник витрин: GET /api/views",
        }


class BadRequest(ValueError):
    """Параметр запроса не прошёл проверку: это 400, а не 500 и не пустой ответ."""

    def __init__(self, param: str, message: str, known: Sequence[Any] | None = None) -> None:
        super().__init__(message)
        self.param = param
        self.message = message
        self.known = list(known) if known is not None else None

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": "bad_request",
            "param": self.param,
            "message": self.message,
        }
        if self.known is not None:
            body["known"] = self.known
        return body


def _as_int(raw: str | None, param: str, default: int | None, minimum: int, maximum: int) -> int | None:
    """Разбор целочисленного параметра. Не-число — 400, а не молчаливый дефолт."""
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise BadRequest(param, f"{param} должен быть целым числом, получено «{raw}»") from None
    if not minimum <= value <= maximum:
        raise BadRequest(param, f"{param} должен быть в диапазоне {minimum}..{maximum}, получено {value}")
    return value


def parse_run_id(raw: str | None) -> int | None:
    """`?run_id=2`; пусто — прогон по умолчанию (последний удачный)."""
    return _as_int(raw, "run_id", None, 1, 2_147_483_647)


def parse_limit(raw: str | None) -> int:
    return int(_as_int(raw, "limit", LIMIT_DEFAULT, 1, LIMIT_MAX))


def parse_offset(raw: str | None) -> int:
    return int(_as_int(raw, "offset", 0, 0, 2_147_483_647))


def parse_order(raw: str | None, source: Source) -> list[tuple[str, bool]]:
    """`?order=team_id,-gap_hh` → [(колонка, DESC?)]. Только колонки витрины.

    `order` — единственное место, где имя колонки попадает в текст SQL: параметром
    его не подставить. Поэтому здесь белый список, а не экранирование.
    """
    spec = raw if raw not in (None, "") else source.order
    pairs: list[tuple[str, bool]] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        desc = token.startswith("-")
        column = token.lstrip("-+")
        if column not in source.orderable:
            raise BadRequest(
                "order",
                f"колонка «{column}» не сортируется в витрине {source.name}",
                known=list(source.orderable),
            )
        pairs.append((column, desc))
    if not pairs:
        raise BadRequest("order", "order пуст: укажите хотя бы одну колонку", known=list(source.orderable))
    return pairs


def catalog() -> dict[str, Any]:
    """`GET /api/views`: справочник витрин, чтобы фронт не угадывал контракт."""
    return {
        "count": len(SOURCES),
        "limit_default": LIMIT_DEFAULT,
        "limit_max": LIMIT_MAX,
        "items": [source.as_dict() for source in SOURCES],
    }


def last_ok_run_id() -> int | None:
    """Прогон по умолчанию: последний удачный (`MAX(run_id) WHERE status = 'ok'`).

    Тот же предикат, что у KPI и проверки `BASELINE_MUTATED` (SCHEMA.md §2): фронт,
    бэкенд и приёмка не могут разойтись в том, какой прогон «текущий».

    Отдельный запрос на каждый вызов — осознанно: `MAX` по двум строкам дешевле
    кэша, а устаревший «текущий прогон» в UI дороже лишнего запроса.
    """
    row = db.query_one(LAST_OK_RUN_SQL) or {}
    value = row.get("run_id")
    return None if value is None else int(value)


def _relation_columns(relation: str) -> list[str]:
    """Колонки витрины для пустого результата: у пустого `items` ключей нет."""
    rows = db.query_dicts(COLUMNS_SQL, [relation])
    return [str(row["column_name"]) for row in rows]


def fetch(
    name: str,
    *,
    run_id: int | None = None,
    limit: int | None = None,
    offset: int | None = None,
    order: str | None = None,
) -> dict[str, Any]:
    """Строки витрины как есть плюс конверт (ADR-019).

    Конверт отвечает на вопросы, которые иначе решает каждый экран по-своему:
    ЧТО показано (`view`), на каком прогоне (`run_id`), на какой момент
    (`as_of`), сколько всего строк (`count`), какой формы строки (`columns`).
    Без `as_of` в шапке цифры «плывут» между экранами, а без `count` фронт не
    отличит «витрина пуста» от «страница кончилась».

    Пустая витрина — это 200 и `count: 0`, а не 404: «в этом прогоне переносов
    нет» и «нет такой витрины» — разные вещи, и UI должен их различать.
    """
    source = BY_NAME.get(name)
    if source is None:
        raise UnknownView(name)

    limit = LIMIT_DEFAULT if limit is None else int(limit)
    offset = 0 if offset is None else int(offset)
    if not 1 <= limit <= LIMIT_MAX:
        raise BadRequest("limit", f"limit должен быть в диапазоне 1..{LIMIT_MAX}, получено {limit}")
    if offset < 0:
        raise BadRequest("offset", f"offset не может быть отрицательным, получено {offset}")

    if run_id is not None and source.run_column is None:
        raise BadRequest(
            "run_id",
            f"витрина {source.name} не привязана к прогону",
            known=[s.name for s in SOURCES if s.run_column],
        )

    run_default = False
    if source.run_column is not None and run_id is None:
        run_id = last_ok_run_id()  # None — удачных прогонов ещё нет, фильтра не будет
        run_default = True

    pairs = parse_order(order, source)
    where = ""
    where_params: list[Any] = []
    if source.run_column is not None and run_id is not None:
        where = f" WHERE {source.run_column} = %s::int"
        where_params.append(int(run_id))

    # NULLS LAST в обе стороны: у переносов `start_sprint` пуст, и без этого они
    # всплывали бы наверх при DESC — первое, что видит заказчик, было бы «не
    # запланировано» вместо плана.
    order_sql = ", ".join(f"{col} {'DESC' if desc else 'ASC'} NULLS LAST" for col, desc in pairs)
    sql = f"SELECT * FROM {source.name}{where} ORDER BY {order_sql} LIMIT %s::int OFFSET %s::int"
    items = db.query_dicts(sql, [*where_params, limit, offset])

    truncated = len(items) == limit
    if offset or truncated:
        total = int(db.scalar(f"SELECT COUNT(*) FROM {source.name}{where}", where_params) or 0)
    else:
        total = len(items)  # страница не полная и это её начало: больше строк нет

    return {
        "view": source.name,
        "kind": source.kind,
        "screen": source.screen,
        "note": source.note,
        "run_id": run_id,
        "run_column": source.run_column,
        "run_default": run_default,
        "as_of": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "order": [f"-{col}" if desc else col for col, desc in pairs],
        "limit": limit,
        "offset": offset,
        "count": total,
        "returned": len(items),
        "truncated": truncated,
        "has_more": offset + len(items) < total,
        "columns": list(items[0]) if items else _relation_columns(source.name),
        "items": items,
    }

