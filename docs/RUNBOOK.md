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
| Демо-сервер (`/api/health`, `/api/livez`, `/api/version`, `/metrics` + статика `web/dist`) | `app/server.py`, `app/metrics.py` — только stdlib |
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

Для фронт-команды: если `node`/`npm` не в PATH, а портативная распаковка лежит в
`tools\node`, соберите фронт так (иначе `npm ci` уйдёт в сеть и упрётся в прокси):

```powershell
cd web
$env:PATH = "$PWD\..\tools\node;$env:PATH"   # npm.cmd зовёт `node` по имени — нужен PATH
..\tools\node\npm.cmd ci                     # один раз, подтянуть node_modules по package-lock.json
..\tools\node\npm.cmd run build               # пересобрать dist
```

Без добавления `tools\node` в PATH сборка падает на `tsc --noEmit` с
«`'node' is not recognized`» — это самая частая ошибка при портативном Node.
`run.bat` добавляет каталог в PATH сам.

`run.bat` пересобирает `web/dist` сам, но только когда это безопасно и нужно:
устаревание считается **по git** (есть незакоммиченные правки в `web/src` или
последний коммит по `web/src` новее последнего коммита по `web/dist`), а сборка
запускается только при наличии `web\node_modules`. Иначе — предупреждение и
работа на том, что лежит в `web/dist`. Так сделано из-за двух проверенных
ловушек:

* сравнение по времени файлов врёт после `git checkout`/`merge` (git переписывает
  `mtime`), и свежий `dist` выглядит устаревшим;
* `npm ci` в автоматическом пути — это сеть: на демо-стенде без интернета
  `run.bat` падал бы на ровном месте.

Поэтому `dist` держим в коммите и **в одном коммите с правками `web/src`** —
тогда демо-машине Node не нужен вовсе.

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

Ветку фронта создаём от готового бэкенда, а не от `develop`: фронту нужны
`/api/views` и замороженные метрики, а не «последнее состояние репозитория».

```powershell
git switch -c feature/frontend v0.2.4-backend-views   # бэкенд, на который можно опираться
git push -u origin feature/frontend
```

Промоушен по вехам: merge `--no-ff` в `develop` + аннотированный тег.

| Тег | Когда | Гейт |
|---|---|---|
| `v0.1-m0-db` | БД поднята, приёмки M0 зелёные | 6 проверок из раздела 7 |
| `v0.2-m2-planner` | планировщик пишет контракт | нет ошибок в `v_plan_violations` (warning допустим; замещения отклонены, ADR-010) |
| `v0.2.1-m2-planner` | разбор ревью M2 | те же 0 ошибок + негативный тест срабатывает (раздел 9) |
| `v0.2.2-data-calendar` | точный календарь PI + детерминированный ETL | приёмка M0 (раздел 7) зелёная, два прогона дают побайтово одинаковый `build/seed.sql`, `57 passed` |
| `v0.2.3-backend-metrics` | контракт метрик для devops | `/metrics` отдаёт `pi_planner_db_up 1` и `fund_factor` календаря, `/metrics` закрыт снаружи (Caddy `respond 404`) |
| `v0.2.4-backend-views` | витрины для фронта (`/api/views`, ADR-019) | 25 витрин отвечают на живых данных, инъекция в имя витрины и в `order` не доходит до SQL, `89 passed` |
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

Каталог создайте заранее от имени пользователя деплоя и передайте его uid/gid
контейнеру (на Linux: `BACKUP_UID=$(id -u)`, `BACKUP_GID=$(id -g)`). Значения в
`.env` должны совпадать с владельцем `BACKUP_DIR`; иначе backup завершится с
`Permission denied`, потому что контейнер работает без Linux capabilities.

### 5.2. Подготовка production-хоста

Минимальные требования: Linux x86_64, Docker Engine с Compose v2, DNS-записи
для `CADDY_DOMAIN` и `GRAFANA_DOMAIN`, открытые входящие TCP 80/443 и достаточно
места для Docker volumes и архивов. PostgreSQL, приложение, Prometheus и
Grafana не публикуют host-порты; наружу смотрит только Caddy.

