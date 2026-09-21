# RUNBOOK — как поднять и проверить сервис

Документ для того, кто поднимает проект «с нуля» на чистой машине Windows,
и для devops, который будет забирать релиз. Всё, что здесь написано, проверено
на живой машине; результаты приёмок — в разделах «Приёмка M0» и «Приёмка сервера».

## 0. Что где лежит

| Что | Где |
|---|---|
| Схема, контракт, вьюхи ДС | `db/01_schema.sql` … `db/05_invariants.sql` |
| Сид (данные) | `build/seed.sql` |
| ETL ДС (пересборка сида) | `etl/load.py`, `etl/config.py` |
| Наш код | `app/*.py`, `tests/*.py` |
| Демо-сервер (`/api/health` + статика `web/dist`) | `app/server.py`, только stdlib |
| Фронт | `web/` (собранный — `web/dist/`, коммитим) |
| Типы фронта из схемы БД | `tools/gen_types.py` → `web/src/types/db.ts` |
| Приёмка БД | `tools/acceptance.sql` |
| Точка входа для демо | `run.bat` |

## 1. Быстрый старт

```bat
run.bat
```

Скрипт делает по порядку: `uv sync --frozen` → проверка PostgreSQL на
`127.0.0.1:5432` (при необходимости поднимает `pg_ctl`) → сборка `web/dist`,
если её нет → старт сервера на `http://127.0.0.1:8000` → открытие браузера.
Повторный запуск безопасен.

Сервер работает в этом же окне и печатает по строке на запрос; остановка —
`Ctrl+C`. Вкладку открывает отдельный фоновый процесс с задержкой в 3 секунды:
`run.bat` не может открыть её сам, порт ещё не слушается. Если браузер всё же
успел открыться раньше сервера и показал «не удаётся подключиться» — обновите
страницу.

## 2. Установка инструментов

### 2.1. uv (обязательно)

```powershell
powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Ставится в `C:\Users\<user>\.local\bin` (проверено: 0.12.17, `uv.exe`, `uvx.exe`, `uvw.exe`).
Альтернатива через пакетный менеджер: `winget install --id astral-sh.uv --exact`.
Если uv не в PATH, `run.bat` сам найдёт его по этому пути.

### 2.2. Python

Отдельно ставить не нужно: `uv python install` подтянет 3.14 по `.python-version`.
Ручной вариант — `uv python install 3.14`.

### 2.3. Node (нужен только для сборки фронта)

```powershell
winget install --id OpenJS.NodeJS.LTS --exact
```

Вариант без прав администратора (использован на рабочей машине): распаковать
`node-v24.19.0-win-x64.zip` в `tools\node\` — каталог в `.gitignore`, `run.bat`
подхватывает `tools\node\npm.cmd` автоматически.
**На демо-машине Node не нужен:** `web/dist` собран и закоммичен.

### 2.4. PostgreSQL 17

Основной путь на Windows без прав администратора — portable-сборка:

```powershell
$root = "$env:USERPROFILE\pg17"
curl.exe -L -o "$env:TEMP\pg.zip" https://get.enterprisedb.com/postgresql/postgresql-17.11-4-windows-x64-binaries.zip
Expand-Archive "$env:TEMP\pg.zip" -DestinationPath $root      # получится $root\pgsql\bin
& "$root\pgsql\bin\initdb.exe" -D "$root\data" -U postgres -A scram-sha-256 `
    -E UTF8 --locale=C --pwfile=<файл с одной строкой: postgres>
& "$root\pgsql\bin\pg_ctl.exe" -D "$root\data" -l "$root\pg.log" start
& "$root\pgsql\bin\createdb.exe" -h 127.0.0.1 -U postgres pi_planner
```

> **Ловушка, из-за которой ломается кириллица.** В сиде есть русский текст
> (`'НАЙМ: закрыть некем'`). На русской Windows `initdb` по умолчанию выбирает
> локаль `Russian_Russia.1251` и кодировку WIN1251 — вставка UTF-8 упадёт или
> запишет мусор. Поэтому `-E UTF8 --locale=C` **обязательны**.
> Проверка: `SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='pi_planner';` → `UTF8`.

Вариант с инсталлятором (нужны права администратора, будет один запрос UAC):

```powershell
.\postgresql-17.11-4-windows-x64.exe --mode unattended --superpassword postgres `
    --serverport 5432 --prefix "C:\pgsql" --enable-components server,commandlinetools
