@echo off
rem =====================================================================
rem  run.bat — одна команда для запуска демо.
rem  Порядок: uv sync -> PostgreSQL -> web/dist -> сервер -> браузер.
rem  Скрипт идемпотентный: повторный запуск ничего не ломает.
rem =====================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "APP_PORT=8000"
set "APP_URL=http://127.0.0.1:%APP_PORT%"

rem ---------- 1. uv ----------
set "UV="
where uv >nul 2>nul && set "UV=uv"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV (
  echo [run] ОШИБКА: uv не найден.
  echo [run] Установите: powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
  pause & exit /b 1
)

rem ---------- 2. Python-окружение ----------
if not exist ".venv" (
  echo [run] создаю .venv: %UV% sync --frozen
  call "%UV%" sync --frozen || (echo [run] ОШИБКА: uv sync не прошёл & pause & exit /b 1)
)

rem ---------- 3. PostgreSQL ----------
set "PGBIN="
if exist "%USERPROFILE%\pg17\pgsql\bin\pg_isready.exe" set "PGBIN=%USERPROFILE%\pg17\pgsql\bin"
if not defined PGBIN if exist "C:\pgsql\bin\pg_isready.exe" set "PGBIN=C:\pgsql\bin"
if not defined PGBIN (
  for /f "delims=" %%I in ('where pg_isready.exe 2^>nul') do if not defined PGBIN set "PGBIN=%%~dpI"
)

if defined PGBIN (
  "%PGBIN%\pg_isready.exe" -h 127.0.0.1 -p 5432 >nul 2>nul
  if errorlevel 1 (
    if exist "%USERPROFILE%\pg17\data\PG_VERSION" (
      echo [run] PostgreSQL не отвечает, поднимаю pg_ctl...
      "%PGBIN%\pg_ctl.exe" -D "%USERPROFILE%\pg17\data" -l "%USERPROFILE%\pg17\pg.log" start
      timeout /t 3 /nobreak >nul
    )
  )
  "%PGBIN%\pg_isready.exe" -h 127.0.0.1 -p 5432 >nul 2>nul
  if errorlevel 1 (
    echo [run] ОШИБКА: PostgreSQL на 127.0.0.1:5432 недоступен.
    echo [run] Запустите его вручную или через Docker: docker run -d --name pi-planner-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pi_planner -p 5432:5432 postgres:17
    pause & exit /b 1
  )
  echo [run] PostgreSQL отвечает на 127.0.0.1:5432
) else (
  echo [run] ВНИМАНИЕ: pg_isready.exe не найден, пропускаю проверку БД.
)

rem ---------- 4. Собранный фронт ----------
set "NPM="
where npm >nul 2>nul && set "NPM=npm"
if not defined NPM if exist "%~dp0tools\node\npm.cmd" (
  set "NPM=%~dp0tools\node\npm.cmd"
  rem npm.cmd зовёт `node` по имени, поэтому каталог портативного Node нужен в
  rem PATH, иначе `tsc --noEmit` падает с «'node' is not recognized».
  set "PATH=%~dp0tools\node;%PATH%"
)

rem Собранный фронт лежит в коммите: на стенде без Node и без интернета он должен
rem просто работать, поэтому пересборка здесь никогда не обязательна — кроме
rem случая, когда web\dist нет вообще. Устаревание определяем по git, а не по
rem времени файлов: checkout и merge переписывают mtime, и свежий dist выглядел бы
rem «устаревшим», а это на стенде без сети обернулось бы падением на npm ci.
set "NEED_BUILD="
if not exist "web\dist\index.html" set "NEED_BUILD=web\dist отсутствует"
if not defined NEED_BUILD for /f "delims=" %%C in ('git status --porcelain -- web/src 2^>nul') do set "NEED_BUILD=в web\src есть незакоммиченные правки"
if not defined NEED_BUILD (
  set "SRC_COMMIT="
  set "DIST_COMMIT="
  for /f "delims=" %%C in ('git rev-list -1 HEAD -- web/src 2^>nul') do set "SRC_COMMIT=%%C"
  for /f "delims=" %%C in ('git rev-list -1 HEAD -- web/dist 2^>nul') do set "DIST_COMMIT=%%C"
  call :src_vs_dist
)

if defined NEED_BUILD (
  if defined NPM if exist "web\node_modules" (
    echo [run] %NEED_BUILD% — пересобираю web\dist...
    pushd web
    call "%NPM%" run build || (echo [run] ОШИБКА: сборка фронта упала & popd & pause & exit /b 1)
    popd
  ) else (
    echo [run] ВНИМАНИЕ: %NEED_BUILD%, но собрать нечем ^(нет npm или web\node_modules^).
    echo [run] Работаю на том, что лежит в web\dist. Собрать вручную: cd web ^&^& npm ci ^&^& npm run build
  )
)
if not exist "web\dist\index.html" (
  echo [run] ОШИБКА: web\dist\index.html нет, и собрать не удалось.
  echo [run] Установите Node LTS: winget install OpenJS.NodeJS.LTS, затем cd web ^&^& npm ci ^&^& npm run build
  pause & exit /b 1
)

rem ---------- 5. Сервер ----------
if not exist "app\server.py" (
  echo [run] ОШИБКА: нет app\server.py — обновите рабочую копию ^(git pull^).
  pause & exit /b 1
)

echo [run] стартую сервер на %APP_URL%
rem Сервер поднимается в этом же окне и открыть вкладку сам не может: если
rem открыть её сразу, браузер попадёт на ещё не слушающий порт. Поэтому
rem вкладку открывает отдельный процесс через паузу, а мы сразу стартуем.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%APP_URL%'"
call "%UV%" run --frozen --no-sync python -m app.server --port %APP_PORT%
endlocal
exit /b 0

rem ---------- вспомогательное: web\src ушёл вперёд web\dist? ----------
rem Без call «%SRC_COMMIT%» раскрылось бы на этапе разбора блока и осталось пустым.
:src_vs_dist
if not defined SRC_COMMIT exit /b 0
if not defined DIST_COMMIT exit /b 0
if not "%SRC_COMMIT%"=="%DIST_COMMIT%" set "NEED_BUILD=web\src менялся после web\dist"
exit /b 0
