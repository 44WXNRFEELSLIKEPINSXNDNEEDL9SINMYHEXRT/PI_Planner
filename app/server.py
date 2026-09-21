"""HTTP-сервер демо: `/api/health` и статика собранного фронта.

Запускается ровно так, как его зовёт `run.bat`:

    uv run python -m app.server --port 8000

Только стандартная библиотека: в `pyproject.toml` HTTP-фреймворка нет, и
ради одного эндпоинта тянуть FastAPI/uvicorn не нужно — `uv.lock` остаётся
без изменений.

Отношение к базе — **read-only**: единственный запрос к PostgreSQL это
`app.db.health()`, а он идёт в read-only сессии. Ни один маршрут этого
сервера не пишет в контракт планировщика.
"""
from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app import db

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
INDEX = DIST / "index.html"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

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

KNOWN_API = ("/api/health",)


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


class Handler(BaseHTTPRequestHandler):
    """GET/HEAD: `/api/*` — JSON, остальное — собранный фронт."""

    server_version = "pi-planner"
    sys_version = ""  # не светим версию Python в ответах и логах

    # ---------------------------------------------------------------- GET/HEAD
    def do_GET(self) -> None:  # noqa: N802 — имя задано стандартной библиотекой
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._api(path)
        else:
            self._static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

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

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {
                "error": "not_found",
                "message": f"нет такого эндпоинта: {path}",
                "known": list(KNOWN_API),
            },
        )

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

    def _respond(self, status: HTTPStatus, ctype: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # Демо живёт на локальной машине, кэш браузера только мешает правкам.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Браузер закрыл вкладку раньше ответа — это не ошибка сервера.
            pass

    # ------------------------------------------------------------------ логи
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write(f"[server] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()

    def log_error(self, fmt: str, *args: Any) -> None:
        self.log_message(fmt, *args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.server",
        description="Демо-сервер PI-Planner: /api/health и собранный фронт из web/dist.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="по умолчанию 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="по умолчанию 8000")
    args = parser.parse_args(argv)

    if not INDEX.is_file():
        print(f"[server] ВНИМАНИЕ: {INDEX} отсутствует, соберите фронт (npm run build)", flush=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"[server] слушаю {url}   (статика: {DIST})", flush=True)
    print(f"[server] проверка живости: {url}/api/health", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("", flush=True)
        print("[server] остановлен по Ctrl+C", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())