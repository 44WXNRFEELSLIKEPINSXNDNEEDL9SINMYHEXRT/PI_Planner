"""Тесты реестра метрик: формат Prometheus, лейблы и поведение без базы.

Реестр проверяется напрямую, без HTTP: `tests/test_server.py` смотрит на те же
метрики через `/metrics`, а здесь важна логика — кэш снимка, отказ базы и
кардинальность лейбла `route`. Живая база не нужна: коллектор подменяется.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app import metrics, server

# Набор известных маршрутов берём у сервера: лейблы метрик и маршруты не должны
# разъезжаться (ADR-018). Сам набор заморожен — тест ниже.
KNOWN = server.KNOWN_API

SNAPSHOT = {
    "db_up": 1,
    "scrape_seconds": 0.01,
    "taken_at": 1_700_000_000.0,
    "runs_total": 2,
    "run": {
        "run_id": 2,
        "as_of_sprint": 3,
        "status": "ok",
        "active_run_id": 2,
        "created_epoch": Decimal("1789996748.323258"),
        "errors": 0,
        "warnings": 12,
        "in_quarter": 7,
        "assigned_hh": Decimal("522.01"),
        "loan_hh": Decimal("40.00"),
        "load_seconds": Decimal("0.12"),
        "build_seconds": Decimal("0.34"),
        "write_seconds": Decimal("0.05"),
    },
    "calendar": {
        "pi_id": "PI-2026-Q3",
        "start_date": date(2026, 7, 1),
        "end_date": date(2026, 9, 30),
        "sprint_count": 7,
        "fund_factor": Decimal("6.5714"),
        "fund_hh_per_fte": Decimal("525.71"),
    },
    "decisions": [
        {"decision": "in_quarter", "reason": "none", "tasks": 7, "hours": Decimal("522")},
        {"decision": "deferred_next_pi", "reason": "M2", "tasks": 4, "hours": Decimal("132")},
    ],
    "alerts": [{"level": "orange", "alert_type": "role_deficit", "count": 4}],
    "kpis": [
        {
            "sprint_no": 3,
            "kpi_code": "say_do_ratio",
            "kind": "forecast",
            "value": Decimal("92.5"),
            "target_min": Decimal("90"),
            "target_max": Decimal("105"),
        }
    ],
    "dq": [{"severity": "warning", "count": 35}],
    "etl": {
        "loaded_epoch": Decimal("1789996000"),
        "etl_version": "1.1.0",
        "pi_start": date(2026, 7, 1),
    },
    "migrations": {"applied": 2, "pending": 0, "last_applied_epoch": 1_789_996_100},
}


def registry(collector=None, ttl: float = 0.0) -> metrics.Metrics:
    """Реестр с подменённым коллектором: база тестам не нужна."""
    return metrics.Metrics(
        app_version="0.3.0",
        etl_version="1.1.0",
        pi_id="PI-2026-Q3",
        known_api=KNOWN,
        ttl=ttl,
        collector=collector or (lambda: dict(SNAPSHOT)),
    )


def parse(text: str) -> dict[str, float]:
    """Разбор вывода: ключ — образец без значения, лейблы как есть."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        out[name] = float(value)
    return out


def test_render_is_prometheus_text_with_build_info() -> None:
    """Каждая метрика объявлена через HELP/TYPE — иначе Prometheus не примет."""
    text = registry().render()
    parsed = parse(text)

    assert "# TYPE pi_planner_up gauge" in text
    assert parsed["pi_planner_up"] == 1.0
    assert (
        'pi_planner_build_info{version="0.3.0",etl_version="1.1.0",'
        'pi_id="PI-2026-Q3"' in text
    )
    assert parsed["pi_planner_db_up"] == 1.0


def test_render_reports_the_last_run_and_the_calendar() -> None:
    """Бизнес-серии: последний прогон и множитель фонда квартала (ADR-017)."""
    parsed = parse(registry().render())

    assert parsed["pi_planner_plan_runs_total"] == 2.0
    assert parsed['pi_planner_plan_last_run_info{run_id="2",as_of_sprint="3",status="ok"}'] == 1.0
    assert parsed['pi_planner_plan_violations{run_id="2",severity="error"}'] == 0.0
    assert parsed['pi_planner_plan_violations{run_id="2",severity="warning"}'] == 12.0
    assert parsed['pi_planner_plan_tasks_in_quarter{run_id="2"}'] == 7.0
    assert parsed['pi_planner_plan_assigned_hours{run_id="2"}'] == 522.01
    assert parsed["pi_planner_plan_last_run_id"] == 2.0
    assert parsed["pi_planner_plan_loan_hours"] == 40.0
    assert parsed['pi_planner_plan_tasks{decision="deferred_next_pi",reason="M2"}'] == 4.0
    assert parsed['pi_planner_plan_alerts{level="orange",type="role_deficit"}'] == 4.0
    assert parsed['pi_planner_plan_kpi_value{kpi="say_do_ratio",sprint="3",kind="forecast"}'] == 92.5
    assert parsed["pi_planner_plan_active_run_id"] == 2.0
    assert parsed['pi_planner_data_quality_issues{severity="warning"}'] == 35.0
    assert parsed['pi_planner_job_last_duration_seconds{phase="build_plan"}'] == 0.34
    assert parsed["pi_planner_migrations_applied"] == 2.0
    assert parsed["pi_planner_migrations_pending"] == 0.0
    assert (
        'pi_planner_calendar_info{pi_id="PI-2026-Q3",pi_start="2026-07-01",'
        'pi_end="2026-09-30",sprint_count="7",fund_factor="6.5714"} 525.71' in registry().render()
    )


