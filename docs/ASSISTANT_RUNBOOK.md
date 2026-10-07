# Помощник PI Planner: запуск бэкенда

Полный контракт запросов и ответов — [OpenAPI](openapi/assistant.yaml). Чат хранит историю и
закрепляет ревизию контекста, версии двух обязательных промптов, профиль модели и ревизию
базы знаний. Справочный чат работает без прогона. Плановый чат читает сохранённый снимок;
сценарии не изменяют опубликованный план.

## Запуск

1. Задайте `POSTGRES_PASSWORD`, `PI_PLANNER_ADMIN_TOKEN` и отдельный
   `ASSISTANT_DB_PASSWORD` (`openssl rand -hex 32`) в `.env`. Пустые `*_API_KEY`
   допустимы, если используются только локальные модели.
   Ограничьте доступ к файлу: `chmod 600 .env`.
   После изменения ключей пересоздайте `app` и `assistant-worker` через
   `docker compose up -d --force-recreate app assistant-worker`: окружение уже
   запущенного контейнера не обновляется. Для Gemini и Groq worker использует
   отдельную сеть `ai_egress`; портов наружу у него нет.
2. Для существующего тома PostgreSQL обновите образ БД и примените миграции:

   ```bash
   docker compose up -d db
   docker compose run --rm migrate
   docker compose up -d --build app assistant-worker caddy
   ```

   База использует PostgreSQL 17 с pgvector и сохраняет прежний `postgres_data`.
   Не запускайте приложение поверх старого образа PostgreSQL без расширения `vector`.
3. Для локального поиска правил поднимите Ollama, установите модель эмбеддингов и
   проиндексируйте разрешённые документы. Команды ниже выполняются из корня
   репозитория при стандартном имени Compose-проекта; при `COMPOSE_PROJECT_NAME`
   замените `$(basename "$PWD")` на это имя:

   ```bash
   docker compose --profile rag-local up -d ollama
   # На время загрузки дайте Ollama исходящий доступ, затем отключите его.
   docker network connect "$(basename "$PWD")_ai_egress" "$(docker compose --profile rag-local ps -q ollama)"
   docker compose --profile rag-local exec ollama ollama pull embeddinggemma
   docker network disconnect "$(basename "$PWD")_ai_egress" "$(docker compose --profile rag-local ps -q ollama)"
   docker compose exec app python tools/index_assistant_kb.py
   ```

   При остановке локальных моделей используйте `docker compose --profile rag-local down`.
   Обычный `docker compose down` не включает Ollama из профиля и оставляет сеть
   `backend` занятой. Том `ollama_data` при остановке сохраняется; флаг `-v` удалит его.

   Альтернатива последней команде — `POST /api/assistant/kb/reindex` с ролью `admin`;
   состояние — `GET /api/assistant/kb/status`. Индексируются только файлы из
   `docs/assistant_kb_manifest.json`. Нужна установленная модель эмбеддингов: код не
   скачивает её автоматически. Если индекс ещё не готов, чат сообщает об ограничении.
   Код `kb_not_indexed` означает отсутствие активной ревизии: до первой успешной
   индексации это ожидаемо. В `GET /api/assistant/kb/status` тогда `revision=null`
   и `documents=0`. После индексации проверьте, что ревизия появилась и документов
   больше нуля; ключ Gemini или Groq сам по себе базу знаний не индексирует.

Контейнеры `app`, `assistant-worker` и опциональный `ollama` имеют read-only rootfs,
сброшенные Linux capabilities и запрет повышения привилегий. Ollama пишет модели
только в том `ollama_data`. SQL для ассистента задан кодом, модель не исполняет
собственные запросы. `assistant-db-init` создаёт worker отдельную роль БД: чтение
снимков и запись только в служебные таблицы ассистента. Worker не получает
административный API-токен. Само приложение пока использует роль `postgres` —
её дальнейшее ограничение требует разделения прав остальных функций планировщика.