```

Фолбэк, если локальный сервер не поднимается: `docker run -d --name pi-planner-pg
-e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pi_planner -p 5432:5432 postgres:17`.

## 3. Залив SQL — порядок обязателен

```powershell
$psql = "$env:USERPROFILE\pg17\pgsql\bin\psql.exe"
$env:PGPASSWORD = "postgres"
foreach ($f in @("db\01_schema.sql","db\02_contract.sql","build\seed.sql",
                 "db\03_substitutions.sql","db\04_views.sql","db\05_invariants.sql")) {
    & $psql -h 127.0.0.1 -U postgres -d pi_planner -v ON_ERROR_STOP=1 -f $f
}
```

`-v ON_ERROR_STOP=1` не косметика: без него `psql` продолжит работу после ошибки,
и вы получите полупустую базу, не заметив этого. `seed.sql` идёт **после** схемы
и **до** вьюх — вьюхи зависят от заполненных таблиц.

> **Не пересевайте базу после первого планирования.** `build/seed.sql` начинается
> с `TRUNCATE … RESTART IDENTITY`, то есть стирает историю прогонов планировщика
> (`plan_runs` и всё, что на неё ссылается). На демо-машине пересев = потеря
> результатов, которые показываются на защите.

## 4. Ветки и релизы

Модель — git-flow-lite. `main` не трогаем: туда мержит devops.

| Ветка | Роль |
|---|---|
| `main` | приёмка/сдача, обновляется только devops'ом слиянием `develop → main` |
| `develop` | всегда зелёная и запускаемая одной командой; сюда только `--no-ff` слияния |
| `feature/backend` | наш бэкенд (планировщик, репланировщик, метрики, API) |
| `feature/frontend` | наши экраны (ветвится от `feature/backend`) |
| `feature/data` | **заморожена** — слой ДС, мы в неё не коммитим |

```powershell
# создать ветки (одноразово, уже сделано)
git switch -c develop feature/data;      git push -u origin develop
git switch -c feature/backend develop;   git push -u origin feature/backend
```

Промоушен по вехам: merge `--no-ff` в `develop` + аннотированный тег.

| Тег | Когда | Гейт |
|---|---|---|
| `v0.1-m0-db` | БД поднята, приёмки M0 зелёные | 6 проверок из раздела 7 |
| `v0.2-m2-planner` | планировщик пишет контракт | `v_plan_violations` пуст полностью (замещения отклонены, ADR-010) |
| `v0.3-m4-ui` | экраны работают | `run.bat` открывает UI и отдаёт данные |
| `v1.0-demo` | сдача | прогон демо-сценария без правок «на ходу» |

```powershell
git switch develop
git merge --no-ff feature/backend -m "merge(backend): M0 into develop"
git tag -a v0.1-m0-db -m "M0: PostgreSQL 17 + сид ДС, шесть приёмок, run.bat, скелет Vite"
git push origin develop --follow-tags
```

## 5. Передача релиза devops

1. Убедиться, что `develop` зелёный: `run.bat` поднимается с нуля на чистой машине.
2. Отправить merge request `develop → main` (GitLab-ссылка печатается самим git
   при пуше новой ветки).
3. В описании MR — таблица приёмок и команда запуска.
4. **Риск:** если слияние в `main` не сделать, `main` останется пустой заглушкой
   («Initial commit»), и проверяющая сторона не увидит решения. Отправляем MR
   сразу после `v0.1-m0-db` и напоминаем за сутки до дедлайна.
5. Попросить включить защиту `main`/`develop` (merge request + запрет force-push),
   если платформа это позволяет.

### 5.1. Деплой, миграции и резервные копии Docker-версии

Первичная инициализация PostgreSQL выполняет SQL из
`docker-entrypoint-initdb.d` только на пустом `postgres_data`. После первого
старта **нельзя** обновлять схему повторным запуском `seed.sql`: он удаляет
историю прогонов. Каждое изменение схемы оформляйте новым файлом
`db/migrations/NNNN_описание.sql`; применённые файлы не редактируются —
`tools/migrate.py` сверяет их SHA-256.

Перед запуском новой версии приложения выполните миграции и только затем
пересоздавайте сервисы:

```bash
docker compose up -d db
docker compose run --rm migrate
docker compose up -d --build app caddy backup
```

`backup` снимает PostgreSQL custom-format dump сразу после старта и далее раз
в сутки. После каждого dump он восстанавливает архив во временную базу и
проверяет наличие ключевых объектов; сбой завершает контейнер, после чего
Compose его перезапускает. Каталог, срок хранения и интервал задаются в `.env`:
`BACKUP_DIR`, `BACKUP_RETENTION_DAYS`, `BACKUP_INTERVAL_SECONDS`.
Для production `BACKUP_DIR` должен указывать на подключённое или удалённо
реплицируемое хранилище, а восстановление из последнего архива следует
проверять отдельно перед релизом.

## 6. Что легко сломать (проверено на живых данных ДС)

| Симптом | Причина | Что делать |
|---|---|---|
| `column "is_loan" can only be updated to DEFAULT` | `plan_assignments.is_loan` — генерируемая колонка | никогда не включать её в `INSERT`/`UPDATE` |
| `operator does not exist: text = integer` | параметры уходят как `text` | приводить в SQL явно: `WHERE task_id = %s::int` |
| Русский текст превратился в мусор | база создана в WIN1251 | пересоздать с `-E UTF8 --locale=C` (раздел 2.4) |
| `psql` «прошёл», но база пустая | нет `ON_ERROR_STOP=1` | всегда `-v ON_ERROR_STOP=1` |
| История прогонов исчезла | повторный залив `seed.sql` (`TRUNCATE … RESTART IDENTITY`) | не пересевать после первого планирования |
| `v_dq_summary` не 38 находок | залит не тот порядок файлов | перезалить по разделу 3 |
| `v_role_coverage_org` снова показывает 132 ЧЧ на 4 ролях | залит старый `db/03_substitutions.sql` со статусом `proposed` | перезалить файл: в нём все 6 строк `rejected` (ответ №2, ADR-010) |
| UI показывает «API недоступен: 503: …» | PostgreSQL не отвечает или `dsn.json` смотрит в другую базу | `curl http://127.0.0.1:8000/api/health` — в теле причина, DSN без пароля и подсказка с **реальным** адресом из `dsn.json`/`PI_PLANNER_DSN`; поднять Postgres (раздел 2.4) |
| `OSError: [WinError 10048]` при старте | порт 8000 занят прошлым запуском | закрыть прежнее окно `run.bat` (сервер останавливается по `Ctrl+C`) |