def test_forecast_and_actual_kpi_samples_are_distinct() -> None:
    snapshot = dict(SNAPSHOT)
    snapshot["kpis"] = [
        {"sprint_no": 6, "kpi_code": "pi_predictability", "kind": kind,
         "value": Decimal(value), "target_min": Decimal("80"), "target_max": Decimal("100")}
        for kind, value in (("forecast", "100"), ("actual", "0"))
    ]

    body = registry(collector=lambda: snapshot).render()
    samples = [line for line in body.splitlines() if line.startswith("pi_planner_plan_kpi_")]
    labels = [line.rpartition(" ")[0] for line in samples]

    assert len(labels) == len(set(labels))  # значения и оба порога имеют уникальные серии
    parsed = parse(body)
    assert parsed['pi_planner_plan_kpi_value{kpi="pi_predictability",sprint="6",kind="forecast"}'] == 100
    assert parsed['pi_planner_plan_kpi_value{kpi="pi_predictability",sprint="6",kind="actual"}'] == 0
    assert parsed['pi_planner_plan_kpi_target_min{kpi="pi_predictability",sprint="6",kind="actual"}'] == 80
    assert parsed['pi_planner_plan_kpi_target_max{kpi="pi_predictability",sprint="6",kind="forecast"}'] == 100


def test_plan_metrics_use_the_same_active_run_as_ui() -> None:
    for sql in (metrics.DECISIONS_SQL, metrics.ALERTS_SQL, metrics.KPI_SQL):
        assert "MAX(run_id)" in sql
        assert "status = 'ok'" in sql
    assert "k.kind" in metrics.KPI_SQL
    assert "active_run_id" in metrics.BUSINESS_SQL


def test_no_active_run_has_zero_id_and_keeps_last_attempt_visible() -> None:
    snapshot = dict(SNAPSHOT)
    snapshot["run"] = {**SNAPSHOT["run"], "status": "failed", "active_run_id": None}
    snapshot["kpis"] = []
    snapshot["decisions"] = []
    snapshot["alerts"] = []

    parsed = parse(registry(collector=lambda: snapshot).render())

    assert parsed["pi_planner_plan_active_run_id"] == 0
    assert parsed['pi_planner_plan_last_run_info{run_id="2",as_of_sprint="3",status="failed"}'] == 1
    assert not any(name.startswith("pi_planner_plan_kpi_value") for name in parsed)


def test_empty_plan_history_reports_zero_without_phantom_run() -> None:
    snapshot = {**SNAPSHOT, "runs_total": 0, "run": {"run_id": None, "active_run_id": None},
                "kpis": [], "decisions": [], "alerts": []}

    parsed = parse(registry(collector=lambda: snapshot).render())

    assert parsed["pi_planner_plan_runs_total"] == 0
    assert parsed["pi_planner_plan_active_run_id"] == 0
    assert not any(name.startswith("pi_planner_plan_last_run_info") for name in parsed)


def test_known_api_routes_are_frozen() -> None:
    """Набор известных маршрутов — часть контракта (ADR-018): меняется вместе с ADR.

    `/api/views` добавлен к замороженному набору вместе с витринами для фронта:
    это расширение набора, а не переименование меток.
    """
    assert server.KNOWN_API == (
        "/api/health",
        "/api/livez",
        "/api/version",
        "/api/views",
        "/api/planner/compare",
        # Текущий пользователь и роль (ADR-026).
        "/api/me",
        # Загрузки (ADR-021): ТЗ требует, чтобы датасет и факт спринтов
        # загружал пользователь, а не оператор из командной строки.
        "/api/dataset",
        "/api/actuals",
        "/api/actuals/template",
        "/api/actuals/role-review",
        "/api/scenarios/absence",
        "/api/scenarios/sensitivity",
        "/api/scenarios/workforce",
        "/api/tasks/goal-confirmation",
        "/api/tasks/skill-review",
        "/api/initiatives/priority",
        "/api/engineers/availability",
        "/api/engineers/qualifications",
        "/api/upload-revisions",
        "/api/upload-revisions/file",
        "/api/upload-revisions/snapshot",
        "/api/dq-issues/review",
        "/api/plan-quality",
        "/api/tasks/trace",
        "/api/pi-contexts",
        "/metrics",
    )


