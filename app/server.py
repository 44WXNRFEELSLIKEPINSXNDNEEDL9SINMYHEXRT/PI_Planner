"""HTTP-сервер демо: `/api/*`, `/metrics`, статика из `web/dist`.

Читает сервер только витрины из белого списка. Пишут три маршрута, и только
они (ТЗ: пользователь загружает датасет и факт спринтов):

* `POST /api/dataset` — xlsx датасета: ETL, заливка, базовый план;
* `GET  /api/actuals/template?sprint=N` — CSV-шаблон факта спринта;
* `POST /api/actuals?sprint=N` — факт спринта, пересборка состояния и пересчёт;
* `POST /api/planner/compare` — двойной прогон greedy/LNS на одном входе.

Тело загрузки — сам файл (`Content-Type` неважен, имя — в `?filename=`):
multipart в stdlib Python 3.13 разбирать нечем, а сырое тело отправляется
из браузера одной строкой `fetch(url, {method: "POST", body: file})`.

Запускается ровно так, как его зовёт `run.bat`:

    uv run python -m app.server --port 8000

Только стандартная библиотека: в `pyproject.toml` HTTP-фреймворка нет, и ради
нескольких эндпоинтов тянуть FastAPI/uvicorn не нужно — `uv.lock` остаётся без
изменений. Метрики отдаются в текстовом формате Prometheus (version=0.0.4)
руками, без `prometheus_client`; имена метрик — **замороженный контракт** для
devops, описан в docs/OBSERVABILITY.md. Переименование
метрики или лейбла = сломанный дашборд, а не рефакторинг.

Роли эндпоинтов разные, и путать их нельзя:

* `/api/livez` — процесс жив, база **не** трогается: liveness-проба. Рестарт по
  ней недопустим, иначе контейнер перезапускается из-за упавшей базы;
* `/api/health` — готовность: 200 только если база отвечает, иначе 503 с
  причиной и подсказкой;
* `/api/version` — версии приложения и ETL, работает без базы;
* `/api/views` — справочник витрин, `/api/views/{view}` — строки витрины как
  есть (белый список и конверт — `app/views.py`, ADR-019): маршрут один, имя
  витрины — параметр пути, иначе фиксированный набор лейблов `route` раздулся бы
  до числа экранов;
* `/metrics` — всегда 200, даже при мёртвой базе (`pi_planner_db_up 0`): иначе
  мониторинг теряет вместе с метриками и причину их отсутствия.

Чтение витрин и метрик идёт в read-only сессиях. Явные операции записи
планировщика и загрузок проходят через транзакционные функции приложения.
"""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
import signal
import sys
import threading
import time

from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app import __version__ as APP_VERSION
from app import (
    absence, auth, availability, contexts, data_quality, db, ingest,
    plan_quality, qualifications, sensitivity, skill_review, trace, views, workforce,
)
from app.assistant import conversations, evidence, knowledge, prompts, providers, recommendations, scenarios
from app.metrics import NO_RESPONSE_STATUS, PROMETHEUS_CONTENT_TYPE, Metrics

try:  # версия ETL и PI живут в одном месте — etl/config.py, а не здесь
    from etl.config import ETL_VERSION, PI_ID
except Exception:  # noqa: BLE001 — сервер обязан подниматься и без ETL-пакета
    ETL_VERSION, PI_ID = "unknown", None

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
INDEX = DIST / "index.html"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Сколько живёт снимок бизнес-метрик из базы. Без кэша каждый scrape
# (у Prometheus это раз в 15 секунд, у нескольких инстансов — чаще) дёргал бы
# `v_plan_violations` — самую тяжёлую вьюху контракта.
METRICS_TTL_SECONDS = float(os.environ.get("PI_PLANNER_METRICS_TTL", "15"))

# mimetypes на Windows берёт типы из реестра и про UTF-8 не знает, поэтому
# для текстовых файлов кодировку выставляем сами: иначе кириллица в UI поедет.
MIME_OVERRIDES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

# Публичный контракт для фронта и devops. `/metrics` тоже здесь: он известен
# серверу, но наружу его закрывает Caddy (`respond 404`) — см. docs/OBSERVABILITY.md.
# `/api/views` — справочник витрин; сами витрины живут под ним же, но лейблом
# `route` становится `/api/views/{view}` (см. app/metrics.py).
KNOWN_API = (
    "/api/health", "/api/livez", "/api/version", "/api/views",
    "/api/planner/compare",
    "/api/me", "/api/dataset", "/api/actuals", "/api/actuals/template", "/api/actuals/role-review",
    "/api/scenarios/absence", "/api/scenarios/sensitivity", "/api/scenarios/workforce",
    "/api/tasks/goal-confirmation", "/api/tasks/skill-review", "/api/initiatives/priority",
    "/api/engineers/availability",
    "/api/engineers/qualifications",
    "/api/upload-revisions", "/api/upload-revisions/file", "/api/upload-revisions/snapshot",
    "/api/dq-issues/review", "/api/plan-quality", "/api/tasks/trace",
    "/api/pi-contexts", "/metrics",
)

# Реестр метрик один на процесс: Handler создаётся на каждый запрос.
METRICS = Metrics(
    app_version=APP_VERSION,
    etl_version=ETL_VERSION,
    pi_id=PI_ID,
    known_api=KNOWN_API,
    ttl=METRICS_TTL_SECONDS,
)

# Заголовки безопасности для каждого ответа (Caddy добавляет свои, но приложение
# должно быть безопасным и без него: run.sh, прямой порт).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
# Mantine вставляет стили из JS, поэтому style-src допускает inline; скрипты — только свои.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)

STARTED_AT = time.time()
LOG_FORMAT = "text"  # переключается --log-format / PI_PLANNER_LOG_FORMAT


