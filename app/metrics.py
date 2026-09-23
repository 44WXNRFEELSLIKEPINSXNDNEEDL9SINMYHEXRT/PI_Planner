"""Метрики процесса в текстовом формате Prometheus 0.0.4.

Имена метрик и лейблов — **контракт с devops** (`docs/OBSERVABILITY.md`):
переименование метрики или лейбла ломает дашборд, поэтому оно
считается breaking change, а не рефакторингом.

Своя реализация вместо `prometheus_client`: процесс один, эндпоинт один, и
лишняя зависимость в демо-стенде дороже десятка строк форматирования. Формат
проверяемый: `promtool check metrics` на вывод `/metrics` проходит.

Пять групп метрик:

* **HTTP** — считает сам сервер (`Metrics.observe`), базы не касается. Метка
  `route` берётся из фиксированного набора, а не из URL: иначе произвольный
  путь вроде `/assets/index-a1b2c3.js` раздул бы кардинальность до числа файлов;
* **процесс** — uptime, CPU, RSS и число потоков;
* **DB-клиент** — подключения и операции из `app.db`, без SQL в labels;
* **витрины** — имя только из whitelist `app.views.SOURCES`;
* **бизнес** — снимок из базы (`collect_business_metrics`) с кэшем на
  `ttl` секунд. Без кэша каждый scrape дёргал бы `v_plan_violations` — самую
  тяжёлую вьюху контракта. Снимок кэшируется и при ошибке: упавшая база не
  должна получать запросы чаще, чем здоровый scrape.

Ошибка базы не делает `/metrics` недоступным: вместо бизнес-серий отдаются
`pi_planner_db_up 0` и `pi_planner_db_metrics_error{error_class}`. Иначе
мониторинг терял бы вместе с метриками и причину их отсутствия.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

from app import db

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
HTTP_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0)
VIEW_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0)

# Метки маршрута — фиксированный набор (см. модуль).
STATIC_ROUTE = "/static"
UNKNOWN_ROUTE = "/api/*"
VIEWS_PREFIX = "/api/views"  # справочник витрин: GET /api/views
VIEWS_ROUTE = "/api/views/{view}"  # одна метка на все витрины: их число растёт вместе с UI
NO_RESPONSE_STATUS = "0"  # ответ не отправлен: клиент оборвал соединение

# Бизнес-метрики читаются одним repeatable-read bundle, чтобы снимок был согласованным:
# «последний прогон» из `plan_runs` и его нарушения из `v_plan_violations`
# не должны разъезжаться на середине scrape.
BUSINESS_SQL = """
WITH last AS (
    SELECT run_id, as_of_sprint, status, created_at, params
    FROM plan_runs
    ORDER BY run_id DESC
    LIMIT 1
)
SELECT (SELECT COUNT(*) FROM plan_runs)                            AS runs_total,
       l.run_id,
       l.as_of_sprint,
       l.status,
       EXTRACT(EPOCH FROM l.created_at)                            AS created_epoch,
       (SELECT COUNT(*) FROM v_plan_violations v
         WHERE v.run_id = l.run_id AND v.severity = 'error')       AS errors,
       (SELECT COUNT(*) FROM v_plan_violations v
         WHERE v.run_id = l.run_id AND v.severity = 'warning')     AS warnings,
       (SELECT COUNT(*) FROM plan_task_schedule s
         WHERE s.run_id = l.run_id AND s.decision = 'in_quarter')  AS in_quarter,
       (SELECT COALESCE(SUM(a.hours), 0) FROM plan_assignments a
         WHERE a.run_id = l.run_id)                                AS assigned_hh,
       (SELECT COALESCE(SUM(a.hours), 0) FROM plan_assignments a
         WHERE a.run_id = l.run_id AND a.is_loan)                  AS loan_hh,
       NULLIF(l.params #>> '{observability,load_inputs_seconds}', '')::numeric
                                                                    AS load_seconds,
       NULLIF(l.params #>> '{observability,load_baseline_seconds}', '')::numeric
                                                                    AS baseline_seconds,
       NULLIF(l.params #>> '{observability,build_plan_seconds}', '')::numeric
                                                                    AS build_seconds,
       NULLIF(l.params #>> '{observability,write_plan_seconds}', '')::numeric
                                                                    AS write_seconds
FROM last l
"""

# Календарь отдаём метрикой, а не только в логах: если фонд квартала поедет,
# это должно быть видно на дашборде (fund_factor против sprint_count).
CALENDAR_SQL = """
SELECT p.pi_id, p.start_date, p.end_date, p.sprint_count,
       f.factor                                    AS fund_factor,
       ROUND(p.fte_hours_per_sprint * f.factor, 2) AS fund_hh_per_fte
FROM pi_periods p
JOIN v_pi_fund_factor f USING (pi_id)
ORDER BY p.start_date DESC
LIMIT 1
"""

DECISIONS_SQL = """
WITH last AS (SELECT run_id FROM plan_runs ORDER BY run_id DESC LIMIT 1)
SELECT s.decision, COALESCE(s.decision_reason, 'none') AS reason,
       COUNT(*) AS tasks,
       COALESCE(SUM(t.estimated_hh_effective), 0) AS hours
FROM last l
JOIN plan_task_schedule s USING (run_id)
JOIN tasks t USING (task_id)
GROUP BY s.decision, COALESCE(s.decision_reason, 'none')
ORDER BY s.decision, reason
"""

ALERTS_SQL = """
WITH last AS (SELECT run_id FROM plan_runs ORDER BY run_id DESC LIMIT 1)
SELECT a.level, a.alert_type, COUNT(*) AS count
FROM last l
JOIN alerts a USING (run_id)
GROUP BY a.level, a.alert_type
ORDER BY a.level, a.alert_type
"""

KPI_SQL = """
WITH last AS (SELECT run_id FROM plan_runs ORDER BY run_id DESC LIMIT 1)
SELECT k.sprint_no, k.kpi_code, k.value, k.target_min, k.target_max
FROM last l
JOIN kpi_snapshots k USING (run_id)
ORDER BY k.kpi_code, k.sprint_no
"""

DQ_SQL = """
SELECT severity, COUNT(*) AS count
FROM dq_issues
GROUP BY severity
ORDER BY severity
"""

ETL_SQL = """
SELECT EXTRACT(EPOCH FROM loaded_at) AS loaded_epoch, etl_version, pi_start
FROM load_batches
ORDER BY batch_id DESC
LIMIT 1
"""

MIGRATIONS_PRESENT_SQL = """
SELECT to_regclass('public.schema_migrations') IS NOT NULL AS present
"""

MIGRATIONS_SQL = """
SELECT COUNT(*) AS applied,
       EXTRACT(EPOCH FROM MAX(applied_at)) AS last_applied_epoch
FROM schema_migrations
"""


def collect_business_metrics() -> dict[str, Any]:
    """Снимок бизнес-метрик: последний прогон планировщика и календарь PI."""
    started = time.perf_counter()
    bundle = db.query_bundle(
        (
            ("metrics_plan", BUSINESS_SQL, True),
            ("metrics_calendar", CALENDAR_SQL, True),
            ("metrics_decisions", DECISIONS_SQL, False),
            ("metrics_alerts", ALERTS_SQL, False),
            ("metrics_kpis", KPI_SQL, False),
            ("metrics_dq", DQ_SQL, False),
            ("metrics_etl", ETL_SQL, True),
        )
    )
    run = bundle["metrics_plan"]
    calendar = bundle["metrics_calendar"]
    decisions = bundle["metrics_decisions"]
    alerts = bundle["metrics_alerts"]
    kpis = bundle["metrics_kpis"]
    dq = bundle["metrics_dq"]
    etl = bundle["metrics_etl"]
    migration_table = db.query_one(
        MIGRATIONS_PRESENT_SQL, operation="metrics_migrations_present"
    ) or {}
    migrations = (
        db.query_one(MIGRATIONS_SQL, operation="metrics_migrations") or {}
        if migration_table.get("present")
        else {"applied": 0, "last_applied_epoch": 0}
    )
    discovered_migrations = len(list((Path(__file__).resolve().parent.parent / "db/migrations").glob("*.sql")))
    migrations["pending"] = max(0, discovered_migrations - int(migrations.get("applied") or 0))
    return {
        "db_up": 1,
        "scrape_seconds": time.perf_counter() - started,
        "taken_at": time.time(),
        "runs_total": int(run.get("runs_total") or 0),
        "run": dict(run) if run else None,
        "calendar": dict(calendar) if calendar else None,
        "decisions": decisions,
        "alerts": alerts,
        "kpis": kpis,
        "dq": dq,
        "etl": etl or None,
        "migrations": migrations,
    }


def _label(value: Any) -> str:
    """Значение лейбла по правилам формата: экранируем слэш, кавычку, перевод строки."""
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(name: str, labels: dict[str, Any] | None, value: Any) -> str:
    """Одна строка-образец: `name{label="value"} value`."""
    if not labels:
        return f"{name} {value}"
    inner = ",".join(f'{key}="{_label(val)}"' for key, val in labels.items())
    return f"{name}{{{inner}}} {value}"


def _bound(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _resident_memory_bytes() -> int:
    """RSS процесса на Linux; 0 на платформах без `/proc`."""
    try:
        pages = int((Path("/proc/self/statm").read_text().split())[1])
        return pages * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError):
        return 0


class Metrics:
    """Счётчики процесса: HTTP + кэшированный снимок из базы.

    Потокобезопасен: `ThreadingHTTPServer` создаёт `Handler` на каждый запрос,
    поэтому реестр живёт один на процесс, а не в хендлере.
    """

    def __init__(
        self,
        *,
        app_version: str,
        etl_version: str,
        pi_id: str | None,
        known_api: Sequence[str],
        ttl: float,
        started_at: float | None = None,
        collector: Any = None,
    ) -> None:
        self.app_version = app_version
        self.etl_version = etl_version
        self.pi_id = pi_id or "unknown"
        self.known_api = tuple(known_api)
        self.ttl = ttl
        self.started_at = time.time() if started_at is None else started_at
        # `collector` подменяется в тестах: живая база тестам не нужна.
        self.collector = collector or collect_business_metrics
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._duration_count: dict[tuple[str, str], int] = defaultdict(int)
        self._duration_buckets: dict[tuple[str, str, float], int] = defaultdict(int)
        self._response_bytes: dict[tuple[str, str], int] = defaultdict(int)
        self._exceptions: dict[tuple[str, str], int] = defaultdict(int)
        self._in_flight = 0
        self._view_total: dict[tuple[str, str], int] = defaultdict(int)
        self._view_duration_sum: dict[str, float] = defaultdict(float)
        self._view_duration_count: dict[str, int] = defaultdict(int)
        self._view_duration_buckets: dict[tuple[str, float], int] = defaultdict(int)
        self._view_rows: dict[str, int] = defaultdict(int)
        self._view_truncated: dict[str, int] = defaultdict(int)
        self._business: dict[str, Any] | None = None
        self._business_at = 0.0
        self._business_error: str | None = None

    # ------------------------------------------------------------------ HTTP
    def route_label(self, path: str) -> str:
        """Метка маршрута — только из фиксированного набора (кардинальность!)."""
        if path in self.known_api:
            return path
        if path.startswith(f"{VIEWS_PREFIX}/"):
            # Имя витрины — часть пути, но метка у всех витрин одна: иначе каждый
            # новый экран добавлял бы серию на дашборд devops.
            return VIEWS_ROUTE
        if path.startswith("/api/"):
            return UNKNOWN_ROUTE
        return STATIC_ROUTE

    def enter(self) -> None:
        with self._lock:
            self._in_flight += 1

    def leave(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def observe(
        self,
        method: str,
        route: str,
        status: Any,
        seconds: float,
        response_bytes: int = 0,
        error_class: str | None = None,
    ) -> None:
        """Записать факт запроса: код ответа и длительность."""
        key = (method, route, str(status))
        with self._lock:
            self._requests[key] += 1
            self._duration_sum[(method, route)] += seconds
            self._duration_count[(method, route)] += 1
            self._response_bytes[(method, route)] += max(0, response_bytes)
            for bound in HTTP_DURATION_BUCKETS:
                if seconds <= bound:
                    self._duration_buckets[(method, route, bound)] += 1
            if error_class:
                self._exceptions[(route, error_class)] += 1

    def observe_view(
        self,
        view: str,
        outcome: str,
        seconds: float,
        *,
        rows: int = 0,
        truncated: bool = False,
    ) -> None:
        """Записать обращение к витрине; `view` приходит только из whitelist."""
        with self._lock:
            self._view_total[(view, outcome)] += 1
            self._view_duration_sum[view] += seconds
            self._view_duration_count[view] += 1
            self._view_rows[view] += max(0, rows)
            if truncated:
                self._view_truncated[view] += 1
            for bound in VIEW_DURATION_BUCKETS:
                if seconds <= bound:
                    self._view_duration_buckets[(view, bound)] += 1

    # ------------------------------------------------------------------- БД
    def business(self) -> dict[str, Any]:
        """Снимок из базы с кэшем: scrape раз в 15 секунд не должен бить по вьюхам."""
        now = time.monotonic()
        with self._lock:
            cached = self._business
            if cached is not None and now - self._business_at < self.ttl:
                return cached
        try:
            snapshot = self.collector()
            error = None
        except Exception as exc:  # noqa: BLE001 — метрики обязаны отдаваться и без базы
            snapshot = {"db_up": 0, "taken_at": time.time(), "scrape_seconds": 0.0}
            error = type(exc).__name__
        with self._lock:
            self._business = snapshot
            self._business_at = now
            self._business_error = error
        return snapshot

    def reset(self) -> None:
        """Сбросить счётчики — нужен тестам, чтобы серии не текли между прогонами."""
        with self._lock:
            self._requests.clear()
            self._duration_sum.clear()
            self._duration_count.clear()
            self._duration_buckets.clear()
            self._response_bytes.clear()
            self._exceptions.clear()
            self._in_flight = 0
            self._view_total.clear()
            self._view_duration_sum.clear()
            self._view_duration_count.clear()
            self._view_duration_buckets.clear()
            self._view_rows.clear()
            self._view_truncated.clear()
            self._business = None
            self._business_at = 0.0
            self._business_error = None

    # -------------------------------------------------------------- отрисовка
    def render(self) -> str:
        """Вывод `/metrics` целиком: формат Prometheus 0.0.4, один текст на ответ."""
        snapshot = self.business()
        with self._lock:
            requests = dict(self._requests)
            sums = dict(self._duration_sum)
            counts = dict(self._duration_count)
            duration_buckets = dict(self._duration_buckets)
            response_bytes = dict(self._response_bytes)
            exceptions = dict(self._exceptions)
            in_flight = self._in_flight
            view_total = dict(self._view_total)
            view_sums = dict(self._view_duration_sum)
            view_counts = dict(self._view_duration_count)
            view_buckets = dict(self._view_duration_buckets)
            view_rows = dict(self._view_rows)
            view_truncated = dict(self._view_truncated)
            business_error = self._business_error

        out: list[str] = []

        def block(name: str, help_: str, mtype: str, samples: list[str]) -> None:
            """# HELP + # TYPE + образцы. Порядок образцов задают вызывающие."""
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} {mtype}")
            out.extend(samples)

        block(
            "pi_planner_up",
            "1 пока процесс отвечает на запросы",
            "gauge",
            ["pi_planner_up 1"],
        )
        block(
            "pi_planner_build_info",
            "Версии запущенного кода: приложение, ETL, контракт PI",
            "gauge",
            [
                _sample(
                    "pi_planner_build_info",
                    {
                        "version": self.app_version,
                        "etl_version": self.etl_version,
                        "pi_id": self.pi_id,
                        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                    },
                    1,
                )
            ],
        )
        block(
            "pi_planner_uptime_seconds",
            "Секунды с момента старта процесса",
            "gauge",
            [f"pi_planner_uptime_seconds {round(time.time() - self.started_at, 3)}"],
        )
        block(
            "pi_planner_process_cpu_seconds_total",
            "CPU-секунды процесса user+system",
            "counter",
            [f"pi_planner_process_cpu_seconds_total {round(time.process_time(), 6)}"],
        )
        block(
            "pi_planner_process_resident_memory_bytes",
            "Resident set size процесса; 0 если платформа не предоставляет /proc",
            "gauge",
            [f"pi_planner_process_resident_memory_bytes {_resident_memory_bytes()}"],
        )
        block(
            "pi_planner_process_threads",
            "Число активных Python-потоков",
            "gauge",
            [f"pi_planner_process_threads {threading.active_count()}"],
        )
        block(
            "pi_planner_http_requests_in_flight",
            "Запросы, которые обрабатываются прямо сейчас",
            "gauge",
            [f"pi_planner_http_requests_in_flight {in_flight}"],
        )
        block(
            "pi_planner_http_requests_total",
            "Запросы по маршруту и коду ответа. route — фиксированный набор, не URL",
            "counter",
            [
                _sample(
                    "pi_planner_http_requests_total",
                    {"method": method, "route": route, "status": status},
                    value,
                )
                for (method, route, status), value in sorted(requests.items())
            ],
        )
        duration_samples: list[str] = []
        for (method, route), total in sorted(sums.items()):
            labels = {"method": method, "route": route}
            for bound in HTTP_DURATION_BUCKETS:
                duration_samples.append(
                    _sample(
                        "pi_planner_http_request_duration_seconds_bucket",
                        {**labels, "le": _bound(bound)},
                        duration_buckets.get((method, route, bound), 0),
                    )
                )
            duration_samples.append(
                _sample(
                    "pi_planner_http_request_duration_seconds_bucket",
                    {**labels, "le": "+Inf"},
                    counts[(method, route)],
                )
            )
            duration_samples.append(
                _sample("pi_planner_http_request_duration_seconds_sum", labels, round(total, 6))
            )
            duration_samples.append(
                _sample(
                    "pi_planner_http_request_duration_seconds_count",
                    labels,
                    counts[(method, route)],
                )
            )
        block(
            "pi_planner_http_request_duration_seconds",
            "Histogram длительности запроса; p95/p99 считаются histogram_quantile",
            "histogram",
            duration_samples,
        )
        block(
            "pi_planner_http_response_bytes_total",
            "Суммарный размер тел HTTP-ответов",
            "counter",
            [
                _sample(
                    "pi_planner_http_response_bytes_total",
                    {"method": method, "route": route},
                    value,
                )
                for (method, route), value in sorted(response_bytes.items())
            ],
        )
        block(
            "pi_planner_http_exceptions_total",
            "Необработанные исключения до отправки HTTP-ответа",
            "counter",
            [
                _sample(
                    "pi_planner_http_exceptions_total",
                    {"route": route, "error_class": error_class},
                    value,
                )
                for (route, error_class), value in sorted(exceptions.items())
            ],
        )
        block(
            "pi_planner_view_requests_total",
            "Обращения к whitelist-витринам по исходу",
            "counter",
            [
                _sample(
                    "pi_planner_view_requests_total",
                    {"view": view, "outcome": outcome},
                    value,
                )
                for (view, outcome), value in sorted(view_total.items())
            ],
        )
        view_duration_samples: list[str] = []
        for view, total in sorted(view_sums.items()):
            for bound in VIEW_DURATION_BUCKETS:
                view_duration_samples.append(
                    _sample(
                        "pi_planner_view_query_duration_seconds_bucket",
                        {"view": view, "le": _bound(bound)},
                        view_buckets.get((view, bound), 0),
                    )
                )
            view_duration_samples += [
                _sample(
                    "pi_planner_view_query_duration_seconds_bucket",
                    {"view": view, "le": "+Inf"},
                    view_counts[view],
                ),
                _sample("pi_planner_view_query_duration_seconds_sum", {"view": view}, round(total, 6)),
                _sample("pi_planner_view_query_duration_seconds_count", {"view": view}, view_counts[view]),
            ]
        block(
            "pi_planner_view_query_duration_seconds",
            "Histogram полной выборки whitelist-витрины",
            "histogram",
            view_duration_samples,
        )
        block(
            "pi_planner_view_rows_returned_total",
            "Строки, возвращённые whitelist-витринами",
            "counter",
            [_sample("pi_planner_view_rows_returned_total", {"view": view}, value) for view, value in sorted(view_rows.items())],
        )
        block(
            "pi_planner_view_truncated_total",
            "Ответы витрин, ограниченные limit",
            "counter",
            [_sample("pi_planner_view_truncated_total", {"view": view}, value) for view, value in sorted(view_truncated.items())],
        )

        out.extend(self._db_lines(db.metrics_snapshot()))
        block(
            "pi_planner_db_up",
            "1 если последний сбор метрик из базы прошёл, 0 если база недоступна",
            "gauge",
            [f"pi_planner_db_up {snapshot.get('db_up', 0)}"],
        )
        block(
            "pi_planner_db_metrics_timestamp_seconds",
            "Unix-время снимка бизнес-метрик: по нему видно, что снимок устарел",
            "gauge",
            [f"pi_planner_db_metrics_timestamp_seconds {round(snapshot.get('taken_at', 0.0), 3)}"],
        )
        block(
            "pi_planner_db_metrics_scrape_seconds",
            "Сколько занял последний сбор бизнес-метрик",
            "gauge",
            [
                "pi_planner_db_metrics_scrape_seconds "
                f"{round(snapshot.get('scrape_seconds', 0.0), 6)}"
            ],
        )
        if business_error:
            block(
                "pi_planner_db_metrics_error",
                "1 при ошибке последнего сбора: error_class — имя класса исключения",
                "gauge",
                [_sample("pi_planner_db_metrics_error", {"error_class": business_error}, 1)],
            )

        out.extend(self._plan_lines(snapshot))
        return "\n".join(out) + "\n"

    def _db_lines(self, snapshot: dict[str, Any]) -> list[str]:
        """Метрики DB-клиента процесса; PostgreSQL-сервер покроет exporter."""
        out: list[str] = []
        out += [
            "# HELP pi_planner_db_connections_open Открытые соединения этого процесса",
            "# TYPE pi_planner_db_connections_open gauge",
            f"pi_planner_db_connections_open {snapshot['connections']}",
            "# HELP pi_planner_db_operations_in_flight DB-операции этого процесса в работе",
            "# TYPE pi_planner_db_operations_in_flight gauge",
            f"pi_planner_db_operations_in_flight {snapshot['operations']}",
            "# HELP pi_planner_db_connections_total Попытки подключения к PostgreSQL",
            "# TYPE pi_planner_db_connections_total counter",
        ]
        for outcome, value in sorted(snapshot["connection_total"].items()):
            out.append(_sample("pi_planner_db_connections_total", {"outcome": outcome}, value))

        out += [
            "# HELP pi_planner_db_connection_duration_seconds Histogram установки соединения",
            "# TYPE pi_planner_db_connection_duration_seconds histogram",
        ]
        for bound in db.DB_DURATION_BUCKETS:
            out.append(
                _sample(
                    "pi_planner_db_connection_duration_seconds_bucket",
                    {"le": _bound(bound)},
                    snapshot["connection_buckets"].get(bound, 0),
                )
            )
        out += [
            _sample(
                "pi_planner_db_connection_duration_seconds_bucket",
                {"le": "+Inf"},
                snapshot["connection_count"],
            ),
            f"pi_planner_db_connection_duration_seconds_sum {round(snapshot['connection_sum'], 6)}",
            f"pi_planner_db_connection_duration_seconds_count {snapshot['connection_count']}",
            "# HELP pi_planner_db_operations_total DB-операции по имени и исходу",
            "# TYPE pi_planner_db_operations_total counter",
        ]
        for (operation, outcome), value in sorted(snapshot["operation_total"].items()):
            out.append(
                _sample(
                    "pi_planner_db_operations_total",
                    {"operation": operation, "outcome": outcome},
                    value,
                )
            )

        out += [
            "# HELP pi_planner_db_operation_duration_seconds Histogram DB-операций",
            "# TYPE pi_planner_db_operation_duration_seconds histogram",
        ]
        for operation, total in sorted(snapshot["operation_sum"].items()):
            for bound in db.DB_DURATION_BUCKETS:
                out.append(
                    _sample(
                        "pi_planner_db_operation_duration_seconds_bucket",
                        {"operation": operation, "le": _bound(bound)},
                        snapshot["operation_buckets"].get((operation, bound), 0),
                    )
                )
            count = snapshot["operation_count"][operation]
            out += [
                _sample(
                    "pi_planner_db_operation_duration_seconds_bucket",
                    {"operation": operation, "le": "+Inf"},
                    count,
                ),
                _sample(
                    "pi_planner_db_operation_duration_seconds_sum",
                    {"operation": operation},
                    round(total, 6),
                ),
                _sample(
                    "pi_planner_db_operation_duration_seconds_count",
                    {"operation": operation},
                    count,
                ),
            ]
        out += [
            "# HELP pi_planner_db_rows_total Строки, прочитанные или изменённые DB-операцией",
            "# TYPE pi_planner_db_rows_total counter",
        ]
        for operation, value in sorted(snapshot["rows_total"].items()):
            out.append(_sample("pi_planner_db_rows_total", {"operation": operation}, value))
        return out

    # ------------------------------------------------- бизнес-серии из базы
    def _plan_lines(self, snapshot: dict[str, Any]) -> list[str]:
        """Серии последнего прогона и календаря. Пусто, если базы нет."""
        out: list[str] = []
        run = snapshot.get("run")
        if run:
            run_id = str(run.get("run_id"))
            out += [
                "# HELP pi_planner_plan_runs_total Прогонов планировщика в plan_runs",
                "# TYPE pi_planner_plan_runs_total gauge",
                f"pi_planner_plan_runs_total {snapshot.get('runs_total', 0)}",
                "# HELP pi_planner_plan_last_run_info Последний прогон: run_id, as_of_sprint, status",
                "# TYPE pi_planner_plan_last_run_info gauge",
                _sample(
                    "pi_planner_plan_last_run_info",
                    {
                        "run_id": run_id,
                        "as_of_sprint": run.get("as_of_sprint"),
                        "status": run.get("status"),
                    },
                    1,
                ),
                "# HELP pi_planner_plan_last_run_timestamp_seconds Unix-время создания прогона",
                "# TYPE pi_planner_plan_last_run_timestamp_seconds gauge",
                _sample(
                    "pi_planner_plan_last_run_timestamp_seconds",
                    {"run_id": run_id},
                    round(float(run.get("created_epoch") or 0.0), 3),
                ),
                "# HELP pi_planner_plan_violations Нарушения контракта по severity",
                "# TYPE pi_planner_plan_violations gauge",
                _sample(
                    "pi_planner_plan_violations",
                    {"run_id": run_id, "severity": "error"},
                    int(run.get("errors") or 0),
                ),
                _sample(
                    "pi_planner_plan_violations",
                    {"run_id": run_id, "severity": "warning"},
                    int(run.get("warnings") or 0),
                ),
                "# HELP pi_planner_plan_tasks_in_quarter Задачи с решением in_quarter",
                "# TYPE pi_planner_plan_tasks_in_quarter gauge",
                _sample(
                    "pi_planner_plan_tasks_in_quarter",
                    {"run_id": run_id},
                    int(run.get("in_quarter") or 0),
                ),
                "# HELP pi_planner_plan_assigned_hours Часы исполнителей в прогоне",
                "# TYPE pi_planner_plan_assigned_hours gauge",
                _sample(
                    "pi_planner_plan_assigned_hours",
                    {"run_id": run_id},
                    round(float(run.get("assigned_hh") or 0.0), 2),
                ),
                "# HELP pi_planner_plan_last_run_id Числовой ID последнего прогона без churn лейбла",
                "# TYPE pi_planner_plan_last_run_id gauge",
                f"pi_planner_plan_last_run_id {int(run.get('run_id') or 0)}",
                "# HELP pi_planner_plan_loan_hours Часы межкомандных займов последнего прогона",
                "# TYPE pi_planner_plan_loan_hours gauge",
                f"pi_planner_plan_loan_hours {round(float(run.get('loan_hh') or 0.0), 2)}",
            ]

            phase_samples = []
            for phase, field in (
                ("load_inputs", "load_seconds"),
                ("load_baseline", "baseline_seconds"),
                ("build_plan", "build_seconds"),
                ("write_plan", "write_seconds"),
            ):
                if run.get(field) is not None:
                    phase_samples.append(
                        _sample(
                            "pi_planner_job_last_duration_seconds",
                            {"phase": phase},
                            round(float(run[field]), 6),
                        )
                    )
            if phase_samples:
                out += [
                    "# HELP pi_planner_job_last_duration_seconds Длительность фаз последнего сохранённого прогона",
                    "# TYPE pi_planner_job_last_duration_seconds gauge",
                    *phase_samples,
                ]

        decisions = snapshot.get("decisions") or []
        if decisions:
            out += [
                "# HELP pi_planner_plan_tasks Задачи последнего прогона по решению и причине",
                "# TYPE pi_planner_plan_tasks gauge",
            ]
            out.extend(
                _sample(
                    "pi_planner_plan_tasks",
                    {"decision": row["decision"], "reason": row["reason"]},
                    int(row.get("tasks") or 0),
                )
                for row in decisions
            )
            out += [
                "# HELP pi_planner_plan_task_hours Часы задач последнего прогона по решению и причине",
                "# TYPE pi_planner_plan_task_hours gauge",
            ]
            out.extend(
                _sample(
                    "pi_planner_plan_task_hours",
                    {"decision": row["decision"], "reason": row["reason"]},
                    round(float(row.get("hours") or 0.0), 2),
                )
                for row in decisions
            )

        alerts = snapshot.get("alerts") or []
        if alerts:
            out += [
                "# HELP pi_planner_plan_alerts Алерты последнего прогона по уровню и типу",
                "# TYPE pi_planner_plan_alerts gauge",
            ]
            out.extend(
                _sample(
                    "pi_planner_plan_alerts",
                    {"level": row["level"], "type": row["alert_type"]},
                    int(row.get("count") or 0),
                )
                for row in alerts
            )

        kpis = snapshot.get("kpis") or []
        if kpis:
            for metric_name, field, help_text in (
                ("pi_planner_plan_kpi_value", "value", "Значение KPI последнего прогона"),
                ("pi_planner_plan_kpi_target_min", "target_min", "Нижняя граница KPI"),
                ("pi_planner_plan_kpi_target_max", "target_max", "Верхняя граница KPI"),
            ):
                samples = [
                    _sample(
                        metric_name,
                        {"kpi": row["kpi_code"], "sprint": row["sprint_no"]},
                        float(row[field]),
                    )
                    for row in kpis
                    if row.get(field) is not None
                ]
                if samples:
                    out += [
                        f"# HELP {metric_name} {help_text}",
                        f"# TYPE {metric_name} gauge",
                        *samples,
                    ]

        dq = snapshot.get("dq") or []
        if dq:
            out += [
                "# HELP pi_planner_data_quality_issues Находки качества данных по severity",
                "# TYPE pi_planner_data_quality_issues gauge",
            ]
            out.extend(
                _sample(
                    "pi_planner_data_quality_issues",
                    {"severity": row["severity"]},
                    int(row.get("count") or 0),
                )
                for row in dq
            )

        etl = snapshot.get("etl")
        if etl:
            out += [
                "# HELP pi_planner_etl_last_success_timestamp_seconds Время последней успешной загрузки ETL",
                "# TYPE pi_planner_etl_last_success_timestamp_seconds gauge",
                "pi_planner_etl_last_success_timestamp_seconds "
                f"{round(float(etl.get('loaded_epoch') or 0.0), 3)}",
                "# HELP pi_planner_etl_info Версия последней загрузки ETL и начало PI",
                "# TYPE pi_planner_etl_info gauge",
                _sample(
                    "pi_planner_etl_info",
                    {"version": etl.get("etl_version"), "pi_start": etl.get("pi_start")},
                    1,
                ),
            ]

        migrations = snapshot.get("migrations")
        if migrations:
            out += [
                "# HELP pi_planner_migrations_applied Применённые versioned migrations",
                "# TYPE pi_planner_migrations_applied gauge",
                f"pi_planner_migrations_applied {int(migrations.get('applied') or 0)}",
                "# HELP pi_planner_migrations_pending Миграции образа, которых нет в БД",
                "# TYPE pi_planner_migrations_pending gauge",
                f"pi_planner_migrations_pending {int(migrations.get('pending') or 0)}",
                "# HELP pi_planner_migrations_last_applied_timestamp_seconds Время последней миграции",
                "# TYPE pi_planner_migrations_last_applied_timestamp_seconds gauge",
                "pi_planner_migrations_last_applied_timestamp_seconds "
                f"{round(float(migrations.get('last_applied_epoch') or 0.0), 3)}",
            ]

        calendar = snapshot.get("calendar")
        if calendar:
            out += [
                "# HELP pi_planner_calendar_info Границы PI и множитель фонда",
                "# TYPE pi_planner_calendar_info gauge",
                _sample(
                    "pi_planner_calendar_info",
                    {
                        "pi_id": calendar.get("pi_id"),
                        "pi_start": calendar.get("start_date"),
                        "pi_end": calendar.get("end_date"),
                        "sprint_count": calendar.get("sprint_count"),
                        "fund_factor": calendar.get("fund_factor"),
                    },
                    calendar.get("fund_hh_per_fte") or 1,
                ),
            ]
        return out