def test_route_label_is_bounded() -> None:
    """Лейбл маршрута — фиксированный набор: произвольный путь раздул бы кардинальность."""
    reg = registry()

    assert reg.route_label("/api/health") == "/api/health"
    assert reg.route_label("/api/livez") == "/api/livez"
    assert reg.route_label("/api/nope") == "/api/*"
    assert reg.route_label("/assets/index-a1b2c3.js") == "/static"
    assert reg.route_label("/") == "/static"


def test_views_share_a_single_route_label() -> None:
    """Все витрины — один лейбл: число витрин растёт вместе с UI, кардинальность нет."""
    reg = registry()

    assert reg.route_label("/api/views") == "/api/views"
    assert reg.route_label("/api/views/v_task_board") == "/api/views/{view}"
    assert reg.route_label("/api/views/v_orbit_map") == "/api/views/{view}"
    # Даже мусор в пути остаётся одной меткой: до SQL он не доходит (app/views.py).
    assert reg.route_label("/api/views/nope;DROP TABLE tasks") == "/api/views/{view}"


def test_counters_are_labelled_by_route_and_status() -> None:
    reg = registry()
    # Как в сервере: в счётчик уходит уже нормализованная метка маршрута.
    reg.observe("GET", reg.route_label("/api/livez"), 200, 0.01)
    reg.observe("GET", reg.route_label("/api/livez"), 200, 0.03)
    reg.observe("GET", reg.route_label("/api/nope"), 404, 0.02)

    parsed = parse(reg.render())

    assert parsed['pi_planner_http_requests_total{method="GET",route="/api/livez",status="200"}'] == 2.0
    assert parsed['pi_planner_http_requests_total{method="GET",route="/api/*",status="404"}'] == 1.0
    assert (
        parsed['pi_planner_http_request_duration_seconds_count{method="GET",route="/api/livez"}'] == 2.0
    )
    assert (
        parsed['pi_planner_http_request_duration_seconds_sum{method="GET",route="/api/livez"}'] == 0.04
    )
    assert (
        parsed[
            'pi_planner_http_request_duration_seconds_bucket{method="GET",route="/api/livez",le="0.05"}'
        ]
        == 2.0
    )


def test_response_bytes_exceptions_and_view_metrics_are_bounded() -> None:
    reg = registry()
    reg.observe("GET", "/api/*", 0, 0.02, response_bytes=17, error_class="RuntimeError")
    reg.observe_view("v_task_board", "success", 0.03, rows=5, truncated=True)

    parsed = parse(reg.render())

    assert parsed['pi_planner_http_response_bytes_total{method="GET",route="/api/*"}'] == 17.0
    assert parsed['pi_planner_http_exceptions_total{route="/api/*",error_class="RuntimeError"}'] == 1.0
    assert parsed['pi_planner_view_requests_total{view="v_task_board",outcome="success"}'] == 1.0
    assert parsed['pi_planner_view_rows_returned_total{view="v_task_board"}'] == 5.0
    assert parsed['pi_planner_view_truncated_total{view="v_task_board"}'] == 1.0


def test_snapshot_is_cached_for_the_ttl() -> None:
    """Снимок из базы берётся раз в ttl: scrape не должен бить по вьюхам каждый раз."""
    calls = []

    def collector() -> dict:
        calls.append(1)
        return dict(SNAPSHOT)

    reg = registry(collector=collector, ttl=60.0)
    reg.render()
    reg.render()

    assert len(calls) == 1

    # ttl = 0 — «без кэша»: так тесты и ручная проверка видят свежие данные.
    fresh = registry(collector=collector, ttl=0.0)
    fresh.render()
    fresh.render()
    assert len(calls) == 3


def test_failing_collector_still_returns_metrics() -> None:
    """Мёртвая база не делает /metrics недоступным: иначе теряется и причина."""

    def boom() -> dict:
        raise RuntimeError("connection refused")

    text = registry(collector=boom).render()
    parsed = parse(text)

    assert parsed["pi_planner_up"] == 1.0
    assert parsed["pi_planner_db_up"] == 0.0
    assert parsed['pi_planner_db_metrics_error{error_class="RuntimeError"}'] == 1.0
    assert "pi_planner_plan_runs_total" not in parsed  # бизнес-серий нет, но эндпоинт жив


def test_reset_clears_the_counters() -> None:
    reg = registry()
    reg.observe("GET", "/api/livez", 200, 0.01)
    reg.reset()

    parsed = parse(reg.render())

    assert not [key for key in parsed if key.startswith("pi_planner_http_requests_total")]


def test_sample_escapes_quotes_and_backslashes() -> None:
    """Экранирование обязательно: кавычка в значении ломает парсер Prometheus."""
    line = metrics._sample("m", {"a": 'он сказал "да"', "b": r"C:\путь"}, 1)

    assert line == 'm{a="он сказал \\"да\\"",b="C:\\\\путь"} 1'
