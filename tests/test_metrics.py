"""Тесты реестра метрик: формат Prometheus, лейблы и поведение без базы.

Реестр проверяется напрямую, без HTTP: `tests/test_server.py` смотрит на те же
метрики через `/metrics`, а здесь важна логика — кэш снимка, отказ базы и
кардинальность лейбла `route`. Живая база не нужна: коллектор подменяется.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app import metrics

KNOWN = ("/api/health", "/api/livez", "/api/version", "/metrics")

SNAPSHOT = {
    "db_up": 1,
    "scrape_seconds": 0.01,
    "taken_at": 1_700_000_000.0,
    "runs_total": 2,
    "run": {
        "run_id": 2,
        "as_of_sprint": 3,
        "status": "ok",
        "created_epoch": Decimal("1789996748.323258"),
        "errors": 0,
        "warnings": 12,
        "in_quarter": 7,
        "assigned_hh": Decimal("522.01"),
    },
    "calendar": {
        "pi_id": "PI-2026-Q3",
        "start_date": date(2026, 7, 1),
        "end_date": date(2026, 9, 30),
        "sprint_count": 7,
        "fund_factor": Decimal("6.5714"),
        "fund_hh_per_fte": Decimal("525.71"),
    },
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
    assert (
        'pi_planner_calendar_info{pi_id="PI-2026-Q3",pi_start="2026-07-01",'
        'pi_end="2026-09-30",sprint_count="7",fund_factor="6.5714"} 525.71' in registry().render()
    )


def test_route_label_is_bounded() -> None:
    """Лейбл маршрута — фиксированный набор: произвольный путь раздул бы кардинальность."""
    reg = registry()

    assert reg.route_label("/api/health") == "/api/health"
    assert reg.route_label("/api/livez") == "/api/livez"
    assert reg.route_label("/api/nope") == "/api/*"
    assert reg.route_label("/assets/index-a1b2c3.js") == "/static"
    assert reg.route_label("/") == "/static"


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
