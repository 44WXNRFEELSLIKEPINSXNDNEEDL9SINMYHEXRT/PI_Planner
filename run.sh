#!/usr/bin/env bash
# Запуск сервиса одной командой на Linux и macOS (аналог run.bat для Windows).
#
#   ./run.sh              поднять базу при необходимости, залить, спланировать, стартовать
#   ./run.sh --reset      перезалить базу с нуля (сносит загруженный факт и прогоны)
#   ./run.sh --no-build   не собирать фронт
#
# Клиент psql не нужен: SQL заливается через tools/apply_sql.py.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${APP_PORT:-8000}"
RESET=0
BUILD=1
for arg in "$@"; do
  case "$arg" in
    --reset) RESET=1 ;;
    --no-build) BUILD=0 ;;
    *) echo "[run] неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- Python
# uv, если он свежий (проект требует >= 0.12), иначе обычный venv + pip:
# на чужой машине uv может быть старым, и это не повод не запуститься.
PY=""
if command -v uv >/dev/null 2>&1 && uv sync --frozen >/dev/null 2>&1; then
  PY="uv run --frozen --no-sync python"
  echo "[run] окружение: uv"
else
  [ -d .venv ] || { echo "[run] создаю .venv"; python3 -m venv .venv; }
  ./.venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
  ./.venv/bin/python -m pip install -q -r requirements.txt
  PY="./.venv/bin/python"
  echo "[run] окружение: .venv"
fi

# ------------------------------------------------------------- PostgreSQL
export PI_PLANNER_DSN="${PI_PLANNER_DSN:-host=127.0.0.1 port=5432 dbname=pi_planner user=postgres password=postgres}"
# Ждём чуть дольше: сразу после установки зависимостей первый заход в базу
# бывает медленным, а поднимать лишний контейнер поверх рабочей базы — хуже.
if ! $PY tools/apply_sql.py --wait 10 >/dev/null 2>&1; then
  # Проверяем не наличие бинарника, а РАБОТОСПОСОБНОСТЬ: docker без прав на
  # сокет есть почти везде, а пользы от него столько же, сколько от отсутствия.
  RUNTIME=""
  docker info >/dev/null 2>&1 && RUNTIME=docker
  [ -z "$RUNTIME" ] && podman info >/dev/null 2>&1 && RUNTIME=podman
  if [ -z "$RUNTIME" ]; then
    echo "[run] ОШИБКА: PostgreSQL недоступен, а docker/podman не найдены." >&2
    echo "[run] Поднимите базу сами и задайте PI_PLANNER_DSN." >&2
    exit 1
  fi
  echo "[run] поднимаю PostgreSQL 17 в $RUNTIME (контейнер pi-planner-pg)"
  $RUNTIME start pi-planner-pg >/dev/null 2>&1 || \
    $RUNTIME run -d --name pi-planner-pg \
      -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pi_planner \
      -p 5432:5432 postgres:17-alpine >/dev/null
  $PY tools/apply_sql.py --wait 90 || exit 1
fi

# --------------------------------------------------------------- схема
if [ "$RESET" = 1 ] || ! $PY tools/apply_sql.py --check >/dev/null 2>&1; then
  echo "[run] заливаю схему и данные"
  [ -f build/seed.sql ] || $PY etl/load.py
  $PY tools/apply_sql.py \
    db/01_schema.sql db/02_contract.sql build/seed.sql \
    db/03_substitutions.sql db/04_views.sql db/05_invariants.sql
fi

# ------------------------------------------------------------ базовый план
RUNS="$($PY - <<'PYEOF'
import sys
sys.path.insert(0, ".")
from app import db
try:
    print(db.scalar("SELECT COUNT(*) FROM plan_runs") or 0)
except Exception:
    print(0)
PYEOF
)"
if [ "${RUNS:-0}" = "0" ]; then
  echo "[run] строю базовый план (as_of_sprint = 0)"
  $PY tools/run_planner.py
fi

# ---------------------------------------------------------------- фронт
if [ "$BUILD" = 1 ] && [ ! -f web/dist/index.html ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "[run] собираю фронт"
    (cd web && npm install --silent && npm run build --silent)
  else
    echo "[run] npm не найден — сервер отдаст только API"
  fi
fi

echo "[run] сервис: http://127.0.0.1:${PORT}"
exec $PY -m app.server --port "$PORT"