Создайте `.env` из примера и замените как минимум следующие значения:

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 32       # отдельное значение для POSTGRES_PASSWORD
openssl rand -hex 32       # отдельное значение для GRAFANA_ADMIN_PASSWORD
```

В `.env` задайте реальные `CADDY_DOMAIN`, `GRAFANA_DOMAIN`, оба пароля и
абсолютный `BACKUP_DIR` на отдельном диске. Значения `change-me-*`, localhost и
относительный каталог backups являются блокером production-релиза. Сам `.env`
не коммитится и не прикладывается к тикету; в CI его должен создавать secret
manager с правами только у deploy job.

До первого старта проверьте итоговую конфигурацию без вывода секретов в лог:

```bash
docker compose config --quiet
docker compose build app migrate
docker compose pull db backup caddy prometheus grafana
```

Версии Prometheus и Grafana закреплены точно, но `postgres:17-bookworm`,
`python:3.14-slim`, `node:22-bookworm-slim` и `caddy:2-alpine` остаются
плавающими тегами. Для строго воспроизводимого production-релиза registry/CI
должен публиковать собранный `app` по immutable digest, а базовые образы —
фиксироваться digest-политикой платформы.

### 5.3. Релиз без потери данных

Перед каждым релизом запишите текущий git SHA и снимите проверенный backup:

```bash
git rev-parse HEAD
docker compose up -d db backup
docker compose restart backup
docker compose logs --since=10m backup
```

Дождитесь строки `[backup] completed ...`; имя архива сохраните в release
ticket. Затем примените миграции ровно одним job и обновите сервисы:

```bash
docker compose run --rm migrate
docker compose up -d --build app
docker compose up -d caddy prometheus grafana backup
docker compose ps
```

Текущий `docker-compose.yaml` не передаёт `PI_PLANNER_GIT_SHA` внутрь `app`,
поэтому `git_sha` в `/api/version` будет `null`. До добавления этой переменной
в deploy-манифест SHA фиксируется в release ticket вместе с digest образа; это
известное ограничение, а не повод считать локальный SHA внутри контейнера.

Текущий Compose рассчитан на один экземпляр приложения и допускает короткий
перерыв при пересоздании `app`. Настоящий zero-downtime требует оркестратора,
двух реплик, внешней балансировки и readiness-gate; простой `--scale app=2`
здесь не является готовым решением.

### 5.4. Smoke-тест после релиза

Замените домен и запускайте проверки с машины, которая обращается к сервису
тем же путём, что пользователь:

```bash
curl -fsS "https://planner.example.ru/api/livez"
curl -fsS "https://planner.example.ru/api/health"
curl -fsS "https://planner.example.ru/api/version"
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  "https://planner.example.ru/metrics")" = 404
curl -fsS "https://grafana.example.ru/api/health"
docker compose exec -T prometheus promtool check config \
  /etc/prometheus/prometheus.yml
docker compose exec -T prometheus wget -qO- \
  'http://127.0.0.1:9090/api/v1/query?query=up%7Bjob%3D%22pi-planner%22%7D'
```

Дополнительно откройте один разрешённый `/api/views/{view}`, сверьте версию из
`/api/version` с release ticket, а также убедитесь, что запрос Prometheus
возвращает значение 1. Релиз считается принятым только при нуле
`pi_planner_plan_violations{severity="error"}` и отсутствии firing critical
rules.

### 5.5. Откат приложения и миграций

Откат приложения — повторный запуск предыдущего immutable image/commit. Перед
переключением убедитесь, что старая версия совместима с уже применённой схемой.
SQL-миграции намеренно не имеют автоматического down: откатывать схему вслепую
опаснее, чем оставить обратно совместимое расширение.

Если миграция разрушительна или старая версия несовместима со схемой:

1. остановите запись/планировщик и приложение;
2. сохраните аварийный dump текущей базы отдельно;
3. восстановите последний проверенный дорелизный dump по разделу 5.6;
4. запустите предыдущую версию приложения;
5. повторите smoke-тест раздела 5.4.

Любое удаление/переименование колонки выполняется только двухфазно: сначала
релиз, умеющий читать обе схемы, затем миграция и лишь в следующем релизе
удаление старого контракта.

### 5.6. Восстановление PostgreSQL из dump

Восстановление перезаписывает данные, поэтому сначала остановите сервисы,
которые читают или меняют БД, и сохраните аварийную копию. Пример рассчитан на
архив custom format из `BACKUP_DIR`:

Команды ниже предполагают, что `POSTGRES_USER` и `POSTGRES_DB` уже экспортированы
из защищённого окружения и указывают на точные значения:

```bash
docker compose stop app backup
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -Fc --no-owner --no-privileges > pre-restore-emergency.dump
docker compose exec -T db dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
docker compose exec -T db createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
docker compose exec -T db pg_restore -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" --exit-on-error --no-owner --no-privileges \
  < /absolute/path/to/pi_planner-YYYYMMDDTHHMMSSZ.dump
