"""HTTP-сервер демо: `/api/*`, `/metrics`, статика из `web/dist`.

Читает сервер только витрины из белого списка. Пишут три маршрута, и только
они (ТЗ: пользователь загружает датасет и факт спринтов):

* `POST /api/dataset` — xlsx датасета: ETL, заливка, базовый план;
* `GET  /api/actuals/template?sprint=N` — CSV-шаблон факта спринта;
* `POST /api/actuals?sprint=N` — факт спринта, пересборка состояния и пересчёт.

Тело загрузки — сам файл (`Content-Type` неважен, имя — в `?filename=`):
multipart в stdlib Python 3.13 разбирать нечем, а сырое тело отправляется
из браузера одной строкой `fetch(url, {method: "POST", body: file})`.

Запускается ровно так, как его зовёт `run.bat`:

    uv run python -m app.server --port 8000

Только стандартная библиотека: в `pyproject.toml` HTTP-фреймворка нет, и ради
нескольких эндпоинтов тянуть FastAPI/uvicorn не нужно — `uv.lock` остаётся без
изменений. Метрики отдаются в текстовом формате Prometheus (version=0.0.4)
руками, без `prometheus_client`; имена метрик — **замороженный контракт** для
devops, описан в docs/RUNBOOK.md («Контракт для мониторинга»). Переименование
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

Отношение к базе — **read-only**: единственные запросы к PostgreSQL это
`app.db.health()`, `app.db.query_one()` для бизнес-метрик и `app.views.fetch()`
для витрин, все три идут в read-only сессии. Ни один маршрут этого сервера не
пишет в контракт планировщика.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app import __version__ as APP_VERSION
from app import db, ingest, views
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
# серверу, но наружу его закрывает Caddy (`respond 404`) — см. RUNBOOK.
# `/api/views` — справочник витрин; сами витрины живут под ним же, но лейблом
# `route` становится `/api/views/{view}` (см. app/metrics.py).
KNOWN_API = (
    "/api/health", "/api/livez", "/api/version", "/api/views",
    "/api/dataset", "/api/actuals", "/api/actuals/template", "/metrics",
)

# Реестр метрик один на процесс: Handler создаётся на каждый запрос.
METRICS = Metrics(
    app_version=APP_VERSION,
    etl_version=ETL_VERSION,
    pi_id=PI_ID,
    known_api=KNOWN_API,
    ttl=METRICS_TTL_SECONDS,
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




def unavailable_payload(exc: Exception) -> dict[str, Any]:
    """Тело 503: причина, DSN без пароля и подсказка с РЕАЛЬНЫМ адресом базы.

    Адрес берём из `app.db.dsn()`, а не из константы: база может быть поднята
    на другом порту или хосте (`dsn.json`, `PI_PLANNER_DSN`), и подсказка про
    `127.0.0.1:5432` в этом случае уводит в сторону.

    `db.dsn()` вызываем терпимо: если `dsn.json` не парсится, падает и он —
    без этой защиты фронт вместо внятного 503 получил бы оборванное соединение.
    """
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
            if path.startswith("/api/") or path == "/metrics":
                self._api(path)
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
            self._upload(path)
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

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ingest.UploadError("пустое тело запроса: файл отправляется телом POST")
        if length > ingest.MAX_UPLOAD_BYTES:
            raise ingest.UploadError(
                f"файл больше {ingest.MAX_UPLOAD_BYTES // (1024 * 1024)} МБ"
            )
        return self.rfile.read(length)

    def _upload(self, path: str) -> None:
        query = parse_qs(urlparse(self.path).query)
        try:
            if path == "/api/dataset":
                body = self._read_body()
                result = ingest.load_dataset(body, self._query_param(query, "filename"))
            elif path == "/api/actuals":
                raw_sprint = self._query_param(query, "sprint")
                if raw_sprint is None or not raw_sprint.isdigit():
                    raise ingest.UploadError("укажите номер спринта: POST /api/actuals?sprint=N")
                body = self._read_body()
                result = ingest.load_actuals(
                    body, self._query_param(query, "filename"), int(raw_sprint)
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
            self._send_json(HTTPStatus.BAD_REQUEST, exc.payload())
            return
        except Exception as exc:  # noqa: BLE001 — база могла уйти, а фронту нужен ответ
            log_event("upload_failed", level="error", path=path, error=repr(exc))
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
            return
        log_event("upload_ok", path=path, run_id=result.get("plan", {}).get("run_id"))
        self._send_json(HTTPStatus.OK, result)

    # ------------------------------------------------------------------- API
    def _api(self, path: str) -> None:
        if path == "/api/health":
            try:
                payload = db.health()
            except Exception as exc:  # noqa: BLE001 — фронту нужен внятный ответ,
                # а не оборванное соединение: демо-машина может стартовать раньше PostgreSQL
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, unavailable_payload(exc))
                return
            self._send_json(HTTPStatus.OK, payload)
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
            self._send_json(HTTPStatus.OK, version_payload())
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
        target = DIST / rel if rel else INDEX
        if target.is_dir():
            target = target / "index.html"

        if not target.is_file() or not target.is_relative_to(DIST.resolve()):
            if target.suffix:  # такого ассета нет — честный 404, а не подмена на index.html
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": path})
                return
            target = INDEX  # SPA-fallback: у фронта один вход
        self._send_file(target)

    # ------------------------------------------------------------- отправка
    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._respond(status, "application/json; charset=utf-8", body)

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
            # Демо живёт на локальной машине, кэш браузера только мешает правкам.
            self.send_header("Cache-Control", "no-store")
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
    log_event(
        "server_started",
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
