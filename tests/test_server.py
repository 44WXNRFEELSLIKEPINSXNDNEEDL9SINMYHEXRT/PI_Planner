"""Смоук-тесты сервера: поднимаем на свободном порту и дёргаем по HTTP.

Живая база не нужна — `app.db.health` и сборщик бизнес-метрик подменяются,
поэтому тесты проходят и на машине без PostgreSQL. Проверка на настоящей базе
описана в docs/RUNBOOK.md (разделы с приёмкой сервера и метрик).
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app import server

HEALTH = {
    "dsn": "host=127.0.0.1 port=5432 dbname=pi_planner user=postgres",
    "server_version": "17.11",
    "dbname": "pi_planner",
    "tables": 29,
    "views": 17,
}

# Снимок бизнес-метрик: форма ровно та, что отдаёт app.metrics.collect_business_metrics.
SNAPSHOT = {
    "db_up": 1,
    "scrape_seconds": 0.01,
    "taken_at": 1_700_000_000.0,
    "runs_total": 2,
    "run": {
        "run_id": 2,
        "as_of_sprint": 3,
        "status": "ok",
        "created_epoch": 1_700_000_000.0,
        "errors": 0,
        "warnings": 12,
        "in_quarter": 7,
        "assigned_hh": 522.01,
    },
    "calendar": {
        "pi_id": "PI-2026-Q3",
        "start_date": "2026-07-01",
        "end_date": "2026-09-30",
        "sprint_count": 7,
        "fund_factor": "6.5714",
        "fund_hh_per_fte": 525.71,
    },
}


def server_metrics(collector=None) -> server.Metrics:
    """Свежий реестр метрик: счётчики иначе текут из теста в тест."""
    return server.Metrics(
        app_version=server.APP_VERSION,
        etl_version=server.ETL_VERSION,
        pi_id=server.PI_ID,
        known_api=server.KNOWN_API,
        ttl=0.0,
        collector=collector or (lambda: dict(SNAPSHOT)),
    )


def samples(body: bytes) -> dict[str, float]:
    """Разбор вывода `/metrics`: ключ — образец без значения."""
    out: dict[str, float] = {}
    for line in body.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        out[name] = float(value)
    return out


@pytest.fixture()
def base_url(monkeypatch) -> str:
    """Сервер на порту 0 (свободный) в отдельном потоке + адрес для запросов."""
    monkeypatch.setattr(server.db, "health", lambda: dict(HEALTH))
    monkeypatch.setattr(server, "METRICS", server_metrics())
    # Логи сервера в тестах глушим: строки из потоков хендлеров иначе попадают
    # в вывод pytest уже после закрытия capture. Формат лога проверяется
    # отдельно — тесты ниже вызывают `log_event` напрямую и читают capsys.
    monkeypatch.setattr(server, "log_event", lambda *args, **kwargs: None)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def get(url: str) -> tuple[int, dict[str, str], bytes]:
    """GET без исключений на 4xx/5xx — статус нужен как значение."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_health_returns_db_facts(base_url: str) -> None:
    status, headers, body = get(f"{base_url}/api/health")

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body.decode("utf-8")) == HEALTH


def test_unknown_api_is_json_404(base_url: str) -> None:
    status, headers, body = get(f"{base_url}/api/nope")

    assert status == 404
    assert headers["Content-Type"].startswith("application/json")
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "not_found"
    # Список известных эндпоинтов — из константы, а не переписанный в тесте:
    # новый эндпоинт не должен ломать 404 у фронта.
    assert payload["known"] == list(server.KNOWN_API)
    assert "/metrics" in payload["known"]


def test_livez_answers_without_the_database(base_url: str, monkeypatch) -> None:
    """Liveness не ходит в базу: иначе перезапуск контейнера по чужой аварии."""

    def boom() -> dict:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(server.db, "health", boom)

    status, headers, body = get(f"{base_url}/api/livez")
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert payload["status"] == "alive"
    assert payload["version"] == server.APP_VERSION
    assert payload["uptime_seconds"] >= 0


def test_version_reports_app_etl_and_pi(base_url: str, monkeypatch) -> None:
    """Версии доступны без базы: devops спрашивает их до первой заливки данных."""

    def boom() -> dict:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(server.db, "health", boom)

    status, _, body = get(f"{base_url}/api/version")
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert payload["version"] == server.APP_VERSION
    assert payload["etl_version"] == server.ETL_VERSION
    assert payload["pi_id"] == server.PI_ID
    assert payload["git_sha"] is None  # локально переменной нет — и это честно
    assert "password" not in json.dumps(payload)


def test_metrics_is_prometheus_text(base_url: str) -> None:
    status, headers, body = get(f"{base_url}/metrics")
    text = body.decode("utf-8")
    parsed = samples(body)

    assert status == 200
    assert headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert "# HELP pi_planner_up" in text and "# TYPE pi_planner_up gauge" in text
    assert parsed["pi_planner_up"] == 1.0
    assert f'version="{server.APP_VERSION}"' in text
    assert parsed["pi_planner_db_up"] == 1.0
    assert parsed["pi_planner_plan_runs_total"] == 2.0
    assert parsed['pi_planner_plan_violations{run_id="2",severity="error"}'] == 0.0