docker compose run --rm migrate
docker compose up -d app backup
```

Не используйте неразрешённые переменные или glob вместо конкретного имени dump.
После восстановления повторите smoke-тест и SQL-приёмку; аварийный dump
удаляйте только после подтверждения владельца данных.

### 5.7. Эксплуатационный чек-лист

| Периодичность | Проверка | Критерий |
|---|---|---|
| каждый релиз | миграции, health, версия, Prometheus rules | все команды завершились с кодом 0 |
| ежедневно | возраст и restore-проверка backup | последний успех моложе 26 часов |
| еженедельно | место на дисках/volumes, firing alerts | запас диска не меньше 30%, critical отсутствуют |
| ежемесячно | ручное восстановление в изолированную БД | схема и ключевые таблицы читаются |
| перед обновлением образов | release notes и backup | есть проверенный dump и план отката |

RPO текущей схемы backup — до 24 часов, RTO заранее не гарантирован и должен
быть измерен учебным восстановлением. Уменьшение `BACKUP_INTERVAL_SECONDS`
снижает RPO, но не заменяет вынос архивов с хоста и контроль свободного места.

## 6. Что легко сломать (проверено на живых данных ДС)

| Симптом | Причина | Что делать |
|---|---|---|
| `column "is_loan" can only be updated to DEFAULT` | `plan_assignments.is_loan` — генерируемая колонка | никогда не включать её в `INSERT`/`UPDATE` |
| `operator does not exist: text = integer` | параметры уходят как `text` | приводить в SQL явно: `WHERE task_id = %s::int` |
| Русский текст превратился в мусор | база создана в WIN1251 | пересоздать с `-E UTF8 --locale=C` (раздел 2.4) |
| `psql` «прошёл», но база пустая | нет `ON_ERROR_STOP=1` | всегда `-v ON_ERROR_STOP=1` |
| История прогонов исчезла | повторный залив `seed.sql` (`TRUNCATE … RESTART IDENTITY`) | не пересевать после первого планирования |
| `v_dq_summary` не 38 находок | залит не тот порядок файлов | перезалить по разделу 3 |
| Фонд часов остался «80 × 6» после смены календаря | залит старый `db/04_views.sql` (без `v_pi_fund_factor`) или `sprints.length_days` нет | перезалить схему и витрины по разделу 3; проверить разделом 7, пункты 0а/0б/7б |
| `planner: календарь PI разъехался` | `pi_periods.sprint_count` не совпадает с числом строк `sprints` | перезалить `build/seed.sql` (он генерит оба) и витрины |
| `etl: календарь PI: спринты покрывают N дней из M` | в `etl/config.py` разошлись `PI_START` / `PI_END` / `SPRINT_COUNT` | править конфиг, а не guard: сетка обязана закрыть квартал ровно (ADR-007) |
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
| 0а | `pi_periods` + `v_pi_fund_factor` | Q3: 01.07–22.09.2026, 84 дня, 6 спринтов, фонд ставки 480 ЧЧ (ADR-025) | ровно эти значения, `fund_factor = 6.5714`, `fund_hh_per_fte = 525.71` | ✅ |
| 0б | `v_sprint_fund_factor` | шесть полных спринтов (×1.0000) | 1..6 по 14 дней / 1.0000 | ✅ |
| 1 | `load_batches.row_counts` | 21 роль, 45 задач, 258 строк сметы, 19 зависимостей, 30 инженеров, 12 снимков истории, 38 находок DQ | ровно эти значения (плюс 6 команд, 117 навыков, 183 связи, 34 орбиты, 15 инициатив, **7 спринтов**, 24 факта) | ✅ |
| 1а | живые `COUNT(*)` по 8 таблицам | совпадают с `row_counts` | 38 / 34 / 30 / 21 / 19 / 258 / 45 / 12 | ✅ |
| 1б | объекты в `public` | 29 таблиц + 17 вьюх | 29 + 17 (две новые — `v_pi_fund_factor`, `v_sprint_fund_factor`) | ✅ |
| 2 | `v_dq_summary` | 38 находок, 0 блокирующих | 38 = 0 error + 35 warning + 3 info | ✅ |
| 3 | `v_role_deficit`, `gap_hh > 0` | ~2371 ЧЧ на 47 связках | 47 связок, 2371.00 ЧЧ | ✅ |
| 3а | природа дефицита | все «роли нет в команде» | 47/47, 2371.00 ЧЧ | ✅ |
| 4 | `v_role_coverage_org`, вердикт «НАЙМ…» | 4 роли 1С, 132 ЧЧ | **6 ролей, 665.00 ЧЧ** (замещения отклонены): РП 449 + Разработчик 1С 107 + Специалист поддержки 84 + Специалист поддержки 1С 12 + Аналитик 1С 10 + Архитектор 1С 3 | ✅ |
| 4а | роли вне штата (`v_bus_factor`, BF = 0) | 6 ролей, спрос 665 ЧЧ | те же 6 ролей и 665.00 ЧЧ: закрывать нечем | ✅ |
| 4б | строгий режим замещений | 0 активных правил, 30 нативных пар | `role_substitutions`: 6 × `rejected`; `v_engineer_role_coverage`: 30 пар, все `is_native` | ✅ |
| 5 | `v_bus_factor`, BF = 1 и спрос > 0 | 8 ролей, 1429 ЧЧ | 8 ролей, 1429.00 ЧЧ | ✅ |
| 5а | кириллица | читаемый русский текст | «НАЙМ: закрыть некем» | ✅ |
| 6 | `load_batches.source_sha256` | sha256 исходного xlsx | `a618cb80…f22e`, ETL **1.1.0**, старт PI **2026-07-01** | ✅ |
| 6а | `v_plan_violations` | 0 ошибок во всех прогонах планировщика | 0 `error`; 12 `warning` — 5 `PLANNED_END_OVERSAIL` в прогоне 1 и 7 в прогоне 2 (раздел 9) | ✅ |
| 7 | `v_team_capacity_sp`, SP/спринт | velocity × 0.8 | Team-K 12.80 … Team-Platform 7.20 | ✅ |
| 7а | `v_team_capacity_sp`, SP за квартал | velocity × 0.8 × 6 | Team-K 84.11 … Team-Platform 47.31 | ✅ |
| 7б | фонд часов по спринтам (`v_satellite_capacity`) | 2400 ЧЧ в каждом спринте | 2400.00 × 6 | ✅ |
| 7в | `plan_assignments` по спринтам | часы только внутри спринтов 1..5 | прогон 1: 258.01 / 254.00 / 10.00; прогон 2: 158.01 / 339.00 / 25.00 | ✅ |

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

### Недетерминизм ETL ДС: найдено и исправлено в ETL 1.1.0

Было (`etl/load.py:468`):

```python
title = max(set(v["titles"]), key=v["titles"].count) if v["titles"] else None
```

На ничьей `max` брал первый элемент **множества**, а порядок обхода `set` зависит от
`PYTHONHASHSEED`: два прогона ETL на одном и том же xlsx давали разные `build/seed.sql` —
расходились 20 строк (заголовки инициатив `PRODF-7121/7122/7125/7129/7131/7133/7134`).
Поля `prodf_id`, `br_id`, `priority_rung` не менялись, на планирование это не влияло, но
закоммиченный сид перестал быть воспроизводимым артефактом: проверка 6 приёмки сверяет
sha256 **исходника**, а не текста сида, и потому этого не видела.

Стало — тай-брейк по отсортированному множеству:

```python
title = max(sorted(set(v["titles"])), key=v["titles"].count) if v["titles"] else None
```

Проверка воспроизводимости (повторять после любой правки ETL):

```powershell
python etl/load.py; Copy-Item build/seed.sql seed_a.sql
python etl/load.py; Copy-Item build/seed.sql seed_b.sql
Compare-Object (Get-Content seed_a.sql) (Get-Content seed_b.sql)   # ожидаем пусто
```

Факт: два прогона подряд дают побайтово одинаковый файл. Сид перегенерирован, так что
расхождение с прежним коммитом осталось только в этих строках плюс календарь.

Решение M0: источник истины — закоммиченный `build/seed.sql`, база залита из него.
**После первого прогона планировщика базу не пересевать** (см. предупреждение в разделе 3).

## 8. Приёмка сервера (стаб к вехе M4)

Сервер — `app/server.py`, только стандартная библиотека (`http.server`,
`ThreadingHTTPServer`); реестр метрик — `app/metrics.py`. Отдельная зависимость не
добавлялась, `uv.lock` не менялся: текстовый формат Prometheus собирается руками.
Маршруты: `GET /api/health`, `GET /api/livez`, `GET /api/version`,
`GET /api/views` + `GET /api/views/{view}` (витрины для фронта, ADR-019),
`GET /metrics` и вся прочая статика из `web/dist` с SPA-fallback на `index.html`.
Записи в БД нет: `app.db.health()`, сбор бизнес-метрик и `app.views.fetch()` идут
в read-only сессии, поэтому демо физически не может испортить данные.

Прогон: `run.bat`, затем в другом окне проверки ниже.

| № | Проверка | Ожидаемо | Факт |
|---|---|---|---|
| 1 | `curl http://127.0.0.1:8000/api/health` | 200, JSON, 5 полей | 200 `application/json; charset=utf-8`, 145 байт |
| 2 | `server_version` / `dbname` | 17.11 / `pi_planner` | 17.11 / `pi_planner` |
| 3 | `tables` / `views` | 29 / 17 (как в приёмке M0, п. 1б) | 29 / 17 |
| 4 | поле `dsn` в ответе | без `password=` | `host=127.0.0.1 port=5432 dbname=pi_planner user=postgres` |
| 5 | `curl http://127.0.0.1:8000/api/nope` | 404 JSON со списком известных эндпоинтов | 404, `known` = `/api/health`, `/api/livez`, `/api/version`, `/api/views`, `/metrics` |
| 6 | `curl http://127.0.0.1:8000/` | 200 `text/html; charset=utf-8` | 200, `index.html`, 463 байта |
| 7 | `curl http://127.0.0.1:8000/plan/3` | SPA-fallback на `index.html` | 200, тот же HTML |
| 8 | `curl http://127.0.0.1:8000/assets/index-*.js` | 200 `text/javascript; charset=utf-8` | 200, 222 189 байт |
| 9 | `curl http://127.0.0.1:8000/assets/nope.js` | 404, а не подмена на `index.html` | 404 JSON |
| 10 | `web/dist` удалён, `GET /` | 503 JSON «frontend_not_built» | 503 (тест) |
| 11 | PostgreSQL остановлен, `GET /api/health` | 503 JSON, сервер не падает | 503 `database_unavailable` (тест) |
| 12 | кириллица в ошибке 404 | читаемая | «нет такого эндпоинта: /api/nope» |
| 13 | `curl http://127.0.0.1:8000/api/livez` | 200 без обращения к базе | 200, 77 байт, `{"status": "alive", ...}` |
| 14 | `curl http://127.0.0.1:8000/api/version` | 200, версии приложения и ETL | 200, 210 байт, `0.4.0` / ETL `1.1.0` / `PI-2026-Q3` |
| 15 | `curl http://127.0.0.1:8000/metrics` | 200 `text/plain; version=0.0.4` | 200, 4270 байт, 139 мс (сбор из базы) |
| 16 | `pi_planner_db_up` в выводе | `1` при живой базе, `0` при мёртвой (и всё равно 200) | `1` |
| 17 | `pi_planner_calendar_info` | `fund_factor="6.0000"`, значение `480` | ровно эти значения (ADR-025) |
| 18 | лог строкой JSON: `--log-format json` | одна строка — один объект | `{"ts": ..., "event": "http_request", "status": 200, ...}` |
| 19 | остановка сигналом (`CTRL_BREAK_EVENT` в тесте — аналог `SIGTERM`) | штатная остановка, код возврата 0 | `server_stopped reason=SIGBREAK`, `uptime_seconds=0.77`, exit `0` |
| 20 | `GET /api/views` | 200, справочник витрин: имена, экран, колонки сортировки | 200, 25 витрин, 10 999 байт, 31 мс |
| 21 | `GET /api/views/v_task_board?limit=1` | 200, строка витрины как есть + конверт (`as_of`, `count`, `columns`) | 200, 1503 байта, `count=45`, `returned=1`, `truncated=true`, `has_more=true` |
| 22 | `GET /api/views/kpi_snapshots`, `/plan_task_schedule`, `/alerts`, `/v_plan_violations`, `/v_orbit_map?order=-orbit_count`, `/v_pi_fund_factor`, `/plan_runs` | 200 у всех, `run_id` подставлен там, где витрина фильтруется прогоном | 200 у всех семи; у первых четырёх `run_id=2`, `run_default=true`, у `v_orbit_map`, `v_pi_fund_factor` и `plan_runs` — `run_id=null` |
| 23 | `GET /api/views/nope` | 404 `not_found` + `known` со всеми именами витрин | 404, 614 байт, `known` = 25 имён, `hint` со ссылкой на `/api/views` |
| 24 | `GET /api/views/v_task_board?order=1;--` | 400 `bad_request`, `param=order`, `known` с колонками витрины, **в SQL ничего не уходит** | 400, 503 байта, 22 колонки в `known` |
| 25 | `GET /api/views/v_task_board?limit=5001` и `?run_id=1` | 400 с объяснением (потолок `limit`; справочная витрина не фильтруется по прогону) | 400, 137 байт, «должен быть в диапазоне 1..5000» |
| 26 | база остановлена, `GET /api/views/v_task_board` | 503 `database_unavailable` с `hint` (как `/api/health`) | 503 (тест) |
| 27 | все витрины в метриках | одна серия `route="/api/views/{view}"`, а не серия на витрину | в живом прогоне: `/api/views` 1 × 200; `/api/views/{view}` 3 × 200, 2 × 400, 1 × 404 |