## 7. Приёмка M0

Прогон `tools/acceptance.sql` (`psql -v ON_ERROR_STOP=1`, `exit=0`) на базе
`pi_planner` после заливки шести SQL-файлов в порядке из раздела 3.
Эталон — цифры из выданного датасета; «факт» — то, что вернула живая база.

Прогон 21.09.2026 идёт **в строгом режиме**: `db/03_substitutions.sql` залит со
статусом `rejected` (ответ организаторов №2, ADR-010), поэтому пункты 4/4а
показывают наём без права замещения, а 4б — саму строгость режима.

| № | Проверка | Эталон ДС | Факт | Итог |
|---|---|---|---|---|
| 0 | кодировка базы | UTF8 | UTF8 | ✅ |
| 1 | `load_batches.row_counts` | 21 роль, 45 задач, 258 строк сметы, 19 зависимостей, 30 инженеров, 12 снимков истории, 38 находок DQ | ровно эти значения (плюс 6 команд, 117 навыков, 183 связи, 34 орбиты, 15 инициатив, 6 спринтов, 24 факта) | ✅ |
| 1а | живые `COUNT(*)` по 8 таблицам | совпадают с `row_counts` | 38 / 34 / 30 / 21 / 19 / 258 / 45 / 12 | ✅ |
| 1б | объекты в `public` | 29 таблиц + 15 вьюх | 29 + 15 | ✅ |
| 2 | `v_dq_summary` | 38 находок, 0 блокирующих | 38 = 0 error + 35 warning + 3 info | ✅ |
| 3 | `v_role_deficit`, `gap_hh > 0` | ~2371 ЧЧ на 47 связках | 47 связок, 2371.00 ЧЧ | ✅ |
| 3а | природа дефицита | все «роли нет в команде» | 47/47, 2371.00 ЧЧ | ✅ |
| 4 | `v_role_coverage_org`, вердикт «НАЙМ…» | 4 роли 1С, 132 ЧЧ | **6 ролей, 665.00 ЧЧ** (замещения отклонены): РП 449 + Разработчик 1С 107 + Специалист поддержки 84 + Специалист поддержки 1С 12 + Аналитик 1С 10 + Архитектор 1С 3 | ✅ |
| 4а | роли вне штата (`v_bus_factor`, BF = 0) | 6 ролей, спрос 665 ЧЧ | те же 6 ролей и 665.00 ЧЧ: закрывать нечем | ✅ |
| 4б | строгий режим замещений | 0 активных правил, 30 нативных пар | `role_substitutions`: 6 × `rejected`; `v_engineer_role_coverage`: 30 пар, все `is_native` | ✅ |
| 5 | `v_bus_factor`, BF = 1 и спрос > 0 | 8 ролей, 1429 ЧЧ | 8 ролей, 1429.00 ЧЧ | ✅ |
| 5а | кириллица | читаемый русский текст | «НАЙМ: закрыть некем» | ✅ |
| 6 | `load_batches.source_sha256` | sha256 исходного xlsx | `a618cb80…f22e`, ETL 1.0.0, старт PI 2026-06-01 | ✅ |
| 6а | `v_plan_violations` | 0 во всех прогонах планировщика | 0 нарушений при 2 прогонах (приёмка M2, раздел 9) | ✅ |
| 7 | `v_team_capacity_sp`, SP/спринт | velocity × 0.8 | Team-K 12.80 … Team-Platform 7.20 | ✅ |