def test_metrics_counts_requests_by_route(base_url: str) -> None:
    """`route` — фиксированный набор, а не URL: кардинальность не растёт с файлами."""
    get(f"{base_url}/api/livez")
    get(f"{base_url}/assets/nope-98765.js")  # 404, но маршрут «статика»

    parsed = samples(get(f"{base_url}/metrics")[2])

    assert parsed['pi_planner_http_requests_total{method="GET",route="/api/livez",status="200"}'] == 1.0
    assert parsed['pi_planner_http_requests_total{method="GET",route="/static",status="404"}'] == 1.0
    assert (
        parsed['pi_planner_http_request_duration_seconds_count{method="GET",route="/api/livez"}'] == 1.0
    )


def test_metrics_survives_the_database_being_down(base_url: str, monkeypatch) -> None:
    """База упала — /metrics всё равно 200 и говорит, почему нет бизнес-серий."""

    def boom() -> dict:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(server, "METRICS", server_metrics(collector=boom))

    status, _, body = get(f"{base_url}/metrics")
    parsed = samples(body)

    assert status == 200
    assert parsed["pi_planner_db_up"] == 0.0
    assert parsed['pi_planner_db_metrics_error{error_class="RuntimeError"}'] == 1.0
    assert "pi_planner_plan_runs_total" not in parsed


def test_metrics_reports_the_calendar_fund_factor(base_url: str) -> None:
    """Множитель фонда квартала виден в метриках: 6.5714, а не 7.0000 (ADR-017)."""
    parsed = samples(get(f"{base_url}/metrics")[2])

    assert (
        parsed[
            'pi_planner_calendar_info{pi_id="PI-2026-Q3",pi_start="2026-07-01",'
            'pi_end="2026-09-30",sprint_count="7",fund_factor="6.5714"}'
        ]
        == 525.71
    )


def test_json_log_line_is_a_single_object(capsys, monkeypatch) -> None:
    """JSON-режим: одна строка — один объект, разбирается парсером, а не регуляркой."""
    monkeypatch.setattr(server, "LOG_FORMAT", "json")

    server.log_event("probe", status=200, route="/api/livez")

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "probe"
    assert payload["level"] == "info"
    assert payload["service"] == "pi-planner"
    assert payload["version"] == server.APP_VERSION
    assert payload["status"] == 200
    assert payload["route"] == "/api/livez"
    assert payload["ts"].count("T") == 1  # ISO-8601 с местным смещением


def test_text_log_line_stays_readable(capsys) -> None:
    """Режим по умолчанию — текст: демо читают глазами, а не лог-сборщиком."""
    server.log_event("probe", status=200)

    out = capsys.readouterr().out
    assert out.startswith("[server] probe ")
    assert "status=200" in out


def test_database_down_gives_503_and_hides_password(base_url: str, monkeypatch) -> None:
    def boom() -> dict:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(server.db, "health", boom)
    # База может жить не на 127.0.0.1:5432: подсказка обязана называть реальный адрес.
    monkeypatch.setattr(
        server.db, "dsn", lambda: "host=10.20.30.40 port=5999 dbname=pi_planner user=postgres"
    )

    status, _, body = get(f"{base_url}/api/health")
    payload = json.loads(body.decode("utf-8"))

    assert status == 503
    assert payload["error"] == "database_unavailable"
    assert "connection refused" in payload["message"]
    assert "password" not in json.dumps(payload)
    assert payload["dsn"] == "host=10.20.30.40 port=5999 dbname=pi_planner user=postgres"
    assert "5999" in payload["hint"]
    assert "5432" not in payload["hint"]  # регрессия: адрес не захардкожен


def test_broken_dsn_config_still_gives_json_503(base_url: str, monkeypatch) -> None:
    """Сломанный dsn.json не должен превращать 503 в оборванное соединение."""

    def boom() -> dict:
        raise RuntimeError("connection refused")

    def broken_dsn() -> str:
        raise RuntimeError("dsn.json не парсится: Expecting value")

    monkeypatch.setattr(server.db, "health", boom)
    monkeypatch.setattr(server.db, "dsn", broken_dsn)

    status, _, body = get(f"{base_url}/api/health")
    payload = json.loads(body.decode("utf-8"))

    assert status == 503
    assert payload["error"] == "database_unavailable"
    assert payload["dsn"] is None
    assert "dsn.json" in payload["hint"]


def test_index_served_for_root(base_url: str) -> None:
    if not server.INDEX.is_file():
        pytest.skip("web/dist не собран: npm run build в web/")

    status, headers, body = get(f"{base_url}/")

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"<!doctype html" in body.lower()


def test_spa_path_falls_back_to_index(base_url: str) -> None:
    if not server.INDEX.is_file():
        pytest.skip("web/dist не собран: npm run build в web/")

    status, headers, _ = get(f"{base_url}/plan/3")

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"


def test_assets_are_served_with_type(base_url: str) -> None:
    assets = sorted((server.DIST / "assets").glob("*.js"))
    if not assets:
        pytest.skip("web/dist/assets пуст: npm run build в web/")

    status, headers, body = get(f"{base_url}/assets/{assets[0].name}")

    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert body


def test_missing_asset_is_404_not_index(base_url: str) -> None:
    status, _, body = get(f"{base_url}/assets/nope-12345.js")

    assert status == 404
    assert json.loads(body.decode("utf-8"))["error"] == "not_found"