UI проверяется глазами: после `run.bat` вкладка открывается сама, карточка
«Сервер» должна показать 17.11 / `pi_planner` / 29 / 17 и не показывать блок
«API недоступен».

Тесты: `uv run pytest -q` → `92 passed`. Файл
`tests/test_server.py` поднимает сервер на свободном порту (`--port 0`) в потоке и
дёргает его по HTTP; живая база не нужна — `app.db.health` и сборщик
бизнес-метрик подменяются через
`monkeypatch`. Проверки статики помечены `skip`, если `web/dist` не собран.
`tests/test_views.py` тоже без базы: `tests/conftest.py` подменяет
`app.views.query_dicts` фейком `FakeViewsDB`, который записывает пришедший SQL в
`calls` — так проверяется, что инъекция в имя витрины или в `order` **не доходит
до базы** (`fake_db.calls == []`), а не только что ответ 400.
`tests/test_planner.py` работает с чистой `build_plan()` и базы не касается вовсе.
`tests/test_etl_calendar.py` проверяет сетку спринтов и её guard: квартал закрыт
ровно, последний спринт короткий, а ошибка конфига падает, а не режется молча.
`tests/test_metrics.py` проверяет формат Prometheus, кэш снимка, отказ базы,
замороженный набор лейблов `route` (сверяется с `server.KNOWN_API`) и что все
витрины дают одну серию `/api/views/{view}`.