def log_event(event: str, level: str = "info", **fields: Any) -> None:
    """Одна строка на событие: `text` для демо-консоли, `json` для devops.

    JSON-режим включается флагом `--log-format json` (или переменной
    `PI_PLANNER_LOG_FORMAT=json`): devops разбирает такие строки парсером, а
    не регулярками, и в каждой есть `ts`, `level`, `event` и `version`.
    """
    if LOG_FORMAT == "json":
        payload = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "level": level,
            "event": event,
            "service": "pi-planner",
            "version": APP_VERSION,
            **fields,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    else:
        tail = " ".join(f"{key}={value}" for key, value in fields.items())
        sys.stdout.write(f"[server] {event} {tail}".rstrip() + "\n")
    sys.stdout.flush()




def unavailable_payload(exc: Exception, public: bool = False) -> dict[str, Any]:
    """Тело 503: причина, DSN без пароля и подсказка с РЕАЛЬНЫМ адресом базы.

    Адрес берём из `app.db.dsn()`, а не из константы: база может быть поднята
    на другом порту или хосте (`dsn.json`, `PI_PLANNER_DSN`), и подсказка про
    `127.0.0.1:5432` в этом случае уводит в сторону.

    `db.dsn()` вызываем терпимо: если `dsn.json` не парсится, падает и он —
    без этой защиты фронт вместо внятного 503 получил бы оборванное соединение.

    `public=True` — вызов без проверенного токена: внутренности (адрес базы, текст
    исключения) не раскрываются.
    """
    if public:
        return {"error": "database_unavailable", "message": "база данных недоступна"}
    try:
        dsn = db.dsn()
    except Exception:  # noqa: BLE001 — диагностика не должна падать сильнее причины
        return {
            "error": "database_unavailable",
            "message": str(exc).strip(),
            "dsn": None,
            "hint": "строку подключения собрать не удалось — проверьте dsn.json "
            "(образец: dsn.example.json)",
        }
    return {
        "error": "database_unavailable",
        "message": str(exc).strip(),
        "dsn": dsn,
        "hint": f"PostgreSQL по адресу «{dsn}» не отвечает — запустите run.bat; "
        f"адрес и порт берутся из dsn.json или PI_PLANNER_DSN",
    }


