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

rem Сборка нужна, если dist отсутствует или если web\src новее собранной
rem страницы: правки фронта без пересборки уезжают «невидимыми», а демо-стенд
rem собирать не умеет (там нет Node) — поэтому dist лежит в коммите.
set "NEED_BUILD="
if not exist "web\dist\index.html" set "NEED_BUILD=нет сборки"
if not defined NEED_BUILD if exist "web\src" (
  for /f "delims=" %%S in ('powershell -NoProfile -Command "$s=(Get-ChildItem web\src -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime; if ($s -gt (Get-Item web\dist\index.html).LastWriteTime) { 'stale' }"') do set "NEED_BUILD=%%S"
)

if defined NEED_BUILD (
  if defined NPM (
    echo [run] web\dist устарел ^(%NEED_BUILD%^), собираю фронт...
    pushd web
    if not exist "node_modules" (
      call "%NPM%" ci || (echo [run] ОШИБКА: npm ci упал & popd & pause & exit /b 1)
    )
    call "%NPM%" run build || (echo [run] ОШИБКА: сборка фронта упала & popd & pause & exit /b 1)
    popd
  ) else (
    echo [run] ОШИБКА: %NEED_BUILD%, а npm не найден. Установите Node LTS: winget install OpenJS.NodeJS.LTS
    echo [run] Либо соберите вручную: cd web ^&^& ..\tools\node\npm.cmd run build
    pause & exit /b 1
  )
)
if not exist "web\dist\index.html" (
  echo [run] ОШИБКА: web\dist\index.html так и не появился.
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