### Контракт для мониторинга (devops)

Полный актуальный контракт, включая histogram, DB-клиент, витрины, batch и
backup: `docs/OBSERVABILITY.md`. Ниже сохранён краткий контракт исходной версии.

Бэкенд заморожен: ниже — то, чем devops может пользоваться, не заглядывая в код.
Переименование метрики, лейбла или эндпоинта — breaking change и объявляется
отдельно, а не делается «попутным рефакторингом».

**Эндпоинты и их роли**

| Метод и путь | Ответ | Ходит в базу | Для чего |
|---|---|---|---|
| `GET /api/livez` | 200 JSON `{status, version, uptime_seconds, pid}` | нет | liveness-проба: по ней рестарт уместен только если процесс не отвечает |
| `GET /api/health` | 200 JSON / 503 `database_unavailable` | да | readiness-проба: трафик и алерт «база недоступна» |
| `GET /api/version` | 200 JSON | нет | версии приложения, ETL и PI — привязать инцидент к релизу |
| `GET /api/views` | 200 JSON | нет | справочник витрин для фронта (что вообще есть и по каким колонкам сортировать) |
| `GET /api/views/{view}` | 200 JSON / 400 / 404 / 503 | да | витрины для фронта: `?run_id=&limit=&offset=&order=` |
| `GET /metrics` | 200 `text/plain; version=0.0.4` | да, с кэшем | scrape Prometheus |

`/api/views/*` — read-only витрины (ADR-019); 503 при мёртвой базе, 404 на
неизвестное имя витрины с полным списком в теле, 400 на плохой параметр. В
HTTP-метрики не заводят отдельную серию маршрута на витрину: лейбл один —
`route="/api/views/{view}"`. Специализированные `pi_planner_view_*` используют
имя витрины из закрытого whitelist для диагностики медленных запросов.

`/metrics` отвечает 200 и при мёртвой базе: вместо бизнес-серий приходят
`pi_planner_db_up 0` и `pi_planner_db_metrics_error{error_class="..."}`. Если
закрывать эндпоинт при недоступной базе, мониторинг потеряет вместе с метриками
и причину их отсутствия.

**Метрики** (все с префиксом `pi_planner_`, формат собирается самим сервером —
`prometheus_client` в зависимости не тянули)

