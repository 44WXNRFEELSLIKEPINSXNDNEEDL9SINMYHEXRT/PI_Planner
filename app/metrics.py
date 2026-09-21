"""Метрики процесса в текстовом формате Prometheus 0.0.4.

Имена метрик и лейблов — **контракт с devops** (docs/RUNBOOK.md, «Контракт для
мониторинга»): переименование метрики или лейбла ломает дашборд, поэтому оно
считается breaking change, а не рефакторингом.

Своя реализация вместо `prometheus_client`: процесс один, эндпоинт один, и
лишняя зависимость в демо-стенде дороже десятка строк форматирования. Формат
проверяемый: `promtool check metrics` на вывод `/metrics` проходит.

Две группы метрик:

* **HTTP** — считает сам сервер (`Metrics.observe`), базы не касается. Метка
  `route` берётся из фиксированного набора, а не из URL: иначе произвольный
  путь вроде `/assets/index-a1b2c3.js` раздул бы кардинальность до числа файлов;
* **бизнес** — снимок из базы (`collect_business_metrics`) с кэшем на
  `ttl` секунд. Без кэша каждый scrape дёргал бы `v_plan_violations` — самую
  тяжёлую вьюху контракта. Снимок кэшируется и при ошибке: упавшая база не
  должна получать запросы чаще, чем здоровый scrape.

Ошибка базы не делает `/metrics` недоступным: вместо бизнес-серий отдаются
`pi_planner_db_up 0` и `pi_planner_db_metrics_error{error_class}`. Иначе
мониторинг терял бы вместе с метриками и причину их отсутствия.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import defaultdict
from typing import Any, Sequence

from app import db

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Метки маршрута — фиксированный набор (см. модуль).
STATIC_ROUTE = "/static"
UNKNOWN_ROUTE = "/api/*"
VIEWS_PREFIX = "/api/views"  # справочник витрин: GET /api/views
VIEWS_ROUTE = "/api/views/{view}"  # одна метка на все витрины: их число растёт вместе с UI
NO_RESPONSE_STATUS = "0"  # ответ не отправлен: клиент оборвал соединение

# Бизнес-метрики читаются одним запросом, чтобы снимок был согласованным:
# «последний прогон» из `plan_runs` и его нарушения из `v_plan_violations`
# не должны разъезжаться на середине scrape.
BUSINESS_SQL = """
WITH last AS (
    SELECT run_id, as_of_sprint, status, created_at
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
         WHERE a.run_id = l.run_id)                                AS assigned_hh
FROM last l
"""

# Календарь отдаём метрикой, а не только в логах: если фонд квартала поедет,
# это должно быть видно на дашборде (fund_factor 6.5714 против 7.0000).
CALENDAR_SQL = """
SELECT p.pi_id, p.start_date, p.end_date, p.sprint_count,
       f.factor                                    AS fund_factor,
       ROUND(p.fte_hours_per_sprint * f.factor, 2) AS fund_hh_per_fte
FROM pi_periods p
JOIN v_pi_fund_factor f USING (pi_id)
ORDER BY p.start_date DESC
LIMIT 1
"""


def collect_business_metrics() -> dict[str, Any]:
    """Снимок бизнес-метрик: последний прогон планировщика и календарь PI."""
    started = time.perf_counter()
    run = db.query_one(BUSINESS_SQL) or {}
    calendar = db.query_one(CALENDAR_SQL) or {}
    return {
        "db_up": 1,
        "scrape_seconds": time.perf_counter() - started,
        "taken_at": time.time(),
        "runs_total": int(run.get("runs_total") or 0),
        "run": dict(run) if run else None,
        "calendar": dict(calendar) if calendar else None,
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
        self._in_flight = 0
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

    def observe(self, method: str, route: str, status: Any, seconds: float) -> None:
        """Записать факт запроса: код ответа и длительность."""
        key = (method, route, str(status))
        with self._lock:
            self._requests[key] += 1
            self._duration_sum[(method, route)] += seconds
            self._duration_count[(method, route)] += 1

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
            self._in_flight = 0
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
            in_flight = self._in_flight
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
            "Сумма и число наблюдений длительности запроса (summary без квантилей)",
            "summary",
            duration_samples,
        )
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