### Версии, на которых получен результат

| Компонент | Версия |
|---|---|
| PostgreSQL | 17.11 (portable, `initdb -E UTF8 --locale=C`) |
| uv | 0.12.17 |
| Python | 3.14.2 |
| psycopg | 3.3.6 |
| openpyxl (только ETL ДС) | 3.1.5 |
| Node / npm | 24.19.0 / 11.17.0 (нужны только для сборки `web/dist`) |
| Vite / React / TypeScript | 8.3.0 / 19.3.0 / 7.0.2 |

### Находка: ETL ДС недетерминирован (M0 не блокирует)

`etl/load.py:468`:

```python
title = max(set(v["titles"]), key=v["titles"].count) if v["titles"] else None
```

На ничьей `max` берёт первый элемент **множества**, а порядок обхода `set` зависит от
`PYTHONHASHSEED`. С `PYTHONHASHSEED=0` ETL воспроизводим байт-в-байт (два прогона дают
один sha256), но от закоммиченного `build/seed.sql` он отличается ровно семью названиями
инициатив: `PRODF-7121/7122/7125/7129/7131/7133/7134`. Поля `prodf_id`, `br_id`,
`priority_rung` не меняются — на планирование это не влияет.

Решение M0: источник истины — закоммиченный `build/seed.sql`, база залита из него.
**После первого прогона планировщика базу не пересевать** (см. предупреждение в разделе 3).

## 8. Приёмка сервера (стаб к вехе M4)

Сервер — `app/server.py`, только стандартная библиотека (`http.server`,
`ThreadingHTTPServer`). Отдельная зависимость не добавлялась, `uv.lock` не менялся.
Маршруты: `GET /api/health` (JSON) и вся прочая статика из `web/dist` с SPA-fallback
на `index.html`. Записи в БД нет: единственный запрос — `app.db.health()`, и он идёт
в read-only сессии, поэтому демо физически не может испортить данные.

Прогон: `run.bat`, затем в другом окне проверки ниже.

| № | Проверка | Ожидаемо | Факт |
|---|---|---|---|
| 1 | `curl http://127.0.0.1:8000/api/health` | 200, JSON, 5 полей | 200 `application/json; charset=utf-8`, 145 байт |
| 2 | `server_version` / `dbname` | 17.11 / `pi_planner` | 17.11 / `pi_planner` |
| 3 | `tables` / `views` | 29 / 15 (как в приёмке M0, п. 1б) | 29 / 15 |
| 4 | поле `dsn` в ответе | без `password=` | `host=127.0.0.1 port=5432 dbname=pi_planner user=postgres` |
| 5 | `curl http://127.0.0.1:8000/api/nope` | 404 JSON | 404, `{"error": "not_found", "known": ["/api/health"]}` |
| 6 | `curl http://127.0.0.1:8000/` | 200 `text/html; charset=utf-8` | 200, `index.html`, 463 байта |
| 7 | `curl http://127.0.0.1:8000/plan/3` | SPA-fallback на `index.html` | 200, тот же HTML |
| 8 | `curl http://127.0.0.1:8000/assets/index-*.js` | 200 `text/javascript; charset=utf-8` | 200, 222 189 байт |
| 9 | `curl http://127.0.0.1:8000/assets/nope.js` | 404, а не подмена на `index.html` | 404 JSON |
| 10 | `web/dist` удалён, `GET /` | 503 JSON «frontend_not_built» | 503 (тест) |
| 11 | PostgreSQL остановлен, `GET /api/health` | 503 JSON, сервер не падает | 503 `database_unavailable` (тест) |
| 12 | кириллица в ошибке 404 | читаемая | «нет такого эндпоинта: /api/nope» |

UI проверяется глазами: после `run.bat` вкладка открывается сама, карточка
«Сервер» должна показать 17.11 / `pi_planner` / 29 / 15 и не показывать блок
«API недоступен».