| Метрика | Тип | Лейблы | Смысл |
|---|---|---|---|
| `pi_planner_up` | gauge | — | 1, пока процесс отвечает |
| `pi_planner_build_info` | gauge | `version`, `etl_version`, `pi_id`, `python` | что именно запущено, всегда 1 |
| `pi_planner_uptime_seconds` | gauge | — | секунды с запуска |
| `pi_planner_http_requests_total` | counter | `method`, `route`, `status` | запросы |
| `pi_planner_http_request_duration_seconds` | histogram | `method`, `route`, `le` | `_bucket`, `_sum`, `_count`; p95/p99 через `histogram_quantile` |
| `pi_planner_http_requests_in_flight` | gauge | — | обработка «прямо сейчас» |
| `pi_planner_db_up` | gauge | — | 1/0 — прошёл ли последний сбор из базы |
| `pi_planner_db_metrics_timestamp_seconds` | gauge | — | когда снят снимок: алерт на устаревание |
| `pi_planner_db_metrics_scrape_seconds` | gauge | — | сколько занял сбор |
| `pi_planner_db_metrics_error` | gauge | `error_class` | 1 при ошибке сбора (имя класса, без текста — кардинальность) |
| `pi_planner_plan_runs_total` | gauge | — | прогонов планировщика в базе |
| `pi_planner_plan_last_run_info` | gauge | `run_id`, `as_of_sprint`, `status` | последний прогон, всегда 1 |
| `pi_planner_plan_last_run_timestamp_seconds` | gauge | `run_id` | когда прогон создан |
| `pi_planner_plan_violations` | gauge | `run_id`, `severity` | нарушения контракта: `error` обязан быть 0 |
| `pi_planner_plan_tasks_in_quarter` | gauge | `run_id` | задач с решением `in_quarter` |
| `pi_planner_plan_assigned_hours` | gauge | `run_id` | часы исполнителей в прогоне |
| `pi_planner_calendar_info` | gauge | `pi_id`, `pi_start`, `pi_end`, `sprint_count`, `fund_factor` | границы PI; значение — фонд ставки за квартал (525.71) |

Лейбл `route` — фиксированный набор (`/api/health`, `/api/livez`, `/api/version`,
`/api/views`, `/api/views/{view}`, `/metrics`, `/api/*`, `/static`), а не URL:
`/assets/index-*.js` не создаёт новую
серию, иначе кардинальность росла бы с каждой сборкой фронта. Код ответа `0` в
`status` означает «ответ не отправлен» — клиент оборвал соединение или хендлер
упал; это не ошибка запроса.

**Алерты, которые имеют смысл** (пороги — предложение, не догма)

| Условие | Что значит |
|---|---|
| `up{job="pi-planner"} == 0` дольше 1 минуты | Prometheus не может опросить процесс — рестарт |
| `pi_planner_db_up == 0` дольше 2 минут | база недоступна: смотреть `/api/health` и `pi_planner_db_metrics_error` |
| `pi_planner_plan_violations{severity="error"} > 0` | контракт плана сломан, приёмка не пройдена |
| `time() - pi_planner_db_metrics_timestamp_seconds > 120` | снимок устарел: сбор падает или залип |
| `rate(pi_planner_http_requests_total{status=~"5.."}[5m]) > 0` | ошибки сервера |

**Scrape**

```yaml
scrape_configs:
  - job_name: pi-planner
    metrics_path: /metrics
    static_configs:
      - targets: ["app:8000"]
```

`/metrics` — внутренний эндпоинт: наружу его закрывает Caddy, чтобы метрики
(а с ними имена вьюх и структура БД) не уехали в публичный интернет.

```caddy
example.com {
    # Публично: UI, health и версия. Метрики — только внутри сети.
    handle /metrics {
        respond 404
    }
    reverse_proxy pi-planner:8000
}
```

**Логи.** По умолчанию — текст для консоли демо (`[server] http_request ...`);
для лог-сборщика включается `--log-format json` или `PI_PLANNER_LOG_FORMAT=json`,
и тогда каждая строка — один объект:

```json
{"ts": "2026-09-21T18:13:10.123+03:00", "level": "info", "event": "http_request",
 "service": "pi-planner", "version": "0.3.0", "method": "GET", "path": "/api/health",
 "route": "/api/health", "status": 200, "duration_ms": 1.27, "bytes": 145,
 "client": "127.0.0.1"}
```

Обязательные поля: `ts` (ISO-8601 с местным смещением), `level`, `event`,
`service`, `version`. События: `server_started`, `server_stopped`,
`http_request`, `http_note` (сообщения самой библиотеки), `frontend_missing`.

**Остановка.** `SIGTERM` / `SIGINT` (на Windows ещё `SIGBREAK`) гасят сервер
штатно: приём новых соединений закрывается, в лог уходит `server_stopped` с
причиной и `uptime_seconds`. Убивать процесс по таймауту не нужно —
`docker stop` проходит за миллисекунды.

**Стоимость scrape.** Бизнес-метрики берутся из базы с кэшем
`PI_PLANNER_METRICS_TTL` (по умолчанию 15 секунд): сбор из базы — около 120 мс,
остальные scrape берут снимок из памяти. Вьюха `v_plan_violations` тяжёлая, и без
кэша каждый scrape считал бы все 29 проверок заново.

### Следующий шаг

M4 (UI) — экраны поверх контракта: гант по `plan_task_schedule`, лента алертов,
KPI-плашки по `target_min` / `target_max`, звёздная карта из `v_orbit_map`.
Точка входа для фронта — `docs/UI_SPEC.md`: что за какой экран отвечает, какие
витрины нужны, типы JSON и чего фронт не считает сам. Витрины уже отдаются
эндпоинтом `/api/views/*` (ADR-019), отдельного «API под экран» не будет без
явного запроса.