Запрос «разобрать критические проблемы» строит полный список из закреплённого
снимка и показывает его страницами. Очередность задаёт сервер: качество данных,
зависимости, сроки, ресурсы, затем серьёзность и охват. По запросу «подробнее о
первой» список пересчитывается из того же неизменяемого снимка. При отказе
объяснения модели сервер показывает проверенный список проблем. Причины отказа
и ID задания возвращаются только при входе по `PI_PLANNER_ADMIN_TOKEN`;
администратор с обычным пользовательским токеном их не получает.

## Профили модели

Профиль создаёт администратор через `POST /api/assistant/profiles`. В базе хранится
`api_key_ref`, а не значение ключа. После создания вызовите
`POST /api/assistant/profiles/{profileId}/check`; затем выберите ID профиля при
создании чата. Для облачной модели используйте `privacy_mode=configured`, для
локального сервера — `local_only`.

Пример Groq:

```json
{
  "name": "groq-main",
  "protocol": "openai_compatible",
  "base_url": "https://api.groq.com/openai/v1",
  "model": "openai/gpt-oss-20b",
  "auth_type": "bearer",
  "api_key_ref": "env:GROQ_API_KEY",
  "network_scope": "external"
}
```

Пример Gemini: `protocol=gemini`, `base_url=https://generativelanguage.googleapis.com/v1beta`,
`model=gemini-3.5-flash-lite`, `auth_type=header`, `auth_header_name=x-goog-api-key`,
`api_key_ref=env:GEMINI_API_KEY`, `network_scope=external`.

Для Qwen Cloud или своего OpenAI-совместимого сервера укажите `protocol=openai_compatible`,
**собственный** корневой `base_url`, имя модели и тип авторизации. Сервис добавляет
`/chat/completions` к корневому адресу. Если сервер принимает Bearer-токен,
используйте `auth_type=bearer` и `api_key_ref=env:QWEN_API_KEY` либо другую переменную.
Если авторизации нет, используйте `auth_type=none` и не передавайте `api_key_ref`.
Внутренний hostname нужно перечислить в `PI_PLANNER_INTERNAL_LLM_HOSTS`; внешний
endpoint должен использовать HTTPS. Не записывайте ключ в `base_url` или JSON профиля.

Для локального Qwen через Ollama:

```bash
# Если модели ещё нет, временно подключите Ollama к ai_egress, как в шаге 3 раздела «Запуск».
docker compose --profile rag-local exec ollama ollama pull qwen3.5:9b
```

```json
{
  "name": "qwen-local",
  "protocol": "ollama",
  "base_url": "http://ollama:11434",
  "model": "qwen3.5:9b",
  "auth_type": "none",
  "network_scope": "internal"
}
```

Из контейнера `localhost` означает сам контейнер приложения; для соседнего сервиса
используйте `ollama`, а для отдельного хоста — доступное контейнеру имя и allowlist.
Память/скорость локальной модели зависят от железа; `OLLAMA_MEMORY_LIMIT` можно менять.

## Чат, промпты и сценарии

Администратор редактирует единственный дефолтный промпт через
`PUT /api/assistant/prompts/default`; пользователь свой обязательный личный промпт —
через `PUT /api/assistant/prompts/me` с телом `{"content":"..."}`. Прежние версии
остаются в истории запросов. Текст промпта не меняет роль и права пользователя.

Создайте справочный чат `POST /api/assistant/conversations` с телом:

```json
{"scope":"knowledge","provider_profile_id":1,"privacy_mode":"configured"}
```

Для планового чата используйте `scope=planning` и добавьте `pi_id`, `scenario_id`,
`run_id`. Отправьте `POST /api/assistant/conversations/{conversationId}/messages`
с заголовком `Idempotency-Key` и телом:

```json
{"text":"Почему команда ALPHA не укладывается?","expected_context_revision":1,"expected_last_message_id":null}
```