def version_payload() -> dict[str, Any]:
    """Версии и границы контракта. Базы не касается — отвечает и без неё.

    `git_sha` приходит из окружения (`PI_PLANNER_GIT_SHA`): на сборке его
    проставляет CI, локально он пуст — и это честнее, чем выдуманное значение.
    """
    return {
        "service": "pi-planner",
        "version": APP_VERSION,
        "etl_version": ETL_VERSION,
        "pi_id": PI_ID,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "git_sha": os.environ.get("PI_PLANNER_GIT_SHA") or None,
        "started_at": datetime.fromtimestamp(STARTED_AT, timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds"),
        "uptime_seconds": round(time.time() - STARTED_AT, 3),
        "pid": os.getpid(),
    }


class Handler(BaseHTTPRequestHandler):
    """GET/HEAD: `/api/*` и `/metrics` — данные, остальное — собранный фронт.

    Каждый запрос логируется один раз и попадает в метрики: код ответа,
    маршрут из фиксированного набора и длительность. Штатный `log_request`
    молчит (см. ниже), иначе строка запроса печаталась бы дважды.
    """

    server_version = "pi-planner"
    sys_version = ""  # не светим версию Python в ответах и логах
    # Зависшее соединение (медленный клиент, обрыв посреди тела) не держит поток вечно.
    timeout = 30

    # ---------------------------------------------------------------- GET/HEAD
    def do_GET(self) -> None:  # noqa: N802 — имя задано стандартной библиотекой
        path = urlparse(self.path).path
        self._route = METRICS.route_label(path)
        self._status: Any = None
        self._bytes = 0
        started = time.perf_counter()
        error_class: str | None = None
        METRICS.enter()
        try:
            if not self._gate(path):
                pass  # ответ 401/403/429 уже отправлен
            elif path.startswith("/api/") or path == "/metrics":
                if (path in ("/api/livez", "/api/version", "/api/me", "/api/pi-contexts", "/metrics")
                        or path.startswith("/api/assistant/")):
                    self._api(path)
                else:
                    try:
                        with db.use_pi_context(self.headers.get("X-PI-ID"), self.headers.get("X-Scenario-ID")):
                            self._api(path)
                    except db.UnknownPIContext as exc:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "pi_not_found", "message": str(exc)})
            else:
                self._static(path)
        except Exception as exc:
            error_class = type(exc).__name__
            raise
        finally:
            # finally, а не после вызова: необработанное исключение в хендлере
            # тоже должно оставить след в логе и в счётчике (status=0 —
            # «ответ не отправлен»), иначе всплеск ошибок будет невидимым.
            METRICS.leave()
            seconds = time.perf_counter() - started
            status = self._status if self._status is not None else NO_RESPONSE_STATUS
            METRICS.observe(
                self.command,
                self._route,
                status,
                seconds,
                response_bytes=self._bytes,
                error_class=error_class,
            )
            log_event(
                "http_request",
                method=self.command,
                path=path,
                route=self._route,
                status=status,
                duration_ms=round(seconds * 1000, 3),
                bytes=self._bytes,
                client=self.address_string(),
            )

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        """Загрузки. Обвязка метрик и логов — та же, что у GET."""
        path = urlparse(self.path).path
        self._route = METRICS.route_label(path)
        self._status: Any = None
        self._bytes = 0
        started = time.perf_counter()
        error_class: str | None = None
        METRICS.enter()
        try:
            if self._gate(path):
                if path == "/api/pi-contexts" or path.startswith("/api/assistant/"):
                    self._upload(path)
                else:
                    try:
                        with db.use_pi_context(self.headers.get("X-PI-ID"), self.headers.get("X-Scenario-ID")):
                            self._upload(path)
                    except db.UnknownPIContext as exc:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "pi_not_found", "message": str(exc)})
        except Exception as exc:
            error_class = type(exc).__name__
            raise
        finally:
            METRICS.leave()
            seconds = time.perf_counter() - started
            status = self._status if self._status is not None else NO_RESPONSE_STATUS
            METRICS.observe(
                self.command,
                self._route,
                status,
                seconds,
                response_bytes=self._bytes,
                error_class=error_class,
            )
            log_event(
                "http_request",
                method=self.command,
                path=path,
                route=self._route,
                status=status,
                duration_ms=round(seconds * 1000, 3),
                bytes=self._bytes,
                client=self.address_string(),
            )

    def do_DELETE(self) -> None:  # noqa: N802
        """Удаление своего чата помощника и выключение профиля модели; остальные маршруты DELETE не поддерживают."""
        path = urlparse(self.path).path
        if not (path.startswith("/api/assistant/conversations/") or path.startswith("/api/assistant/profiles/")):
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED,
                            {"error": "method_not_allowed", "message": path})
            return
        self.do_POST()

    def do_PUT(self) -> None:  # noqa: N802
        """Assistant settings use PUT; reuse the authenticated write envelope."""
        path = urlparse(self.path).path
        if not path.startswith("/api/assistant/"):
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED,
                            {"error": "method_not_allowed", "message": path})
            return
        self.do_POST()

    # ------------------------------------------------------------ доступ (S-2)
    _principal: auth.Principal = auth.ANONYMOUS

    def _client(self) -> str:
        """Адрес клиента. За прокси (Caddy) настоящий адрес — в X-Forwarded-For."""
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _gate(self, path: str) -> bool:
        """Проверка токена и роли. False — ответ уже отправлен, маршрут не исполнять."""
        self._principal = auth.ANONYMOUS
        if path == "/metrics":
            return True  # внутренний маршрут: наружу его закрывает Caddy
        needed = auth.required_role(self.command, path)
        if needed is None:
            return True
        if auth.mode() == "off":
            return True
        client = self._client()
        if auth.LIMITER.blocked(client):
            log_event("auth_blocked", level="warning", client=client, path=path)
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "too_many_attempts", "message": "слишком много неудачных попыток входа, подождите минуту"},
                extra_headers={"Retry-After": "60"},
            )
            return False
        try:
            principal = auth.authenticate(self.headers.get("Authorization"))
        except auth.AuthUnavailable as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc, public=True))
            return False
        if principal is None:
            # Считаем только ПРЕДЪЯВЛЕННЫЕ неверные токены (подбор). Запрос вовсе без токена —
            # обычный первый заход фронта, он не должен приближать блокировку.
            presented = auth.bearer(self.headers.get("Authorization")) is not None
            if presented:
                auth.LIMITER.record(client)
            log_event("auth_denied", level="warning", client=client, path=path,
                      reason="bad_token" if presented else "no_token")
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "unauthorized", "message": "нужен токен доступа: заголовок Authorization: Bearer <токен>"},
                extra_headers={"WWW-Authenticate": 'Bearer realm="pi-planner"'},
            )
            return False
        if not principal.allows(needed):
            log_event("auth_denied", level="warning", client=client, path=path, user=principal.name,
                      reason="role", needed=needed)
            auth.audit(principal, f"{self.command} {path}", "rejected", client=client,
                       detail={"reason": "forbidden", "needed": needed})
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "forbidden", "message": f"нужна роль {needed}, у вас {principal.role}",
                 "required_role": needed, "role": principal.role},
            )
            return False
        self._principal = principal
        return True

    def _is_public_caller(self) -> bool:
        """Вызов без проверенного токена при включённой авторизации: тонкие ответы."""
        if auth.mode() == "off":
            return False
        try:
            return auth.authenticate(self.headers.get("Authorization")) is None
        except auth.AuthUnavailable:
            return True

    def _audited(self, action: str, outcome: str, **detail: Any) -> None:
        auth.audit(self._principal, action, outcome, client=self._client(), detail=detail)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ingest.UploadError("некорректный заголовок Content-Length") from None
        if length <= 0:
            raise ingest.UploadError("пустое тело запроса: файл отправляется телом POST")
        if length > ingest.MAX_UPLOAD_BYTES:
            raise ingest.UploadError(
                f"файл больше {ingest.MAX_UPLOAD_BYTES // (1024 * 1024)} МБ"
            )
        return self.rfile.read(length)

    def _upload(self, path: str) -> None:
        if path == "/api/assistant/kb/reindex":
            self._assistant_kb_reindex()
            return
        if path.startswith("/api/assistant/conversations") or path.startswith("/api/assistant/jobs/"):
            self._assistant_write(path)
            return
        if path.startswith("/api/assistant/profiles"):
            self._assistant_profile_write(path)
            return
        if path.startswith("/api/assistant/prompts/"):
            self._assistant_prompt_write(path)
            return
        query = parse_qs(urlparse(self.path).query)
        try:
            if path == "/api/planner/compare":
                try:
                    payload = json.loads(self._read_body())
                    result = ingest.compare_algorithms(
                        int(payload.get("as_of_sprint", 0)),
                        float(payload.get("search_time_seconds", 10)),
                    )
                except (TypeError, ValueError, OverflowError, json.JSONDecodeError) as exc:
                    raise ingest.UploadError("некорректные параметры двойного прогона", [str(exc)]) from None
            elif path == "/api/dataset":
                body = self._read_body()
                result = ingest.load_dataset(
                    body, self._query_param(query, "filename"), actor=self._principal.name,
                    idempotency_key=self.headers.get("Idempotency-Key"),
                )
            elif path == "/api/actuals":
                raw_sprint = self._query_param(query, "sprint")
                if raw_sprint is None or not raw_sprint.isdigit():
                    raise ingest.UploadError("укажите номер спринта: POST /api/actuals?sprint=N")
                body = self._read_body()
                result = ingest.load_actuals(
                    body, self._query_param(query, "filename"), int(raw_sprint),
                    confirm_complete=self._query_param(query, "confirm_complete") == "true",
                    actor=self._principal.name,
                    idempotency_key=self.headers.get("Idempotency-Key"),
                    confirm_duplicate=self._query_param(query, "confirm_duplicate") == "true",
                )
            elif path == "/api/actuals/role-review":
                try:
                    payload = json.loads(self._read_body())
                    result = ingest.confirm_role_etc(
                        str(payload["task_id"]), int(payload["role_id"]),
                        Decimal(str(payload["remaining_hours"])), str(payload["reason"]),
                        actor=self._principal.name,
                    )
                except (ValueError, TypeError, KeyError, InvalidOperation, json.JSONDecodeError) as exc:
                    raise ingest.UploadError("некорректные поля пересмотра роли", [str(exc)]) from None
            elif path == "/api/initiatives/priority":
                try:
                    payload = json.loads(self._read_body())
                    raw = payload.get("business_priority")
                    result = ingest.set_initiative_priority(
                        str(payload["prodf_id"]), None if raw in (None, "") else int(raw),
                        str(payload.get("note") or ""), actor=self._principal.name,
                    )
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise ingest.UploadError("некорректные поля приоритета инициативы", [str(exc)]) from None
            elif path == "/api/tasks/goal-confirmation":
                try:
                    payload = json.loads(self._read_body())
                    result = ingest.confirm_task_goal(
                        str(payload["task_id"]), str(payload["closure_code"]),
                        str(payload["goal_code"]) if payload.get("goal_code") else None,
                        # При включённой авторизации подтверждает вошедший пользователь, а не
                        # имя из тела запроса.
                        self._principal.name if auth.mode() == "required"
                        else str(payload["confirmed_by"]),
                        str(payload["note"]),
                    )
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise ingest.UploadError("некорректные поля подтверждения результата", [str(exc)]) from None
            elif path == "/api/tasks/skill-review":
                try:
                    payload = json.loads(self._read_body())
                    result = skill_review.save_review(
                        str(payload["task_id"]), int(payload["role_id"]),
                        [int(value) for value in payload["skill_ids"]],
                        str(payload["source_text"]), confirmed=payload["confirmed"] is True,
                        actor=self._principal.name if auth.mode() == "required"
                        else str(payload["reviewed_by"]),
                    )
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise ingest.UploadError("некорректные поля разметки стека", [str(exc)]) from None
            elif path == "/api/engineers/availability":
                try:
                    payload = json.loads(self._read_body())
                    raw_rate = payload.get("available_rate")
                    result = availability.set_rate(
                        str(payload["engineer_id"]), str(payload["team_id"]),
                        int(payload["sprint_no"]),
                        None if raw_rate in (None, "") else Decimal(str(raw_rate)),
                        str(payload.get("source_text") or ""),
                    )
                except (ValueError, TypeError, KeyError, InvalidOperation, json.JSONDecodeError) as exc:
                    raise ingest.UploadError("некорректные поля доступности", [str(exc)]) from None
            elif path == "/api/engineers/qualifications":
                try:
                    payload = json.loads(self._read_body())
                    result = qualifications.confirm(
                        str(payload["engineer_id"]), int(payload["role_id"]),
                        date.fromisoformat(str(payload["valid_from"])),
                        date.fromisoformat(str(payload["valid_until"])) if payload.get("valid_until") else None,
                        str(payload["source_text"]),
                        self._principal.name if auth.mode() == "required"
                        else str(payload["confirmed_by"]),
                    )
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise ingest.UploadError("некорректные поля квалификации", [str(exc)]) from None
            elif path == "/api/dq-issues/review":
                try:
                    payload = json.loads(self._read_body())
                    result = data_quality.review_issue(
                        int(payload["issue_id"]), str(payload["decision"]),
                        self._principal.name if auth.mode() == "required"
                        else str(payload["reviewer"]), str(payload["note"]),
                    )
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise ingest.UploadError("некорректные поля решения по находке", [str(exc)]) from None
            elif path == "/api/pi-contexts":
                result = contexts.create(
                    self._read_body(),
                    self._query_param(query, "filename") or "dataset.xlsx",
                    self._query_param(query, "pi_id") or "",
                    self._query_param(query, "scenario_id") or "main",
                    self._query_param(query, "start_date") or "",
                    actor=self._principal.name,
                )
            else:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": "not_found",
                        "message": path,
                        "known": ["/api/dataset", "/api/actuals?sprint=N"],
                    },
                )
                return
        except ingest.UploadError as exc:
            log_event("upload_rejected", level="warning", path=path, message=exc.message)
            self._audited(f"POST {path}", "rejected", message=exc.message)
            self._send_json(HTTPStatus.BAD_REQUEST, exc.payload())
            return
        except ingest.planner.PlanValidationError as exc:
            log_event("plan_rejected", level="error", path=path, run_id=exc.run_id,
                      errors=exc.errors)
            self._audited(f"POST {path}", "failed", reason="plan_validation_failed", run_id=exc.run_id)
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"error": "plan_validation_failed", "message": str(exc),
                 "run_id": exc.run_id, "violations_error": exc.errors,
                 "violations": exc.violations,
                 "hint": "Входные данные приняты; прогон сохранён для диагностики, но не опубликован."},
            )
            return
        except Exception as exc:  # noqa: BLE001 — база могла уйти, а фронту нужен ответ
            log_event("upload_failed", level="error", path=path, error=repr(exc))
            self._audited(f"POST {path}", "failed", error=type(exc).__name__)
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
            return
        run_id = (result.get("plan") or {}).get("run_id")
        if run_id is None and result.get("runs"):
            run_id = result["runs"][-1].get("run_id")
        log_event("upload_ok", path=path, run_id=run_id,
                  user=self._principal.name)
        self._audited(f"POST {path}", "ok", run_id=run_id,
                      run_ids=[item.get("run_id") for item in result.get("runs", [])])
        self._send_json(HTTPStatus.OK, result)

    def _assistant_profile_write(self, path: str) -> None:
        parts = path.removeprefix("/api/assistant/profiles").strip("/").split("/")
        try:
            if path == "/api/assistant/profiles" and self.command == "POST":
                profile_id = None
            elif len(parts) == 1 and parts[0].isdigit() and self.command == "PUT":
                profile_id = int(parts[0])
            elif len(parts) == 1 and parts[0].isdigit() and self.command == "DELETE":
                if not providers.deactivate(int(parts[0])):
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
                    return
                self._audited("assistant_profile_delete", "ok", profile_id=int(parts[0]))
                self._send_json(HTTPStatus.OK, {"deleted": True, "profile_id": int(parts[0])})
                return
            elif len(parts) == 2 and parts[0].isdigit() and parts[1] == "check" and self.command == "POST":
                profile = providers.get(int(parts[0]))
                if profile is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
                    return
                result = providers.check(profile)
                providers.record_check(int(parts[0]), result)
                self._audited("assistant_profile_check", "ok" if result["reachable"] else "failed",
                              profile_id=int(parts[0]))
                self._send_json(HTTPStatus.OK, result)
                return
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 16384:
                raise providers.ProfileError("profile body must be 1–16384 bytes")
            raw = json.loads(self.rfile.read(length))
            saved = providers.save(raw, profile_id)
            if saved is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
                return
            self._audited("assistant_profile_save", "ok", profile_id=saved["profile_id"])
            self._send_json(HTTPStatus.CREATED if profile_id is None else HTTPStatus.OK, saved)
        except (providers.ProfileError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "invalid_profile", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log_event("assistant_profile_failed", level="error", error=type(exc).__name__)
            self._audited("assistant_profile_save", "failed", error=type(exc).__name__)
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "profile_unavailable", "message": "Profile operation failed"})

    def _assistant_prompt_write(self, path: str) -> None:
        if self.command != "PUT" or path not in ("/api/assistant/prompts/default", "/api/assistant/prompts/me"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 16384:
                raise prompts.PromptError("prompt body must be 1–16384 bytes")
            raw = json.loads(self.rfile.read(length))
            if not isinstance(raw, dict) or set(raw) != {"content"}:
                raise prompts.PromptError("content is required")
            scope = "default" if path.endswith("/default") else "user"
            result = prompts.set_prompt(scope, raw["content"], self._principal)
            self._audited("assistant_prompt_set", "ok", scope=scope, prompt_id=result["prompt_id"])
            self._send_json(HTTPStatus.OK, result)
        except (prompts.PromptError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "invalid_prompt", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log_event("assistant_prompt_failed", level="error", error=type(exc).__name__)
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "prompt_unavailable", "message": "Prompt operation failed"})

    def _assistant_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 16384:
                raise conversations.ChatError("invalid_body_size")
            return json.loads(self.rfile.read(length))
        except conversations.ChatError:
            raise
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise conversations.ChatError("invalid_json") from exc

    def _assistant_write(self, path: str) -> None:
        parts = path.removeprefix("/api/assistant/").split("/")
        try:
            if parts == ["conversations"] and self.command == "POST":
                result = conversations.create(self._principal, self._assistant_body())
                status = HTTPStatus.CREATED
            elif len(parts) == 2 and parts[0] == "conversations" and self.command == "DELETE":
                result = conversations.delete(self._principal, parts[1])
                status = HTTPStatus.OK
            elif (len(parts) == 4 and parts[0] == "conversations"
                  and parts[2:] == ["scenarios", "compare"] and self.command == "POST"):
                result = scenarios.enqueue(self._principal, parts[1], self._assistant_body(),
                                           self.headers.get("Idempotency-Key"))
                status = HTTPStatus.ACCEPTED
            elif len(parts) == 3 and parts[0] == "conversations":
                cid, operation = parts[1:]
                if operation == "settings" and self.command == "PUT":
                    result = conversations.settings(self._principal, cid, self._assistant_body())
                    status = HTTPStatus.OK
                elif operation == "context" and self.command == "POST":
                    result = conversations.bind_context(self._principal, cid, self._assistant_body())
                    status = HTTPStatus.OK
                elif operation == "title" and self.command == "PUT":
                    result = conversations.rename(self._principal, cid, self._assistant_body())
                    status = HTTPStatus.OK
                elif operation == "messages" and self.command == "POST":
                    result = conversations.send_message(self._principal, cid, self._assistant_body(),
                                                        self.headers.get("Idempotency-Key"))
                    status = HTTPStatus.ACCEPTED
                else:
                    raise conversations.ChatError("route_not_found", 404)
            elif len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and self.command == "POST":
                result = conversations.cancel_job(self._principal, parts[1])
                status = HTTPStatus.OK
            else:
                raise conversations.ChatError("route_not_found", 404)
            self._audited("assistant_" + parts[0], "ok")
            self._send_json(status, result)
        except conversations.ChatError as exc:
            self._send_json(HTTPStatus(exc.status), {"error": exc.code, "message": exc.code})
        except Exception as exc:  # noqa: BLE001
            log_event("assistant_write_failed", level="error", error=type(exc).__name__)
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "assistant_unavailable", "message": "Assistant operation failed"})

    def _assistant_read(self, path: str) -> None:
        parts = path.removeprefix("/api/assistant/").split("/")
        query = parse_qs(urlparse(self.path).query)
        cursor = self._query_param(query, "cursor")
        try:
            if parts == ["conversations"]:
                result = conversations.list_conversations(self._principal, cursor)
            elif len(parts) == 2 and parts[0] == "conversations":
                result = conversations.get(self._principal, parts[1])
            elif len(parts) == 3 and parts[0] == "conversations" and parts[2] == "messages":
                result = conversations.list_messages(self._principal, parts[1], cursor)
            elif len(parts) == 3 and parts[0] == "conversations" and parts[2] == "recommendations":
                result = recommendations.list_for_conversation(self._principal, parts[1])
            elif len(parts) == 2 and parts[0] == "jobs":
                result = conversations.get_job(self._principal, parts[1])
            elif len(parts) == 2 and parts[0] == "evidence":
                result = evidence.get(self._principal, parts[1])
            else:
                raise conversations.ChatError("route_not_found", 404)
            self._send_json(HTTPStatus.OK, result)
        except conversations.ChatError as exc:
            self._send_json(HTTPStatus(exc.status), {"error": exc.code, "message": exc.code})
        except Exception as exc:  # noqa: BLE001
            log_event("assistant_read_failed", level="error", error=type(exc).__name__)
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "assistant_unavailable", "message": "Assistant operation failed"})

    def _assistant_kb_reindex(self) -> None:
        try:
            result = knowledge.enqueue(self._principal)
            self._audited("assistant_kb_reindex", "queued", job_id=result["job_id"])
            self._send_json(HTTPStatus.ACCEPTED, result)
        except knowledge.KnowledgeError as exc:
            status = HTTPStatus.CONFLICT if str(exc) == "knowledge_reindex_busy" else HTTPStatus.UNPROCESSABLE_ENTITY
            self._send_json(status, {"error": str(exc), "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log_event("assistant_kb_reindex_failed", level="error", error=type(exc).__name__)
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "knowledge_unavailable", "message": "Knowledge indexing unavailable"})

    # ------------------------------------------------------------------- API
    def _api(self, path: str) -> None:
        if path == "/api/assistant/kb/status":
            try:
                self._send_json(HTTPStatus.OK, knowledge.status())
            except Exception as exc:  # noqa: BLE001
                log_event("assistant_kb_status_failed", level="error", error=type(exc).__name__)
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": "knowledge_unavailable", "message": "Knowledge status unavailable"})
            return
        if (path.startswith("/api/assistant/conversations")
                or path.startswith("/api/assistant/jobs/")
                or path.startswith("/api/assistant/evidence/")):
            self._assistant_read(path)
            return
        if path in ("/api/assistant/prompts/default", "/api/assistant/prompts/me"):
            try:
                scope = "default" if path.endswith("/default") else "user"
                self._send_json(HTTPStatus.OK, prompts.get(scope, self._principal))
            except Exception as exc:  # noqa: BLE001
                log_event("assistant_prompt_failed", level="error", error=type(exc).__name__)
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": "prompt_unavailable", "message": "Prompt operation failed"})
            return
        if path == "/api/assistant/profiles":
            try:
                self._send_json(HTTPStatus.OK, {"profiles": providers.list_profiles(self._principal)})
            except Exception as exc:  # noqa: BLE001
                log_event("assistant_profiles_failed", level="error", error=type(exc).__name__)
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": "database_unavailable", "message": "Profiles are unavailable"})
            return
        if path == "/api/health":
            public = self._is_public_caller()
            try:
                payload = db.health()
            except Exception as exc:  # noqa: BLE001 — фронту нужен внятный ответ,
                # а не оборванное соединение: демо-машина может стартовать раньше PostgreSQL
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc, public=public))
                return
            # Без токена — только факт готовности: имя базы и версия сервера не раскрываются.
            self._send_json(HTTPStatus.OK, {"status": "ok"} if public else payload)
            return

        if path == "/api/livez":
            # Liveness: базу не трогаем вообще. Если проба ходит в базу,
            # оркестратор начинает перезапускать живой контейнер из-за чужой
            # аварии, а перезапуск базу не поднимает.
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "alive",
                    "version": APP_VERSION,
                    "uptime_seconds": round(time.time() - STARTED_AT, 3),
                    "pid": os.getpid(),
                },
            )
            return

        if path == "/api/version":
            payload = version_payload()
            if self._is_public_caller():
                payload = {key: payload[key] for key in ("service", "version")}
            self._send_json(HTTPStatus.OK, payload)
            return

        if path == "/api/me":
            self._send_json(HTTPStatus.OK, {**self._principal.as_dict(), "auth": auth.mode()})
            return

        if path == "/api/actuals/template":
            # Шаблон факта спринта: живые задачи и колонки ролей — чтобы
            # пользователю не пришлось угадывать формат.
            query = parse_qs(urlparse(self.path).query)
            raw_sprint = self._query_param(query, "sprint")
            try:
                name, body = ingest.actuals_template(
                    int(raw_sprint) if raw_sprint and raw_sprint.isdigit() else None
                )
            except ingest.UploadError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, exc.payload())
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            self._respond(
                HTTPStatus.OK, "text/csv; charset=utf-8", body,
                extra_headers={"Content-Disposition": f'attachment; filename="{name}"'},
            )
            return

        if path == "/api/views":
            # Справочник витрин: фронт получает контракт (имена, колонки сортировки,
            # экран) не из переписки, а из живого сервера.
            self._send_json(HTTPStatus.OK, views.catalog())
            return

        if path == "/api/tasks/skill-review":
            try:
                self._send_json(HTTPStatus.OK, skill_review.list_reviews())
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
            return

        if path == "/api/engineers/qualifications":
            engineer_id = self._query_param(parse_qs(urlparse(self.path).query), "engineer_id")
            if not engineer_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": "укажите engineer_id"})
                return
            try:
                self._send_json(HTTPStatus.OK, qualifications.list_for_engineer(engineer_id))
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
            return

        if path == "/api/upload-revisions":
            try:
                rows = db.query_dicts(
                    """SELECT revision_id, kind, pi_id, sprint_no, source_file, source_sha256,
                              idempotency_key, superseded_by, recorded_at,
                              plan_snapshot IS NOT NULL AS snapshot_available
                       FROM upload_revisions ORDER BY revision_id DESC LIMIT 500"""
                )
                self._send_json(HTTPStatus.OK, {"items": rows})
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
            return
        if path == "/api/upload-revisions/file":
            raw_id = self._query_param(parse_qs(urlparse(self.path).query), "id")
            if not raw_id or not raw_id.isdigit():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": "укажите id редакции"})
                return
            try:
                row = db.query_one("SELECT source_file, content FROM upload_revisions WHERE revision_id = %s",
                                   (int(raw_id),))
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            if row is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": "редакция не найдена"})
                return
            self._respond(HTTPStatus.OK, "application/octet-stream", bytes(row["content"]),
                          extra_headers={"Content-Disposition": f'attachment; filename="{row["source_file"]}"'})
            return
        if path == "/api/upload-revisions/snapshot":
            raw_id = self._query_param(parse_qs(urlparse(self.path).query), "id")
            if not raw_id or not raw_id.isdigit():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": "укажите id редакции"})
                return
            try:
                row = db.query_one("SELECT plan_snapshot FROM upload_revisions WHERE revision_id = %s",
                                   (int(raw_id),))
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            if row is None or row["plan_snapshot"] is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": "снимок не найден"})
                return
            self._send_json(HTTPStatus.OK, row["plan_snapshot"])
            return

        if path == "/api/pi-contexts":
            try:
                self._send_json(HTTPStatus.OK, contexts.list_contexts())
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
            return

        if path == "/api/scenarios/absence":
            query = parse_qs(urlparse(self.path).query)
            engineer_id = self._query_param(query, "engineer_id")
            raw_run_id = self._query_param(query, "run_id")
            if not engineer_id or not raw_run_id or not raw_run_id.isdigit():
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    "error": "bad_request",
                    "message": "укажите engineer_id и числовой run_id",
                })
                return
            try:
                result = absence.evaluate(engineer_id, int(raw_run_id))
            except absence.ScenarioUnavailable as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": "stale_run", "message": str(exc)})
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 — тот же контракт 503, что у витрин
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/scenarios/sensitivity":
            query = parse_qs(urlparse(self.path).query)
            raw_run_id = self._query_param(query, "run_id")
            if not raw_run_id or not raw_run_id.isdigit():
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    "error": "bad_request", "message": "укажите числовой run_id",
                })
                return
            try:
                result = sensitivity.evaluate(int(raw_run_id))
            except sensitivity.ScenarioUnavailable as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": "stale_run", "message": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/scenarios/workforce":
            query = parse_qs(urlparse(self.path).query)
            raw_run = self._query_param(query, "run_id")
            raw_role = self._query_param(query, "role_id")
            raw_sprint = self._query_param(query, "start_sprint")
            team_id = self._query_param(query, "team_id")
            if (not raw_run or not raw_run.isdigit() or not raw_role or not raw_role.isdigit()
                    or not raw_sprint or not raw_sprint.isdigit() or not team_id):
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    "error": "bad_request", "message": "укажите run_id, role_id, team_id и start_sprint",
                })
                return
            try:
                result = workforce.evaluate(int(raw_run), int(raw_role), team_id, int(raw_sprint))
            except workforce.ScenarioUnavailable as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": "stale_run", "message": str(exc)})
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/plan-quality":
            query = parse_qs(urlparse(self.path).query)
            raw_run_id = self._query_param(query, "run_id")
            if not raw_run_id or not raw_run_id.isdigit():
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    "error": "bad_request", "message": "укажите числовой run_id",
                })
                return
            try:
                result = plan_quality.evaluate(int(raw_run_id))
            except plan_quality.QualityUnavailable as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": "stale_run", "message": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/tasks/trace":
            query = parse_qs(urlparse(self.path).query)
            raw_run_id = self._query_param(query, "run_id")
            task_id = self._query_param(query, "task_id")
            if not raw_run_id or not raw_run_id.isdigit() or not task_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    "error": "bad_request", "message": "укажите run_id и task_id",
                })
                return
            try:
                result = trace.task_trace(int(raw_run_id), task_id)
            except trace.TraceUnavailable as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path.startswith("/api/views/"):
            self._views(path)
            return

        if path == "/metrics":
            # Всегда 200, даже если база мертва: иначе мониторинг теряет вместе
            # с метриками и причину их отсутствия (`pi_planner_db_up 0`).
            self._respond(
                HTTPStatus.OK,
                PROMETHEUS_CONTENT_TYPE,
                METRICS.render().encode("utf-8"),
            )
            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {
                "error": "not_found",
                "message": f"нет такого эндпоинта: {path}",
                "known": list(KNOWN_API),
                "hint": "витрины: GET /api/views (справочник), GET /api/views/{view} (строки)",
            },
        )

    def _views(self, path: str) -> None:
        """`GET /api/views/{view}` — строки витрины как есть плюс конверт (ADR-019).

        Три разных «плохо» различаются кодами, и фронт должен уметь их различать:
        404 — витрины нет в белом списке, 400 — параметр не прошёл проверку
        (включая попытку подсунуть SQL в `order`), 503 — база не отвечает.
        Пустая витрина — не ошибка: 200 и `count: 0`.
        """
        name = unquote(path[len("/api/views/") :])
        metric_view = name if name in views.BY_NAME else "_unknown"
        started = time.perf_counter()
        outcome = "error"
        returned = 0
        truncated = False
        query = parse_qs(urlparse(self.path).query)
        try:
            payload = views.fetch(
                name,
                run_id=views.parse_run_id(self._query_param(query, "run_id")),
                limit=views.parse_limit(self._query_param(query, "limit")),
                offset=views.parse_offset(self._query_param(query, "offset")),
                order=self._query_param(query, "order"),
            )
        except views.UnknownView as exc:
            outcome = "not_found"
            self._send_json(HTTPStatus.NOT_FOUND, exc.payload())
        except views.RunUnavailable as exc:
            outcome = "not_found"
            self._send_json(HTTPStatus.NOT_FOUND, exc.payload())
        except views.BadRequest as exc:
            outcome = "bad_request"
            self._send_json(HTTPStatus.BAD_REQUEST, exc.payload())
        except Exception as exc:  # noqa: BLE001 — как у /api/health: фронту нужен
            # внятный 503, а не оборванное соединение; демо-машина может стартовать
            # раньше PostgreSQL, а браузер кэширует «сервер недоступен» надолго
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
        else:
            outcome = "success"
            returned = int(payload.get("returned") or 0)
            truncated = bool(payload.get("truncated"))
            self._send_json(HTTPStatus.OK, payload)
        finally:
            METRICS.observe_view(
                metric_view,
                outcome,
                time.perf_counter() - started,
                rows=returned,
                truncated=truncated,
            )

    @staticmethod
    def _query_param(query: dict[str, list[str]], key: str) -> str | None:
        """Первый параметр запроса или None: пустая строка значит «не задан»."""
        values = query.get(key) or []
        return values[0] if values and values[0] != "" else None

    # ---------------------------------------------------------------- статика
    def _static(self, path: str) -> None:
        if not INDEX.is_file():
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "frontend_not_built",
                    "message": f"{INDEX} отсутствует",
                    "hint": "соберите фронт: cd web && npm run build",
                },
            )
            return

        rel = unquote(path).lstrip("/")
        dist_root = DIST.resolve()
        # resolve() раскрывает `..`, `%2e%2e` и симлинки ДО проверки границы (S-1):
        # без него `is_relative_to` сравнивал бы только текст пути.
        if "\x00" in rel:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
            return
        target = (DIST / rel).resolve() if rel else INDEX.resolve()
        if not target.is_relative_to(dist_root):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
            return
        if target.is_dir():
            target = target / "index.html"

        if not target.is_file():
            if target.suffix:  # такого ассета нет — честный 404, а не подмена на index.html
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
                return
            target = INDEX  # SPA-fallback: у фронта один вход
        self._send_file(target)

    # ------------------------------------------------------------- отправка
    def _send_json(self, status: HTTPStatus, payload: Any,
                   extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._respond(status, "application/json; charset=utf-8", body, extra_headers=extra_headers)

    def _send_file(self, path: Path) -> None:
        body = path.read_bytes()
        ctype = MIME_OVERRIDES.get(path.suffix.lower()) or "application/octet-stream"
        self._respond(HTTPStatus.OK, ctype, body)

    def _respond(
        self, status: HTTPStatus, ctype: str, body: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        # Факт ответа запоминаем для метрик и лога: `do_GET` смотрит сюда,
        # чтобы отличить «ответили 404» от «ответ не отправился вовсе».
        self._status = int(status)
        self._bytes = len(body)
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # Данные и состояние меняются при каждой загрузке: кэш браузера только мешает.
            self.send_header("Cache-Control", "no-store")
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Браузер закрыл вкладку раньше ответа — это не ошибка сервера.
            pass

    # ------------------------------------------------------------------ логи
    def log_request(self, code: Any = "-", size: Any = "-") -> None:  # noqa: N802
        """Молчим: запрос уже залогирован в `do_GET` — с длительностью и метриками."""
        return

    def log_message(self, fmt: str, *args: Any) -> None:
        """Сообщения самой библиотеки: битый запрос, неподдерживаемый метод."""
        log_event("http_note", message=fmt % args, client=self.address_string())

    def log_error(self, fmt: str, *args: Any) -> None:
        log_event("http_note", level="error", message=fmt % args, client=self.address_string())


def _install_signal_handlers(httpd: ThreadingHTTPServer) -> dict[str, Any]:
    """Штатная остановка по SIGTERM/SIGINT (на Windows ещё и SIGBREAK).

    `httpd.shutdown()` обязан вызываться из другого потока: из того же он ждёт
    завершения `serve_forever` и получается deadlock. Смысл в том, чтобы
    `docker stop` и оркестратор гасили процесс штатно, а не убивали его по
    таймауту вместе с недописанными ответами.

    Возвращаем изменяемый словарь: в него обработчик кладёт имя сигнала,
    чтобы `main` написал в лог, ПОЧЕМУ сервер встал.
    """
    state: dict[str, Any] = {"signal": None}

    def stop(signum: int, _frame: Any) -> None:
        state["signal"] = signal.Signals(signum).name
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):  # не главный поток или сигнал недоступен
            continue
    return state


def main(argv: list[str] | None = None) -> int:
    global LOG_FORMAT

    parser = argparse.ArgumentParser(
        prog="python -m app.server",
        description="Демо-сервер PI-Planner: API, метрики и собранный фронт из web/dist.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="по умолчанию 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="по умолчанию 8000")
    parser.add_argument(
        "--log-format",
        choices=("text", "json"),
        default=os.environ.get("PI_PLANNER_LOG_FORMAT", "text"),
        help="text — для консоли демо, json — для сборщика логов devops",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pi-planner {APP_VERSION} (ETL {ETL_VERSION}, PI {PI_ID})",
    )
    args = parser.parse_args(argv)
    LOG_FORMAT = args.log_format

    if not INDEX.is_file():
        log_event(
            "frontend_missing",
            level="warning",
            index=str(INDEX),
            hint="соберите фронт: cd web && npm run build",
        )

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    state = _install_signal_handlers(httpd)
    try:
        auth_mode = auth.mode()
        env_admin = auth.admin_token() is not None
    except RuntimeError as exc:
        raise SystemExit(f"[server] {exc}") from None
    if auth_mode == "off":
        log_event("auth_disabled", level="warning",
                  message="PI_PLANNER_AUTH=off: любой запрос исполняется как admin; только для доверенной машины")
    elif not env_admin:
        log_event("auth_no_admin_token", level="warning",
                  message="PI_PLANNER_ADMIN_TOKEN не задан: войти можно только пользователями из tools/manage_users.py")
    log_event(
        "server_started",
        auth=auth_mode,
        url=url,
        static=str(DIST),
        pi_id=PI_ID,
        log_format=LOG_FORMAT,
        liveness=f"{url}/api/livez",
        readiness=f"{url}/api/health",
        views=f"{url}/api/views",
        metrics=f"{url}/metrics",
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # Ctrl+C: обработчик сигнала мог не успеть
        state["signal"] = state["signal"] or "KeyboardInterrupt"
    finally:
        httpd.server_close()
    log_event(
        "server_stopped",
        reason=state["signal"] or "shutdown",
        uptime_seconds=round(time.time() - STARTED_AT, 3),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