## 9. Приёмка M2 (планировщик)

Прогон — `uv run python tools/run_planner.py` (по умолчанию `--as-of-sprint 0`:
базовый план Недели 0, он же фиксирует `plan_baseline`). Флаг `--dry-run`
считает план, но в базу не пишет. Варианты правил (ADR-013):

```bash
uv run python tools/run_planner.py --dry-run --dependency-mode finish_start
uv run python tools/run_planner.py --dry-run --initiative-mode atomic
```

Приёмка — одним запросом. **Критерий: нет строк с `severity = 'error'`**;
строки `severity = 'warning'` план не отменяют, но показываются в UI:

```sql
SELECT * FROM v_plan_violations WHERE run_id = 2;                          -- всё
SELECT * FROM v_plan_violations WHERE run_id = 2 AND severity = 'error';   -- приёмка
```

| № | Проверка | Ожидаемо | Факт (прогоны 1 и 2) |
|---|---|---|---|
| 1 | `v_plan_violations`, `severity='error'` | пусто | **0 строк** в обоих прогонах |
| 2 | `v_plan_violations`, `severity='warning'` | допустимо | `PLANNED_END_OVERSAIL`: **5** строк в прогоне 1, **7** в прогоне 2 — прогноз позже даты исходного плана (ADR-016). После перехода на календарный квартал выросло: 20 задач стартуют 01.06, то есть до PI (ADR-007) |
| 3 | `plan_task_schedule` | строка на каждую живую задачу | 37 = 7 `in_quarter` + 30 `deferred_next_pi` (причина `M2`; `M3` — 0) |
| 4 | `plan_assignments` | часы внутри окон, фондов и бюджетов орбит | 18 строк, 522.01 ЧЧ, из них заём — 6 строк / 187.00 ЧЧ |
| 5 | `plan_baseline` | только при `as_of_sprint = 0` | 37 строк, 7 `committed` — есть только у прогона 1 |
| 6 | `task_state` | слепок всех задач | 45 строк на каждый прогон |
| 7 | `alerts` | red по инициативам, orange по ролям | 14 `red/deadline_miss` + 6 `orange/role_deficit`; в прогоне 2 ещё 3 `yellow/cascade_shift` (в первом сравнивать не с чем) |
| 8 | `kpi_snapshots` | 1 + `sprint_count` + 1 | `pi_predictability` 6.67 (норма 80–100), `bus_factor` 0.00 (норма > 1), `say_do_ratio` 7 строк: 100.00 в прогоне 1, 0.00–100.00 в прогоне 2 |
| 9 | `plan_runs.params` | правила **и календарь** прогона записаны | `estimate_source = matrix_column_sum`, `estimate_validated = true`, `estimate_conflicts = 25`, `substitution_mode = rejected`, `objective`, `dependency_mode = start_start`, `initiative_mode = greedy`, `replan_floor = 1`, `initiatives_planned = 15`, `initiatives_complete = 1`, `initiatives_partial = [5 инициатив]`, `calendar = {2026-07-01..2026-09-30, 92 дня, 7 спринтов, fund_factor 6.5714, fund_hh_per_fte 525.71, short_sprints {"7": "0.5714"}}` |
| 10 | `is_loan` | считает СУБД, не мы | 6 строк с `home_team_id <> serving_team_id`, в `INSERT` колонки нет |
| 11 | Проверок в `db/05_invariants.sql` | 29 (A…AC) | см. таблицу кодов в `docs/PLANNER_SPEC.md`, раздел 7 |


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
`Team-Platform` — структурное горлышко: 47.31 SP за квартал против 45 SP спроса.
Роли есть, а рук на всё не хватает — это честнее и совпадает с выводом M0 про
ресурсы, а не граф.

**Календарь Q3 не изменил состав плана — и это ожидаемо.** Фонд вырос с 480 до
525.71 ЧЧ (+9.5%) и ёмкость `Team-Platform` с 43.2 до 47.31 SP — **этот замер
относится к отменённой сетке на семь спринтов (ADR-025)**, но в квартал
по-прежнему попадают те же 7 задач на те же 522.01 ЧЧ: узкое место — покрытие
ролей (11 задач из 37), а не часы. Замер «до» снят из дампа того же дня, а не
по памяти: `%USERPROFILE%\pg17\backups\pi_planner_before_calendar_1_1_0.dump`.
Разбор — `docs/ANSWERS_ORGANIZERS.md`, «Ответ №5».

**Прогоны 1 и 2.** Прогон 1 — базовый план Недели 0 (`as_of_sprint = 0`): он
фиксирует `plan_baseline`, то есть обещание Недели 0, и на него смотрит
`BASELINE_STARTS_SQL` (`MIN(run_id)`). Прогон 2 — пересчёт на начало 3-го
спринта (`as_of_sprint = 3`, ADR-014): состав квартала тот же (7 задач,
522.01 ЧЧ, те же займы 187.00 ЧЧ), но окна сдвинуты — `replan_floor = 3`,
никто не планируется в закрытые спринты 1–2 (это гарантирует проверка **Y**),
а `SRV-4051` растянулась на 4..5. Добавились 3 `yellow/cascade_shift`:
появилось с чем сравнивать.

