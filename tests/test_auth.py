"""Авторизация и роли (S-2), безопасность статики (S-1), заголовки и разбор запроса."""
from __future__ import annotations

import http.client
import json
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

from app import auth, ingest, server

ADMIN_TOKEN = "env-admin-token-0123456789"
USERS = {
    auth.hash_token("viewer-token-0123456789"): ("vera", "viewer"),
    auth.hash_token("planner-token-012345678"): ("pavel", "planner"),
    auth.hash_token("admin-token-0123456789ab"): ("alla", "admin"),
}


@pytest.fixture()
def secured(monkeypatch):
    """Сервер с включённой авторизацией; пользователи — из словаря, без базы."""
    monkeypatch.setenv("PI_PLANNER_AUTH", "required")
    monkeypatch.setenv("PI_PLANNER_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(auth.db, "query_one", lambda sql, params=None: (
        {"user_id": {"vera": 1, "pavel": 2, "alla": 3}[USERS[params[0]][0]],
         "name": USERS[params[0]][0], "role": USERS[params[0]][1]}
        if params and params[0] in USERS else None
    ))
    events: list[tuple] = []
    monkeypatch.setattr(auth.db, "execute_write", lambda sql, params=None, **kw: events.append(tuple(params or ())) or 1)
    monkeypatch.setattr(server.db, "health", lambda: {"dbname": "pi", "dsn": "secret-dsn", "server_version": "17"})
    monkeypatch.setattr(server, "log_event", lambda *args, **kwargs: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], events
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def call(port: int, method: str, path: str, token: str | None = None, body: bytes | None = None,
         headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    head = dict(headers or {})
    if token:
        head["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=body, headers=head)
    response = conn.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    conn.close()
    return result


def test_viewer_chat_routes_receive_verified_principal(secured, monkeypatch) -> None:
    port, _events = secured
    seen = []
    monkeypatch.setattr(server.conversations, "create", lambda principal, body: (
        seen.append((principal.owner_key, body["scope"])) or {"conversation_id": "chat"}))
    monkeypatch.setattr(server.conversations, "send_message", lambda principal, cid, body, key: (
        seen.append((principal.owner_key, cid, key)) or {"job_id": "job", "status": "queued"}))
    status, _, _ = call(port, "POST", "/api/assistant/conversations", "viewer-token-0123456789",
                        json.dumps({"scope": "knowledge"}).encode())
    assert status == 201
    status, _, _ = call(port, "POST", "/api/assistant/conversations/chat/messages",
                        "viewer-token-0123456789", json.dumps({"text": "привет"}).encode(),
                        {"Idempotency-Key": "request-1"})
    assert status == 202
    assert seen == [("db:1", "knowledge"), ("db:1", "chat", "request-1")]


def test_kb_management_requires_admin(secured, monkeypatch) -> None:
    port, _events = secured
    monkeypatch.setattr(server.knowledge, "status", lambda: {"revision": None, "documents": 0})
    monkeypatch.setattr(server.knowledge, "enqueue", lambda principal: {
        "job_id": principal.owner_key, "status": "queued"})
    status, _, _ = call(port, "GET", "/api/assistant/kb/status", "viewer-token-0123456789")
    assert status == 403
    status, _, body = call(port, "GET", "/api/assistant/kb/status", ADMIN_TOKEN)
    assert status == 200 and json.loads(body)["documents"] == 0
    status, _, body = call(port, "POST", "/api/assistant/kb/reindex", ADMIN_TOKEN)
    assert status == 202 and json.loads(body)["job_id"] == "env:admin"


def test_evidence_route_uses_verified_owner(secured, monkeypatch) -> None:
    port, _events = secured
    monkeypatch.setattr(server.evidence, "get", lambda principal, eid: {
        "owner": principal.owner_key, "evidence_id": eid})
    status, _, body = call(port, "GET", "/api/assistant/evidence/record-1",
                           "viewer-token-0123456789")
    assert status == 200
    assert json.loads(body) == {"owner": "db:1", "evidence_id": "record-1"}


def test_scenario_route_requires_planner_role(secured, monkeypatch) -> None:
    port, _events = secured
    seen = []
    monkeypatch.setattr(server.scenarios, "enqueue", lambda principal, cid, body, key: (
        seen.append((principal.owner_key, cid, key)) or {"job_id": "job", "status": "queued"}))
    body = json.dumps({"expected_context_revision": 1, "alternatives": [{"measures": []}]}).encode()
    path = "/api/assistant/conversations/chat/scenarios/compare"
    status, _, _ = call(port, "POST", path, "viewer-token-0123456789", body,
                        {"Idempotency-Key": "scenario-1"})
    assert status == 403
    status, _, _ = call(port, "POST", path, "planner-token-012345678", body,
                        {"Idempotency-Key": "scenario-1"})
    assert status == 202
    assert seen == [("db:2", "chat", "scenario-1")]


def test_recommendations_route_is_owned(secured, monkeypatch) -> None:
    port, _events = secured
    monkeypatch.setattr(server.recommendations, "list_for_conversation", lambda principal, cid: {
        "owner": principal.owner_key, "conversation": cid, "recommendations": []})
    status, _, body = call(port, "GET", "/api/assistant/conversations/chat/recommendations",
                           "viewer-token-0123456789")
    assert status == 200
    assert json.loads(body)["owner"] == "db:1"


# ------------------------------------------------------------------ S-1
@pytest.mark.parametrize("path", [
    "/%2e%2e/%2e%2e/app/db.py",
    "/..%2f..%2fapp/db.py",
    "/%2e%2e%2f%2e%2e%2fapp/db.py",
    "/assets/../../app/db.py",
    "/%2e%2e/%2e%2e/etc/passwd",
])
def test_static_path_traversal_is_blocked(secured, path) -> None:
    port, _ = secured
    status, _headers, body = call(port, "GET", path)
    assert status == 404, path
    assert b"PostgreSQL" not in body and b"root:" not in body


def test_static_null_byte_is_rejected(secured) -> None:
    port, _ = secured
    status, _headers, _body = call(port, "GET", "/index.html%00.py")
    assert status in (404, 400)


def test_static_still_serves_the_frontend_without_a_token(secured) -> None:
    port, _ = secured
    status, headers, body = call(port, "GET", "/")
    assert status == 200 and b"<html" in body.lower()
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]


# ------------------------------------------------------------------ S-2
def test_api_requires_a_token(secured) -> None:
    port, _ = secured
    status, headers, body = call(port, "GET", "/api/views")
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Bearer")
    assert json.loads(body)["error"] == "unauthorized"


def test_wrong_token_is_401_and_not_a_server_error(secured) -> None:
    port, _ = secured
    assert call(port, "GET", "/api/views", token="definitely-not-a-token-1")[0] == 401
    status, _h, _b = call(port, "GET", "/api/views", headers={"Authorization": "Basic abc"})
    assert status == 401


def test_viewer_reads_but_cannot_write(secured) -> None:
    port, _ = secured
    assert call(port, "GET", "/api/me", token="viewer-token-0123456789")[0] == 200
    status, _h, body = call(port, "POST", "/api/actuals?sprint=1", token="viewer-token-0123456789", body=b"x")
    assert status == 403
    payload = json.loads(body)
    assert payload["required_role"] == "planner" and payload["role"] == "viewer"


def test_setting_initiative_priority_needs_the_planner_role(secured) -> None:
    port, _ = secured
    body = json.dumps({"prodf_id": "P", "business_priority": 90, "note": "решение комитета"}).encode()
    assert call(port, "POST", "/api/initiatives/priority", token="viewer-token-0123456789", body=body)[0] == 403
    status, _h, payload = call(port, "POST", "/api/initiatives/priority", token="planner-token-012345678", body=b"{}")
    assert status == 400 and json.loads(payload)["error"] == "bad_upload"  # шлюз пройден, поля не заполнены


def test_planner_cannot_load_the_dataset_but_passes_the_gate_for_actuals(secured) -> None:
    port, _ = secured
    assert call(port, "POST", "/api/dataset", token="planner-token-012345678", body=b"x")[0] == 403
    # Дальше шлюза запрос доходит до разбора: пустое тело — 400, а не 401/403.
    status, _h, _b = call(port, "POST", "/api/actuals?sprint=1", token="planner-token-012345678")
    assert status == 400


def test_double_run_is_planner_only(secured, monkeypatch) -> None:
    port, _ = secured
    payload = json.dumps({"as_of_sprint": 0, "search_time_seconds": 5}).encode()
    monkeypatch.setattr(ingest, "compare_algorithms", lambda *_args: {"runs": []})
    assert call(port, "POST", "/api/planner/compare", token="viewer-token-0123456789",
                body=payload)[0] == 403
    status, _headers, body = call(
        port, "POST", "/api/planner/compare", token="planner-token-012345678",
        body=payload, headers={"Content-Length": str(len(payload))},
    )
    assert status == 200 and json.loads(body) == {"runs": []}


def test_admin_passes_the_gate_for_the_dataset(secured) -> None:
    port, _ = secured
    status, _h, _b = call(port, "POST", "/api/dataset", token="admin-token-0123456789ab")
    assert status == 400  # пустое тело отклонено разбором, шлюз пройден


def test_emergency_admin_token_works_without_the_user_table(secured) -> None:
    port, _ = secured
    status, _h, body = call(port, "GET", "/api/me", token=ADMIN_TOKEN)
    assert status == 200
    assert json.loads(body) == {"name": "admin-token", "role": "admin", "source": "env", "auth": "required"}


def test_health_and_version_are_thin_without_a_token(secured) -> None:
    port, _ = secured
    status, _h, body = call(port, "GET", "/api/health")
    assert status == 200 and json.loads(body) == {"status": "ok"}
    status, _h, body = call(port, "GET", "/api/version")
    assert status == 200 and set(json.loads(body)) == {"service", "version"}
    status, _h, body = call(port, "GET", "/api/health", token="viewer-token-0123456789")
    assert json.loads(body)["dbname"] == "pi"  # с токеном — полные данные


def test_failed_attempts_are_rate_limited(secured) -> None:
    port, _ = secured
    for _ in range(auth.MAX_FAILURES):
        assert call(port, "GET", "/api/views", token="bad-token-bad-token-1")[0] == 401
    status, headers, _b = call(port, "GET", "/api/views", token="bad-token-bad-token-1")
    assert status == 429 and headers["Retry-After"] == "60"
    # Верный токен с того же адреса тоже ждёт: окно считается по адресу.
    assert call(port, "GET", "/api/views", token="viewer-token-0123456789")[0] == 429


def test_requests_without_a_token_are_not_counted_as_guessing(secured) -> None:
    port, _ = secured
    for _ in range(auth.MAX_FAILURES + 5):
        assert call(port, "GET", "/api/me")[0] == 401
    assert call(port, "GET", "/api/me", token="viewer-token-0123456789")[0] == 200


def test_forbidden_attempt_is_written_to_the_audit_log(secured) -> None:
    port, events = secured
    call(port, "POST", "/api/dataset", token="planner-token-012345678", body=b"x")
    assert any(event[0] == "pavel" and event[3] == "rejected" or "rejected" in event for event in events)


def test_auth_unavailable_database_is_503_without_internals(secured, monkeypatch) -> None:
    port, _ = secured

    def broken(sql, params=None):
        raise RuntimeError("password authentication failed for user postgres at 10.0.0.5")

    monkeypatch.setattr(auth.db, "query_one", broken)
    status, _h, body = call(port, "GET", "/api/views", token="some-db-user-token-1234")
    assert status == 503
    assert b"10.0.0.5" not in body and b"password" not in body


def test_goal_confirmation_uses_the_logged_in_user_not_the_body(secured, monkeypatch) -> None:
    port, _ = secured
    seen: dict = {}

    def fake(task_id, closure, goal, confirmed_by, note):
        seen["by"] = confirmed_by
        return {"ok": True}

    monkeypatch.setattr(ingest, "confirm_task_goal", fake)
    payload = json.dumps({"task_id": "A", "closure_code": "ACHIEVED", "goal_code": "R4",
                          "confirmed_by": "someone-else", "note": "ok"}).encode()
    status, _h, _b = call(port, "POST", "/api/tasks/goal-confirmation", token="planner-token-012345678",
                          body=payload, headers={"Content-Length": str(len(payload))})
    assert status == 200 and seen["by"] == "pavel"


def test_off_mode_needs_no_token(monkeypatch, secured) -> None:
    port, _ = secured
    monkeypatch.setenv("PI_PLANNER_AUTH", "off")
    status, _h, body = call(port, "GET", "/api/me")
    assert status == 200 and json.loads(body)["source"] == "anonymous"


# --------------------------------------------------------- разбор запроса
def test_non_numeric_content_length_is_400(secured) -> None:
    port, _ = secured
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(
        b"POST /api/actuals?sprint=1 HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n"
        b"Authorization: Bearer planner-token-012345678\r\n\r\n"
    )
    data = sock.recv(4096).decode("utf-8", "replace")
    sock.close()
    assert data.startswith("HTTP/1.0 400") or data.startswith("HTTP/1.1 400"), data[:60]


# ------------------------------------------------------------------ модуль
def test_required_role_policy() -> None:
    assert auth.required_role("GET", "/api/livez") is None
    assert auth.required_role("GET", "/assets/x.js") is None
    assert auth.required_role("GET", "/api/views/plan_runs") == "viewer"
    assert auth.required_role("POST", "/api/actuals") == "planner"
    assert auth.required_role("POST", "/api/dq-issues/review") == "planner"
    assert auth.required_role("POST", "/api/dataset") == "admin"
    assert auth.required_role("POST", "/api/pi-contexts") == "admin"
    assert auth.required_role("POST", "/api/anything-new") == "admin"  # неизвестная запись — строго


def test_principal_ranks() -> None:
    admin = auth.Principal("a", "admin", "db")
    viewer = auth.Principal("v", "viewer", "db")
    assert admin.allows("planner") and not viewer.allows("planner") and viewer.allows("viewer")


def test_short_admin_token_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("PI_PLANNER_ADMIN_TOKEN", "short")
    with pytest.raises(RuntimeError):
        auth.admin_token()


def test_invalid_mode_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("PI_PLANNER_AUTH", "maybe")
    with pytest.raises(RuntimeError):
        auth.mode()


def test_bearer_parsing() -> None:
    assert auth.bearer("Bearer abc") == "abc"
    assert auth.bearer("bearer   abc ") == "abc"
    assert auth.bearer("Basic abc") is None and auth.bearer("Bearer") is None and auth.bearer(None) is None


def test_failure_limiter_window_expires() -> None:
    limiter = auth.FailureLimiter(limit=2, window=10)
    limiter.record("x", now=0)
    limiter.record("x", now=1)
    assert limiter.blocked("x", now=2)
    assert not limiter.blocked("x", now=20)
    assert not limiter.blocked("y", now=2)
