# Observability PI-Planner

Этот документ — единый контракт метрик, логов, проб и подключения
Prometheus/Grafana. Имена метрик и labels считаются публичным контрактом для
devops: изменение или удаление существующего имени требует переходного релиза.

## 1. Архитектура

```text
браузер → Caddy → app:8000 → PostgreSQL
                     ↑
Grafana → Prometheus ┘ /metrics (Docker-сеть monitoring)

backup → /backups/backup.prom ← node-exporter textfile collector (следующий этап)
PostgreSQL ← postgres_exporter                         (следующий этап)
контейнеры ← cAdvisor                                  (следующий этап)
HTTPS ← blackbox-exporter                              (следующий этап)
```

`Caddyfile` возвращает 404 на публичный `/metrics`. Prometheus должен обращаться
к `app:8000/metrics` напрямую по Docker-сети `monitoring`. Эндпоинт всегда отвечает
200, даже если PostgreSQL недоступен: в этом случае остаются process/HTTP-метрики,
а `pi_planner_db_up` становится равной 0.

Бизнес-метрики читаются из БД не чаще `PI_PLANNER_METRICS_TTL` (по умолчанию
15 секунд). Это защищает тяжёлую `v_plan_violations` от запроса на каждый scrape.

## 2. Пробы

| Путь | БД | Назначение |
|---|---:|---|
| `/api/livez` | нет | процесс жив; использовать как liveness |
| `/api/health` | да | приложение готово обслуживать данные; readiness |
| `/api/version` | нет | версия приложения, ETL, PI, git SHA и uptime |
| `/metrics` | с кэшем | внутренний Prometheus scrape |

Падение PostgreSQL не должно вызывать бесконечные рестарты приложения. Поэтому
liveness не ходит в БД, а readiness ходит.

## 3. Метрики приложения

### 3.1. Процесс

| Метрика | Тип | Labels | Смысл |
|---|---|---|---|
| `pi_planner_up` | gauge | — | процесс сформировал `/metrics` |
| `pi_planner_build_info` | gauge | `version`, `etl_version`, `pi_id`, `python` | состав релиза |
| `pi_planner_uptime_seconds` | gauge | — | время с запуска |
| `pi_planner_process_cpu_seconds_total` | counter | — | CPU процесса |
| `pi_planner_process_resident_memory_bytes` | gauge | — | RSS процесса |
| `pi_planner_process_threads` | gauge | — | активные Python-потоки |

Для алерта на недоступность использовать стандартную метрику Prometheus
`up{job="pi-planner"}`, а не `pi_planner_up`: недоступный процесс не может
отдать собственную метрику.

### 3.2. HTTP

| Метрика | Тип | Labels |
|---|---|---|
| `pi_planner_http_requests_total` | counter | `method`, `route`, `status` |
| `pi_planner_http_request_duration_seconds` | histogram | `method`, `route`, `le` |
| `pi_planner_http_requests_in_flight` | gauge | — |
| `pi_planner_http_response_bytes_total` | counter | `method`, `route` |
| `pi_planner_http_exceptions_total` | counter | `route`, `error_class` |

`route` принимает только `/api/health`, `/api/livez`, `/api/version`,
`/api/views`, `/api/views/{view}`, `/metrics`, `/api/*`, `/static`. Полный URL,
query string и адрес клиента labels не являются.

Пример p95 за 5 минут:

```promql
histogram_quantile(
  0.95,
  sum by (le, route) (
    rate(pi_planner_http_request_duration_seconds_bucket[5m])
  )
)
```

### 3.3. DB-клиент приложения

Эти метрики описывают соединения и запросы именно Python-приложения. Состояние
самого PostgreSQL позднее будет снимать `postgres_exporter`.

| Метрика | Тип | Labels |
|---|---|---|
| `pi_planner_db_connections_open` | gauge | — |
| `pi_planner_db_connections_total` | counter | `outcome` |
| `pi_planner_db_connection_duration_seconds` | histogram | `le` |
| `pi_planner_db_operations_in_flight` | gauge | — |
| `pi_planner_db_operations_total` | counter | `operation`, `outcome` |
| `pi_planner_db_operation_duration_seconds` | histogram | `operation`, `le` |
| `pi_planner_db_rows_total` | counter | `operation` |
| `pi_planner_db_up` | gauge | — |
| `pi_planner_db_metrics_timestamp_seconds` | gauge | — |
| `pi_planner_db_metrics_scrape_seconds` | gauge | — |
| `pi_planner_db_metrics_error` | gauge | `error_class` |

`operation` — короткое имя из кода (`health`, `metrics_plan`,
`metrics_calendar`, `metrics_decisions`, `metrics_alerts`, `metrics_kpis`,
`metrics_dq`, `metrics_etl`, `planner_write` либо имя общей обёртки). SQL,
параметры и текст исключения в labels не попадают.

### 3.4. Витрины

| Метрика | Тип | Labels |
|---|---|---|
| `pi_planner_view_requests_total` | counter | `view`, `outcome` |
| `pi_planner_view_query_duration_seconds` | histogram | `view`, `le` |
| `pi_planner_view_rows_returned_total` | counter | `view` |
| `pi_planner_view_truncated_total` | counter | `view` |

