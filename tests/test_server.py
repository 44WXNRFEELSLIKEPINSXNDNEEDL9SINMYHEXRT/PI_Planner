"""Смоук-тесты сервера: поднимаем на свободном порту и дёргаем по HTTP.

Живая база не нужна — `app.db.health` подменяется, поэтому тесты проходят и
на машине без PostgreSQL. Проверка на настоящей базе описана в docs/RUNBOOK.md
(раздел с приёмкой сервера).
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
    "views": 15,
}


@pytest.fixture()
def base_url(monkeypatch) -> str:
    """Сервер на порту 0 (свободный) в отдельном потоке + адрес для запросов."""
    monkeypatch.setattr(server.db, "health", lambda: dict(HEALTH))

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
    assert payload["known"] == ["/api/health"]


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