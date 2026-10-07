"""Аутентификация и роли (S-2).

Схема — токен в заголовке `Authorization: Bearer <токен>`. Токен не лежит в
cookie, поэтому CSRF-атака через чужую страницу невозможна: браузер сам заголовок
не подставит. В базе хранится только SHA-256 токена (`app_users`).

Роли упорядочены: `viewer` < `planner` < `admin`.

* `viewer`  — читает витрины и шаблон факта;
* `planner` — дополнительно загружает факт спринта, подтверждает ETC и результаты;
* `admin`   — дополнительно загружает датасет (стирает цикл) и видит журнал.

Режимы (`PI_PLANNER_AUTH`):

* `required` (по умолчанию) — без валидного токена `/api/*` отвечает 401;
* `off` — проверка выключена, каждый запрос исполняется как `admin` «anonymous».
  Только для локальной отладки на доверенной машине; в логе при старте — предупреждение.

Аварийный администратор: `PI_PLANNER_ADMIN_TOKEN` (не короче 16 символов) работает
даже при недоступной базе. Остальных пользователей создаёт `tools/manage_users.py`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from app import db

ROLES = ("viewer", "planner", "admin")
RANK = {role: index for index, role in enumerate(ROLES)}
MIN_TOKEN_LENGTH = 16

# Подбор токена: не больше стольких неудач с одного адреса за окно.
MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 60.0


class AuthUnavailable(Exception):
    """База пользователей недоступна, а аварийный токен не подошёл: 503, не 401."""


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    source: str  # env | db | anonymous
    user_id: int | None = None

    def allows(self, required: str) -> bool:
        return RANK[self.role] >= RANK[required]

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role, "source": self.source}

    @property
    def owner_key(self) -> str:
        """Stable, namespace-separated owner identity for assistant resources."""
        if self.source == "db":
            if self.user_id is None:
                raise ValueError("database principal has no user_id")
            return f"db:{self.user_id}"
        return "env:admin" if self.source == "env" else "anonymous:local"


ANONYMOUS = Principal("anonymous", "admin", "anonymous")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mode() -> str:
    value = os.environ.get("PI_PLANNER_AUTH", "required").strip().lower()
    if value not in ("required", "off"):
        raise RuntimeError(f"PI_PLANNER_AUTH={value!r}: допустимо required или off")
    return value


def admin_token() -> str | None:
    token = os.environ.get("PI_PLANNER_ADMIN_TOKEN", "").strip()
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(f"PI_PLANNER_ADMIN_TOKEN короче {MIN_TOKEN_LENGTH} символов")
    return token


def bearer(header: str | None) -> str | None:
    """Токен из `Authorization: Bearer …` или None."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        return None
    return value


def authenticate(header: str | None) -> Principal | None:
    """Принципал по заголовку. None — токена нет или он неверный."""
    if mode() == "off":
        return ANONYMOUS
    token = bearer(header)
    if token is None:
        return None
    digest = hash_token(token)

    env_token = admin_token()
    if env_token is not None and hmac.compare_digest(digest, hash_token(env_token)):
        return Principal("admin-token", "admin", "env")

    try:
        row = db.query_one(
            "SELECT user_id, name, role FROM public.app_users WHERE token_sha256 = %s AND active", (digest,)
        )
    except Exception as exc:  # noqa: BLE001 — база недоступна: это не «неверный токен»
        raise AuthUnavailable(str(exc)) from exc
    if row is None:
        return None
    return Principal(str(row["name"]), str(row["role"]), "db", int(row["user_id"]))


# ---------------------------------------------------------------- политика
# (метод, путь) -> минимальная роль; None — маршрут публичный.
PUBLIC_GET = ("/api/livez", "/api/health", "/api/version")
ADMIN_POST = ("/api/dataset", "/api/pi-contexts")
PLANNER_POST = ("/api/actuals", "/api/actuals/role-review", "/api/tasks/goal-confirmation",
                "/api/tasks/skill-review", "/api/engineers/availability", "/api/engineers/qualifications",
                "/api/initiatives/priority", "/api/dq-issues/review", "/api/planner/compare")


def _assistant_required_role(method: str, path: str) -> str:
    parts = path.removeprefix("/api/assistant/").split("/")
    if not parts or parts == [""]:
        return "admin"
    if parts[0] == "profiles":
        return "viewer" if method in ("GET", "HEAD") else "admin"
    if parts[0] == "prompts":
        if len(parts) == 2 and parts[1] == "me":
            return "viewer"
        return "viewer" if method in ("GET", "HEAD") else "admin"
    if parts[0] == "kb":
        return "admin"
    if parts[0] == "conversations":
        if len(parts) == 4 and parts[3] == "compare" and parts[2] == "scenarios":
            return "planner"
        return "viewer"
    if parts[0] in ("jobs", "evidence"):
        return "viewer"
    return "admin"


def required_role(method: str, path: str) -> str | None:
    """Какая роль нужна для маршрута. None — доступ без токена."""
    if not path.startswith("/api/"):
        return None  # статика фронта: секретов в ней нет
    if path.startswith("/api/assistant/"):
        return _assistant_required_role(method, path)
    if method in ("GET", "HEAD"):
        if path in PUBLIC_GET:
            return None
        return "viewer"
    if path in ADMIN_POST:
        return "admin"
    if path in PLANNER_POST:
        return "planner"
    return "admin"  # неизвестный путь записи — самая строгая роль


# ---------------------------------------------------------- перебор токенов
class FailureLimiter:
    """Скользящее окно неудачных попыток по адресу клиента (в памяти процесса)."""

    def __init__(self, limit: int = MAX_FAILURES, window: float = FAILURE_WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque[float]:
        events = self._failures[key]
        while events and now - events[0] > self.window:
            events.popleft()
        return events

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            return len(self._trim(key, now)) >= self.limit

    def record(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._trim(key, now).append(now)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


LIMITER = FailureLimiter()


# ------------------------------------------------------------------ аудит
def audit(
    principal: Principal, action: str, outcome: str, *, target: str | None = None,
    client: str | None = None, detail: dict[str, Any] | None = None,
) -> None:
    """Дописать событие в `audit_log`. Сбой журнала не должен ронять запрос."""
    import json

    try:
        db.execute_write(
            "INSERT INTO audit_log (actor, role, action, target, outcome, client, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (principal.name, principal.role, action, target, outcome, client,
             json.dumps(detail or {}, ensure_ascii=False, default=str)),
        )
    except Exception:  # noqa: BLE001
        pass