`view` берётся только из whitelist `app.views.SOURCES`; неизвестное имя
агрегируется как `_unknown`. Поэтому пользователь не может создать произвольную
Prometheus series через URL.

### 3.5. Планирование и качество данных

| Метрика | Тип | Labels |
|---|---|---|
| `pi_planner_plan_runs_total` | gauge | — |
| `pi_planner_plan_last_run_id` | gauge | — |
| `pi_planner_plan_last_run_info` | gauge | `run_id`, `as_of_sprint`, `status` |
| `pi_planner_plan_last_run_timestamp_seconds` | gauge | `run_id` |
| `pi_planner_plan_violations` | gauge | `run_id`, `severity` |
| `pi_planner_plan_tasks` | gauge | `decision`, `reason` |
| `pi_planner_plan_task_hours` | gauge | `decision`, `reason` |
| `pi_planner_plan_tasks_in_quarter` | gauge | `run_id` |
| `pi_planner_plan_assigned_hours` | gauge | `run_id` |
| `pi_planner_plan_loan_hours` | gauge | — |
| `pi_planner_plan_alerts` | gauge | `level`, `type` |
| `pi_planner_plan_kpi_value` | gauge | `kpi`, `sprint` |
| `pi_planner_plan_kpi_target_min` | gauge | `kpi`, `sprint` |
| `pi_planner_plan_kpi_target_max` | gauge | `kpi`, `sprint` |
| `pi_planner_job_last_duration_seconds` | gauge | `phase` |
| `pi_planner_data_quality_issues` | gauge | `severity` |
| `pi_planner_etl_last_success_timestamp_seconds` | gauge | — |
| `pi_planner_etl_info` | gauge | `version`, `pi_start` |
| `pi_planner_calendar_info` | gauge | `pi_id`, `pi_start`, `pi_end`, `sprint_count`, `fund_factor` |

`run_id` в старых метриках сохранён для совместимости, но создаёт новую series
на каждый прогон. Новые dashboard следует строить на `pi_planner_plan_last_run_id`
и агрегатах без `run_id`. Старые series можно удалить после переходного релиза.

Длительности `load_inputs`, `load_baseline`, `build_plan` и подготовки к записи
планировщик сохраняет в `plan_runs.params.observability`. `write_plan_seconds`
добавляется той же транзакцией, которая пишет контракт. Поэтому значения
сохраняются после завершения batch-процесса.

Прогон, упавший до записи `plan_runs`, виден по exit code и JSON-логу, но пока
не создаёт Prometheus series: короткоживущий процесс некому scrape-ить. При
добавлении scheduler это событие следует снимать его метрикой job outcome либо
через Pushgateway. Самостоятельно запускать Pushgateway только ради одного job
на текущем стенде не требуется.

### 3.6. Backup

`ops/backup.sh` атомарно обновляет `/backups/backup.prom`:

| Метрика | Тип |
|---|---|
| `pi_planner_backup_last_attempt_timestamp_seconds` | gauge |
| `pi_planner_backup_last_success_timestamp_seconds` | gauge |
| `pi_planner_backup_duration_seconds` | gauge |
| `pi_planner_backup_size_bytes` | gauge |
| `pi_planner_backup_restore_verification_enabled` | gauge |
| `pi_planner_backup_restore_verified` | gauge |
| `pi_planner_backup_failures_total` | counter |

На этапе установки Prometheus каталог `/backups` следует подключить read-only к
`node-exporter` и указать `--collector.textfile.directory`.

Состояние схемы отдаёт само приложение:

| Метрика | Тип |
|---|---|
| `pi_planner_migrations_applied` | gauge |
| `pi_planner_migrations_pending` | gauge |
| `pi_planner_migrations_last_applied_timestamp_seconds` | gauge |

## 4. Labels и кардинальность

Разрешены только labels из закрытого множества: route, status, outcome,
error_class, operation, view whitelist, decision, reason, level, alert type,
KPI и sprint. Запрещены:

- `task_id`, `engineer_id`, `entity_id`, `source_sha256`;
- полный URL и query string;
- SQL и параметры;
- exception message;
- текст алерта или DQ-находки;
- client IP.

Такие данные остаются в структурированных логах и PostgreSQL.

## 5. Логи

Compose задаёт `PI_PLANNER_LOG_FORMAT=json`. Каждая строка содержит `ts`,
`level`, `event`, `service`, `version`. HTTP-событие дополнительно содержит
`method`, `path`, нормализованный `route`, `status`, `duration_ms`, `bytes` и
`client`.

На следующем инфраструктурном этапе stdout контейнеров можно отправлять через
Vector/Promtail в Loki. Поле `path` и `client` допустимы в логах, но не labels
Loki без дополнительной нормализации.

## 6. Начальные алерты

Пороги требуют калибровки по реальной нагрузке.