Тесты: `uv run pytest -q` → `24 passed` (8 сервер + 16 планировщик). Файл
`tests/test_server.py` поднимает сервер на свободном порту (`--port 0`) в потоке и
дёргает его по HTTP; живая база не нужна — `app.db.health` подменяется через
`monkeypatch`. Проверки статики помечены `skip`, если `web/dist` не собран.
`tests/test_planner.py` работает с чистой `build_plan()` и базы не касается вовсе.

### Следующий шаг

M4 (UI) — экраны поверх контракта: гант по `plan_task_schedule`, лента алертов,
KPI-плашки по `target_min` / `target_max`, звёздная карта из `v_orbit_map`.

## 9. Приёмка M2 (планировщик)

Прогон — `uv run python tools/run_planner.py` (по умолчанию `--as-of-sprint 0`:
базовый план Недели 0, он же фиксирует `plan_baseline`). Флаг `--dry-run`
считает план, но в базу не пишет. Приёмка — одним запросом, пустой ответ
означает корректный план:

```sql
SELECT * FROM v_plan_violations WHERE run_id = 2;   -- 14 проверок из db/05_invariants.sql
```

| № | Проверка | Ожидаемо | Факт |
|---|---|---|---|
| 1 | `v_plan_violations` | пусто | **0 строк** — и по `run_id = 2`, и по всем прогонам сразу |
| 2 | `plan_task_schedule` | строка на каждую живую задачу | 37 (7 `in_quarter`, 30 `deferred_next_pi` / `M2`) |
| 3 | `plan_assignments` | часы внутри окон и фондов | 18 строк, 522.01 ЧЧ, из них заём — 6 строк / 187.00 ЧЧ |
| 4 | `plan_baseline` | только при `as_of_sprint = 0` | 37 строк, из них 7 `committed` |
| 5 | `task_state` | слепок всех задач | 45 строк на `as_of_sprint = 0` |
| 6 | `alerts` | red по инициативам, orange по ролям | 14 `red/deadline_miss` + 6 `orange/role_deficit`, жёлтых нет (первый прогон — сравнивать не с чем) |
| 7 | `kpi_snapshots` | 3 KPI с нормами | `pi_predictability` 6.67 (норма 80–100), `say_do_ratio` 100.00 в каждом спринте, `bus_factor` 0.00 (норма > 1) |
| 8 | `plan_runs.params` | источник часов проверен | `estimate_source = matrix_column_sum`, `estimate_validated = true`, `estimate_conflicts = 25`, `substitution_mode = rejected` |
| 9 | `is_loan` | считает СУБД, не мы | 6 строк с `home_team_id <> serving_team_id`, в `INSERT` колонки нет |

**Шесть оранжевых алертов — ровно те роли, что в приёмке M0 (п. 4):**
`Руководитель проекта` 449 ЧЧ, `Разработчик 1С` 107, `Специалист поддержки` 84,
`Специалист поддержки 1С` 12, `Аналитик 1С` 10, `Архитектор 1С` 3 — сумма 665 ЧЧ
совпадает с ответом организаторов №2. В `payload` каждого алерта едут роль, часы
и список задач: это и есть требование «показать риск и перенести» (ответ №3),
поэтому 1С-задачи уезжают в следующий PI с `decision_reason = 'M2'`, а не
подменяются .NET-сеньором.

**Почему 7 задач, а не 11.** «11 из 37» — это задачи, у которых все требуемые
роли есть в штате (замер в `docs/ANSWERS_ORGANIZERS.md`). Планировщик проверяет
ещё часы и SP-ёмкость, и четыре задачи упираются именно в них: `AI-302`
(216 ЧЧ), `DB-202` (247 ЧЧ), `MOB-7012` (210 ЧЧ), `SRV-4042` (120 ЧЧ). Плюс
`Team-Platform` — структурное горлышко: 43.2 SP за квартал против 45 SP спроса.
Роли есть, а рук на всё не хватает — это честнее и совпадает с выводом M0 про
ресурсы, а не граф.

**Прогоны 1 и 2.** Прогон 1 — первый: на нём нашлась неточность формулы
`say_do_ratio` (считался кумулятивно, а спека требует «в спринте / на спринт»).
Прогон 2 — приёмочный: тот же алгоритм и то же расписание, отличается только KPI.
Обе строки остались в `plan_runs` — история пересчётов не перезаписывается, а
обещание Недели 0 берётся из ПЕРВОГО базового прогона (`MIN(run_id)`).

**Заморозка.** После этих прогонов базу не пересевать: `build/seed.sql` сносит
`plan_runs` вместе со всей историей. Откат — дамп `pg_dump -Fc` в
`%USERPROFILE%\pg17\backups\` (вне репозитория), снят перед первым прогоном M2.