Ответ `202` содержит `job_id`; состояние и результат читайте через
`GET /api/assistant/jobs/{jobId}`, историю — через
`GET /api/assistant/conversations/{conversationId}/messages`. Продолжение разговора
передаёт ID последнего сообщения и ту же ревизию контекста. При выборе нового
прогона используйте `POST .../context`: старая история и основания сохраняются.
`GET /api/assistant/evidence/{evidenceId}` возвращает сохранённое основание только
владельцу чата.

Расчёт мер требует роли `planner` и планового чата. Пример для
`POST /api/assistant/conversations/{conversationId}/scenarios/compare` с
`Idempotency-Key`:

```json
{
  "expected_context_revision": 1,
  "alternatives": [
    {"measures": [{"kind":"hire","role_id":7,"team_id":"ALPHA","rate":0.5,
                   "start_sprint":2,"hiring_lag_sprints":1,"skill_ids":[]}]},
    {"measures": [{"kind":"loan","engineer_id":"ENG-1","team_id":"ALPHA",
                   "rate":0.5,"start_sprint":2}]}
  ]
}
```

`skill_ids` должны покрывать требования роли; пустой массив годится только если
подтверждённых обязательных навыков нет. Для `train` дополнительно передайте
`trainee_id`, `mentor_id`, `training_sprints` и `mentor_rate`. До трёх альтернатив,
до пяти мер в каждой. Совместный пакет указывается одним массивом `measures`, его
эффект вычисляется заново. Последняя проверенная комбинация доступна чату для
объяснения; список советов и их актуальность — в `GET .../recommendations`.
Повторный расчёт прежней альтернативы может содержать `supersedes` с ID совета.

Ошибки сценария и недоступные источники возвращаются явными статусами job.
`verified_by_scenario` относится только к успешно пересчитанному пакету.
КPI `bus_factor` в сценарии помечается `unavailable`, поскольку текущая формула
не пересчитывает состав носителей навыков по каждому спринту.

## Прямые ответы и справка без индекса

Вопрос о количестве заёмных часов рассчитывается по назначениям закреплённого
снимка: суммируются часы, у которых home_team_id отличается от serving_team_id.
Ответ включает сумму и разбивку по спринтам, включая нулевые значения. Для
этого вопроса модель и документный поиск не вызываются.

Вопрос «почему задача не попала в план» при однозначном ID получает сохранённую
причину решения планировщика. Допустимы варианты `ANL-3042`, `ANL 3042` и
`ANL3042`. Вопрос не подменяется общим списком проблем при ошибке модели.
Новый общий вопрос не наследует объект прошлого ответа без отсылки к нему.

Базовые правила ролей, ёмкости и Bus Factor передаются справочному чату даже
без доступного индекса документов. Справка не содержит чисел конкретного
прогона и не заменяет сценарный расчёт эффектов мер. Имена KPI с пробелами,
например Bus Factor и PI Predictability, распознаются наряду с кодами.

Проверка на сохранённом снимке: заём 243 часа; задача ANL-3042 перенесена из-за
отсутствующей роли «Руководитель проекта». Настроенная модель Groq ответила
на «Откуда берутся роли, ёмкость команд и Bus Factor?» без требования прогона.
Числа относятся к проверенному снимку и не являются нормативом для других PI.

Вопросы «с чего начать увеличение количества задач», «проанализируй, что мне
делать» и «какие есть предложения» в плановом чате получают серверный план
разбора ограничений без вызова модели. Он перечисляет отсутствующие роли,
неподтверждённые навыки и причины переноса, предлагает порядок проверки и
сценарный расчёт. Количество задач с дефицитом роли не выдаётся за рассчитанный
эффект найма. Это позволяет ответить и при исчерпанной квоте провайдера.

Ошибки фонового задания возвращаются в документированном формате
`{request_id, error: {code, message, retryable}}`. Для rate_limited интерфейс
получает объяснение лимита запросов и признак возможности повторить вопрос;
старые сохранённые ошибки с плоской структурой нормализуются при чтении.