```promql
# процесс не scrape-ится 1 минуту
up{job="pi-planner"} == 0

# БД недоступна 2 минуты
pi_planner_db_up == 0

# снимок бизнес-метрик старше 2 минут
time() - pi_planner_db_metrics_timestamp_seconds > 120

# ошибки контракта плана
pi_planner_plan_violations{severity="error"} > 0

# доля 5xx больше 5% при наличии трафика
sum(rate(pi_planner_http_requests_total{status=~"5.."}[5m]))
/
clamp_min(sum(rate(pi_planner_http_requests_total[5m])), 0.001) > 0.05

# p95 API больше секунды
histogram_quantile(
  0.95,
  sum by (le) (
    rate(pi_planner_http_request_duration_seconds_bucket{route=~"/api/.*"}[5m])
  )
) > 1

# backup неуспешен или старше 26 часов
pi_planner_backup_restore_verification_enabled == 1
and pi_planner_backup_restore_verified != 1

time() - pi_planner_backup_last_success_timestamp_seconds > 93600
```

## 7. План dashboard Grafana

1. **Service overview:** `up`, RPS, 4xx/5xx, p50/p95/p99, in-flight, CPU, RSS,
   threads и версия релиза.
2. **Database:** client latency/errors, connections; после установки
   postgres_exporter — locks, deadlocks, cache hit, transactions и размер БД.
3. **Planning quality:** статус и возраст плана, decisions/reasons, violations,
   alerts, KPI, loan hours, DQ и ETL freshness.
4. **Operations:** backup age/size/duration/restore, миграции, container restart,
   CPU/RAM/disk и срок действия TLS.

## 8. Проверка контракта

Перед релизом:

```bash
docker compose exec -T prometheus sh -c \
  'wget -qO- http://app:8000/metrics | promtool check metrics'
docker compose exec -T prometheus promtool check config \
  /etc/prometheus/prometheus.yml
uv run pytest -q
```

Тесты проверяют histogram buckets, bounded route/view labels, отказ БД,
экранирование Prometheus labels и DB-телеметрию без SQL в series.

`promtool` может вывести lint-предупреждения для сохранённых ради совместимости
`pi_planner_plan_runs_total` (исторически gauge) и бизнес-единицы `_hours`.
Формат при этом валиден. Их переименование выполняется только переходным релизом.

## 9. Prometheus и Grafana в Docker Compose

В Compose закреплены стабильные версии образов:

- `prom/prometheus:v3.14.0`;
- `grafana/grafana:13.2.2`.

Точные теги делают обновление воспроизводимым. Перед обновлением следует поменять
тег вручную, прочитать release notes, выполнить `docker compose pull` и повторить
проверки из раздела 8. Используется стандартный Alpine-вариант Grafana со
встроенными plugins; `-slim` потребовал бы загрузки Prometheus plugin при первом
старте.

Первый локальный запуск:

```bash
cp .env.example .env
# Задать POSTGRES_PASSWORD и GRAFANA_ADMIN_PASSWORD в .env.
docker compose pull prometheus grafana
docker compose up -d --build prometheus grafana
docker compose ps
```

Compose автоматически поднимет зависимости `db` и `app`. Пользовательский
доступ идёт через Caddy:

| Сервис | URL | Назначение |
|---|---|---|
| Prometheus | host-порт отсутствует | внутренний сбор метрик, rules и alerts |
| Grafana | `https://grafana.localhost` | HTTPS через Caddy; логин и пароль берутся из `.env` |

Проверка после запуска:

```bash
curl -kfsS https://grafana.localhost/api/health
docker compose exec -T prometheus wget -qO- http://127.0.0.1:9090/-/ready
docker compose exec -T prometheus wget -qO- \
  'http://127.0.0.1:9090/api/v1/query?query=up%7Bjob%3D%22pi-planner%22%7D'
```

Prometheus опрашивает `app:8000/metrics` каждые 15 секунд. Правила из
`ops/prometheus/rules/pi-planner.yml` загружаются автоматически. В Grafana
источник данных `Prometheus` создаётся автоматически с UID `prometheus` и URL
`http://prometheus:9090`; ручная настройка datasource не нужна.

Данные сохраняются в named volumes `prometheus_data` и `grafana_data`.
Prometheus хранит не более 15 дней и 2 GB по умолчанию; лимиты меняются через
`PROMETHEUS_RETENTION_TIME` и `PROMETHEUS_RETENTION_SIZE`. Его порт доступен
только контейнерам сети `monitoring`. Grafana доступна только Caddy через сеть
`observability_proxy`; TLS завершается на Caddy. Для production нужно задать
`GRAFANA_DOMAIN`, создать DNS-запись и установить новый пароль Grafana.
Фоновая загрузка и автообновление plugins отключены, чтобы запуск не зависел от
доступа контейнера к `grafana.com`.
`GRAFANA_ADMIN_PASSWORD` применяется при создании нового
`grafana_data`; для уже существующего volume пароль меняется через UI или
`grafana cli admin reset-admin-password`.

Текущие alert rules отображаются в Prometheus, но уведомления пока никуда не
отправляются: для маршрутизации в Telegram, email или Slack потребуется
Alertmanager.