Пропуски в нумерации (`run_id` 3) — следствие негативного теста: он берёт
значение последовательности и откатывается. `run_id` не претендует на
непрерывность, а обещание Недели 0 всё равно берётся из первого базового
прогона, а не из «первого по счёту».

Что пересчёт действительно не ломает — видно запросом, а не глазами:

```sql
-- Ни одного назначения в закрытые спринты (проверка Y молчит):
SELECT count(*) FROM plan_assignments a
JOIN plan_runs r ON r.run_id = a.run_id
WHERE r.as_of_sprint > 0 AND a.sprint_no < r.as_of_sprint;   -- 0

-- Обещание Недели 0 одно и то же у базовой линии и у прогона 1:
SELECT count(*) FROM (
  SELECT task_id, planned_sp, committed FROM plan_baseline WHERE run_id = 1
  EXCEPT
  SELECT s.task_id, COALESCE(t.estimation_sp, 0), (s.decision = 'in_quarter')
    FROM plan_task_schedule s JOIN tasks t USING (task_id)
   WHERE s.run_id = 1 AND t.status IN ('ToDo', 'InProgress')
) x;   -- 0
```

**Варианты правил (ADR-013).** Обе новые ветки прогоняются по живому датасету
и меняют результат измеримо (замер 21.09.2026, `--dry-run`, календарь Q3):

| Запуск | Задачи в квартале | Инициатив целиком | Назначений | Займов | `pi_predictability` |
|---|---|---|---|---|---|
| `--dependency-mode start_start` (по умолчанию) | 7 | 1 из 15 | 18 | 6 / 187.00 ЧЧ | 6.67 |
| `--dependency-mode finish_start` | 7 | 1 из 15 | 18 | 6 / 187.00 ЧЧ | 6.67 |
| `--initiative-mode atomic` | 2 | 1 из 15 | 5 | 0 | 6.67 |

`start_start` выбран потому, что ровно эту семантику реализует предпосчитанный
`task_sequence.earliest_start_sprint`: на текущем сиде режимы совпадают, но
правило теперь выбрано явно, а не подразумевается. `atomic` не выбран как
значение по умолчанию: он стоит 5 задач из 7 и не приносит ни одного пункта KPI.

**Негативный тест.** Проверки, которые всегда молчат, ничего не доказывают.
Синтетический «плохой» прогон лежит в `tools/negative_test.sql`: собирается
внутри транзакции и откатывается.

```bash
psql -h 127.0.0.1 -U postgres -d pi_planner -v ON_ERROR_STOP=1 -f tools/negative_test.sql
```

Что ломается намеренно: перенесённая блокирующая при блокируемой «в квартале»
(P), окно 3..9 при 7 спринтах (R), старт раньше графа (Q), перенос без причины
(S), 9999 ЧЧ одному исполнителю (B, X), назначения в закрытые спринты при
`as_of_sprint = 3` (Y), одна строка `task_state` вместо 45 (Z), ни одного
алерта при 30 переносах (AA), ни одной строки KPI (W), а также намеренное
переполнение SP и недогруз («толпа» задач одной команды в 4-м спринте: A, H).

Факт: **56 нарушений, из них 54 `error`, 14 различных кодов** —
`ALERTS_MISSING`, `ASSIGNMENT_IN_CLOSED_SPRINT`, `ASSIGNMENT_OUTSIDE_WINDOW`,
`DEFERRED_WITHOUT_REASON`, `DEPENDENCY_BLOCKER_DEFERRED`, `ENGINEER_OVERLOAD`,
`KPI_INCOMPLETE`, `ORBIT_OVER_BUDGET`, `SP_OVERFLOW`, `START_BEFORE_EARLIEST`,
`STATE_SNAPSHOT_INCOMPLETE`, `UNDER_ALLOCATED`, `WINDOW_OUTSIDE_PI` и warning
`WINDOW_HAS_GAP`. После `ROLLBACK` битого прогона в `plan_runs` не остаётся
(`SELECT count(*) FROM plan_runs WHERE algorithm = 'negative-test'` → 0).
Проверять коды на транзакции приходится именно потому, что живые прогоны дают
0 ошибок: «нулей» для доказательства мало.

**Разбор ревью M2.** `docs/REVIEW_RESPONSE.md`: 10 пунктов ревью, у каждого —
что сделано и какой ADR это закрепил; все 10 закрыты. Там же 5 вопросов, которые
остались к организаторам (семантика зависимостей для длинных задач, ценность
частичной инициативы, даты плана как обязательство, приоритет своей команды над
займом, фактические часы по спринтам) — они требуют ответа заказчика, а не кода.

**Заморозка.** После этих прогонов базу не пересевать: `build/seed.sql` сносит
`plan_runs` вместе со всей историей. Откат — дамп `pg_dump -Fc` в
`%USERPROFILE%\pg17\backups\` (вне репозитория). Дамп снят перед переходом на
календарный квартал: `pi_planner_before_calendar_1_1_0.dump` — из него
восстановлен замер «до» для таблицы в `docs/ANSWERS_ORGANIZERS.md`.

Если пересев всё же понадобился (например, после правки `etl/config.py`),
порядок такой: дамп → залив шести файлов по разделу 3 → приёмка M0 (раздел 7)
→ прогоны заново (`tools/run_planner.py`, затем `--as-of-sprint 3`) → негативный
тест. Обновить ожидаемые числа в разделах 7 и 9 этого файла, если календарь
или фонд поехали.
