# Схема БД: что читать бэкенду

**29 таблиц + 17 вьюх**, схема `public`. Бэкенду нужны не все — ниже только то,
что стоит отдавать наружу.

Правило разделения: **DS-слой пишет, бэкенд читает.** Единственное исключение —
`plan_runs`, если вы решите запускать пересчёт из API.

---

## 1. Справочный слой — отдаётся во фронт как есть

| Таблица / вьюха | Строк | Что внутри |
|---|---|---|
| `v_task_board` | 45 | **Доска задач, денормализована.** Задача + инициатива + приоритет + остаток часов + позиция в графе. Джойнить ничего не надо. |
| `v_orbit_map` | 30 | **Звёздная карта.** Инженер, роль, грейд, массив команд, массив навыков, bus factor. Фронт рисует граф прямо отсюда. |
| `v_team_capacity_sp` | 6 | Ёмкость команды в SP: velocity, focus factor, доступно на спринт и на квартал. |
| `v_role_deficit` | — | **Главная аналитическая витрина.** Дефицит по связке «команда × роль» с вердиктом. |
| `v_bus_factor` | 21 | Незаменимость по ролям, включая роли, которых нет в штате вообще. |
| `sprints` | 7 | Сетка квартала: номер спринта, даты, **`length_days`** (генерируемая колонка; у 7-го спринта 8 дней). |
| `initiatives` | 15 | Инициативы PRODF со скорингом. |
| `ref_result_options` | 8 | Справочник «Варианты выбора цели» — выпадашки в UI планирования. |
| `v_dq_summary` | — | Сводка по качеству исходных данных. Годится отдельным экраном «Диагностика». |

### Календарь и фонд часов (ADR-007, ADR-017)

Квартал: `PI-2026-Q3` = 01.07.2026–22.09.2026, 84 дня (6 × 14),
**7 спринтов**, последний (7-й) короткий — 23.09–30.09, 8 дней. Фонд часов
пропорционален длине спринта, поэтому множитель нельзя «посчитать по числу
спринтов»: 80 ЧЧ × 7 = 560 приписали бы кварталу 4.29 ЧЧ, которых в нём нет.

| Вьюха | Что даёт | На Q3-2026 |
|---|---|---|
| `v_sprint_fund_factor` | множитель фонда спринта: `length_days / sprint_length_days` | все шесть спринтов — **1.0000** |
| `v_pi_fund_factor` | множитель фонда всего PI: `Σ length_days / sprint_length_days`, `days_total` | **6.0000**, 84 дня → **480 ЧЧ** на ставку |
| `sprints.length_days` | длина спринта в днях (генерируемая колонка, ETL её не пишет) | 14 … 14, последний 8 |

Фонд **одного полного** спринта лежит в `v_team_capacity_sp.available_sp_per_sprint`
и в `v_satellite_capacity.hours_own` (он уже умножен на множитель своего спринта);
для короткого спринта ёмкость в SP умножайте на `v_sprint_fund_factor.factor` —
так делает проверка `SP_OVERFLOW`. Фонд **всего квартала** — `available_sp_per_pi`
и `v_role_supply_hh.hh_per_pi`, они уже посчитаны через `v_pi_fund_factor`.

> Бэкенду арифметику «92 / 14» повторять не надо и **нельзя**: единственный
> источник правды — эти две вьюхи, иначе ваш фонд разъедется с инвариантами и
> приёмкой. Готовые границы прогона (`params.calendar`) — в `plan_runs`.

### Взаимозаменяемость ролей (ADR-009)

| Вьюха | Для чего |
|---|---|
| **`v_engineer_role_coverage`** | **Главный вход планировщика.** Какие роли может закрывать инженер: родную + разрешённые замещения. `is_native = false` — замещение, `efficiency` — множитель часов. Читать только эту вьюху, `engineers.role_id` напрямую не хватит. **Сейчас замещения отклонены организаторами (ADR-010): 30 строк, все `is_native = true`.** |
| `v_role_coverage_org` | Срез по компании: где нужен **наём**, а где хватит займов. Сейчас `НАЙМ` = **665 ЧЧ на 6 ролях**: РП 449 + четыре 1С-роли 132 + поддержка 84. |
| `v_role_deficit_effective` | Дефицит по командам с учётом замещения — сравнивать со строгим `v_role_deficit`. |
| `v_plan_assignment_detail` | Назначения с флагами `is_loan` и `is_substitution`. **Показывать в UI:** «всё спланировалось» без ответа «кем» на защите не проходит. |

Правила лежат в таблице `role_substitutions` со `status`:
`proposed` (наша гипотеза) / `confirmed` / `rejected`. **Сейчас все 6 строк
`rejected`** — организаторы запретили замещения (ответ №2, ADR-010), человека
можно только дообучать. Планировщик игнорирует `rejected`, поэтому
`v_engineer_role_coverage` отдаёт только родные роли. Механизм остался в схеме
как журнал решений и ручка на будущее: включается обратно правкой
`db/03_substitutions.sql`.

## 2. Контракт планировщика — главное для бэкенда

Всё, что пишет DS-слой. Структура заморожена: **менять только предупредив.**

```
plan_runs ──┬── plan_baseline        базовая линия Недели 0 (для KPI)
            ├── plan_task_schedule   какая задача в какие спринты, какое решение
            ├── plan_assignments     кто/на что/сколько часов + флаг займа
            ├── task_state           слепок задачи на каждый пересчёт
            ├── alerts               3 уровня рисков
            └── kpi_snapshots        3 KPI с нормами
```

Ключевые моменты:

* **`plan_runs.run_id` — ось всего.** Каждый пересчёт раз в 2 недели создаёт
  новый `run_id`, старые не трогаются. Фронт всегда показывает конкретный
  прогон; «текущий» = `MAX(run_id) WHERE status='ok'`.
* **`plan_runs.status = 'infeasible'`** означает, что алгоритм не уложил бэклог
  даже с переносами. Это нормальный результат, а не ошибка — показывать явно.
* **`plan_assignments.is_loan`** считает СУБД (`home_team_id <> serving_team_id`).
  Не вычисляйте на своей стороне.
* **`plan_task_schedule.decision`** = `in_quarter` | `deferred_next_pi` |
  `cancelled`. Для переносов заполнен `decision_reason` — код из справочника
  причин, это объяснение для заказчика.
* **`alerts.level`** = `red` (срыв дедлайна) | `yellow` (каскадный сдвиг) |
  `orange` (дефицит по роли). `payload` — свободный JSONB с деталями.
* **`kpi_snapshots`** несёт `target_min` / `target_max` рядом со значением —
  не зашивайте пороги во фронте, красьте плашку по этим полям.

## 2а. Приёмка плана

```sql
SELECT * FROM v_plan_violations WHERE run_id = :run_id AND severity = 'error';
```

29 проверок из `db/05_invariants.sql`, правила — `docs/PLANNER_SPEC.md`, раздел 7.
`severity = 'error'` блокирует, `'warning'` требует показа в UI: сейчас это
`SUBSTITUTION_USED` (инженер работает не по своей роли), `PLANNED_END_OVERSAIL`
(прогноз выходит за даты исходного плана) и `WINDOW_HAS_GAP` (в окне задачи есть
спринт без назначений).

## 2б. API для фронта — один маршрут на все витрины

Фронт получает данные из этой схемы только через `app/views.py` (ADR-019):

```
GET /api/views                                            # справочник витрин
GET /api/views/{view}?run_id=&limit=&offset=&order=       # строка витрины как есть
```

Один маршрут, а не маршрут на витрину: набор лейблов `route` в метриках заморожен
(ADR-018), и отдельный путь на каждый экран раздул бы его до числа экранов. В
метриках все витрины — одна серия `/api/views/{view}`.

**Что отдаётся наружу** — 25 источников в белом списке (см. `GET /api/views`):

| Группа | Источники |
|---|---|
| Справочный слой (§1) | `v_task_board`, `tasks`, `v_task_remaining_hh`, `v_orbit_map`, `v_satellite_capacity`, `v_engineer_role_coverage`, `v_team_capacity_sp`, `teams`, `v_role_deficit`, `v_role_deficit_effective`, `v_role_coverage_org`, `v_bus_factor`, `v_sprint_fund_factor`, `v_pi_fund_factor`, `sprints`, `initiatives`, `ref_result_options`, `v_dq_summary` |
| Контракт прогона (§2) | `plan_runs`, `plan_task_schedule`, `v_plan_assignment_detail`, `plan_baseline`, `alerts`, `kpi_snapshots`, `v_plan_violations` |

`run_id` фильтрует только источники контракта: у них он и есть ось (§2), а
`plan_runs` намеренно **не** фильтруется — она нужна, чтобы прогон выбрать.
Без `run_id` подставляется последний удачный (`MAX(run_id) WHERE status = 'ok'`),
и в ответе это видно как `run_default: true`.

**Чего наружу нет:** внутренняя кухня ETL (§3) и промежуточные вьюхи
(`v_role_supply_hh`, `v_backlog_demand`) — у экранов есть готовые витрины с
вердиктом. Писать через `/api/*` нельзя: сессии read-only.

**Конверт ответа** (подробно — `docs/UI_SPEC.md`):

```json
{
  "view": "kpi_snapshots", "kind": "table", "screen": "KPI", "note": "…",
  "run_id": 2, "run_column": "run_id", "run_default": true,
  "as_of": "2026-09-21T19:46:00+03:00",
  "order": ["sprint_no", "kpi_code"], "limit": 500, "offset": 0,
  "count": 9, "returned": 9, "truncated": false, "has_more": false,
  "columns": ["run_id", "sprint_no", "kpi_code", "value", "target_min", "target_max", "details"],
  "items": [{"run_id": 2, "sprint_no": 1, "kpi_code": "say_do_ratio", "value": "0.00",
             "target_min": "90.00", "target_max": "105.00", "details": {"…": "…"}}]
}
```

Четыре правила, которые держат фронт и документы согласованными:

* `as_of` в шапке экрана обязателен: данные читаются из базы на момент запроса;
* `count` считается отдельным `COUNT(*)` только при обрезании — «45 из 300» иначе
  было бы неправдой, а на короткой странице лишний запрос не нужен;
* `numeric` уезжает **строкой** (`"140.00"`): это не строка ради строки, а
  сохранение точности, фронт конвертирует сам;
* `ORDER BY` собирается только из `orderable` витрины (имя колонки параметром не
  подставить), поэтому `?order=1;--` — это 400 с подсказкой, а не 500.

**Коды ответов:** 200 (в том числе пустая витрина с `count: 0`), 400
`bad_request` (параметр), 404 `not_found` (витрина, в теле — `known`),
503 `database_unavailable` (база — как у `/api/health`).

## 3. Внутренняя кухня ETL — бэкенду не нужно

`load_batches`, `dq_issues`, `role_aliases`, `task_sequence`,
`task_role_estimates`, `task_role_spent`, `skills`, `engineer_skills`,
`team_history`, `pi_periods`. Лежат в той же БД, читать можно, но наружу
отдавать нечего: границы PI и длины спринтов бэкенд получает через `sprints`,
`v_pi_fund_factor` и `v_sprint_fund_factor`, а ещё они продублированы в
`plan_runs.params.calendar`.

---

## Примеры запросов

**Текущий прогон и его KPI:**
```sql
WITH cur AS (SELECT MAX(run_id) AS run_id FROM plan_runs WHERE status = 'ok')
SELECT k.kpi_code, k.value, k.target_min, k.target_max,
       k.value BETWEEN COALESCE(k.target_min, '-Infinity')
                   AND COALESCE(k.target_max, 'Infinity') AS in_norm
FROM kpi_snapshots k JOIN cur ON cur.run_id = k.run_id
WHERE k.sprint_no = (SELECT MAX(sprint_no) FROM kpi_snapshots WHERE run_id = cur.run_id);
```

**Гант: задачи текущего прогона по спринтам:**
```sql
SELECT s.task_id, b.summary, b.team_id, b.priority_rung,
       s.start_sprint, s.end_sprint, s.decision
FROM plan_task_schedule s
JOIN v_task_board b USING (task_id)
WHERE s.run_id = $1 AND s.decision = 'in_quarter'
ORDER BY s.start_sprint, b.priority_rung DESC;
```

**Календарь квартала и множители фонда (для шапки ганта):**
```sql
SELECT s.sprint_no, s.start_date, s.end_date, s.length_days, f.factor
FROM sprints s
JOIN v_sprint_fund_factor f USING (pi_id, sprint_no)
JOIN (SELECT pi_id FROM pi_periods ORDER BY start_date DESC LIMIT 1) p USING (pi_id)
ORDER BY s.sprint_no;
-- 1..6: по 14 дней, factor 1.0000
```

**Что перенесли и почему:**
```sql
SELECT b.prodf_id, s.task_id, b.summary, b.priority_rung,
       s.decision, r.label AS reason
FROM plan_task_schedule s
JOIN v_task_board b USING (task_id)
LEFT JOIN ref_mismatch_reasons r ON r.code = s.decision_reason
WHERE s.run_id = $1 AND s.decision <> 'in_quarter'
ORDER BY b.priority_rung DESC;
```

**Займы между командами в спринте:**
```sql
SELECT sprint_no, home_team_id, serving_team_id, engineer_id, SUM(hours) AS hh
FROM plan_assignments
WHERE run_id = $1 AND is_loan
GROUP BY 1,2,3,4 ORDER BY sprint_no, hh DESC;
```

**Лента алертов:**
```sql
SELECT sprint_no, level, alert_type, entity_type, entity_id, message, payload
FROM alerts WHERE run_id = $1
ORDER BY sprint_no,
         CASE level WHEN 'red' THEN 1 WHEN 'orange' THEN 2 ELSE 3 END;
```

---

## Чего в данных нет (чтобы не искали)

* **Связи «задача → требуемые навыки».** Есть только «задача → роль → часы».
  Правило техсоответствия из онбординга требует матчинга по стеку, значит связь
  выводится DS-слоем. Пока её нет — фронту показывать матчинг по **роли**,
  а планировщику использовать `v_engineer_role_coverage`.
* **1С-компетенции в штате нет вообще** — ни одного упоминания среди 117
  навыков. 132 ЧЧ на задачах `SRV-4043`, `SRV-4091`, `MOB-7013` закрыть некем:
  организаторы запретили замещения и предложили только дообучение (ADR-010),
  поэтому эти задачи уезжают в следующий PI с оранжевым алертом.
* **Базовой линии Недели 0** в исходнике фактически нет (ADR-004) — берите
  только из `plan_baseline`, не из `tasks.committed_week0`.
* **Границы квартала** в датасете не заданы, выведены (ADR-007). Если
  организаторы назовут другие даты — поменяется таблица `sprints`, номера
  спринтов в контракте останутся прежними.

---

## 4. Загрузки и факт спринтов (ADR-021)

Сервер пишет только через три маршрута — остальное по-прежнему только на чтение.

| Маршрут | Что делает |
|---|---|
| `POST /api/dataset?filename=x.xlsx` | тело — xlsx датасета: ETL, полная заливка, базовый план. Прежние прогоны и факт стираются |
| `GET /api/actuals/template?sprint=N` | CSV-шаблон факта: живые задачи и колонки ролей |
| `POST /api/actuals?sprint=N&filename=…` | тело — CSV/XLSX факта спринта: проверка, журнал, пересборка состояния, пересчёт на спринт N+1 |

Ответы: `200` — сводка и результат прогона; `400 bad_upload` — файл не принят,
в теле `problems` со списком проблем по строкам; `503` — база недоступна.

### Таблицы

| Таблица | Что внутри |
|---|---|
| `actual_uploads` | журнал загрузок: один спринт — одна строка |
| `task_actuals` | статус и даты задачи по этой загрузке |
| `task_actual_spent` | часы по ролям **за этот спринт** (не накопительно) |
| `tasks_seed_state`, `task_role_spent_seed` | снимок датасета: точка отсчёта воспроизведения |
| `plan_task_sp` | доли Story Points задачи по спринтам (ADR-020) |
| `ref_decision_reasons` | справочник причин решений планировщика |

`apply_actuals()` пересобирает текущее состояние `tasks` и `task_role_spent` из
снимка плюс все загрузки по порядку. Идемпотентна, поэтому повторная загрузка
факта за спринт безопасна: состояние собирается с нуля.

### Новые витрины

| Витрина | Для чего |
|---|---|
| `v_bus_factor_skill` | **Bus Factor по компетенциям** (ТЗ): носители навыка, риск, критичность |
| `v_engineer_absence_risk` | профиль инженера и задачи прогона, у которых нет запасного исполнителя |
| `v_team_profile` | профиль команды: состав, роли, `roles_missing`, компетенции, ёмкость |
| `v_plan_diff` | что изменилось против предыдущего прогона и **почему** (`cause`) |
| `v_sprint_deviation` | факт спринта против плана, который в нём действовал |

### Изменения в контракте

* `plan_task_schedule`: + `reason_code`, `reason_text`, `reason_details`;
* `kpi_snapshots`: + `kind` (`forecast` | `actual`), он вошёл в первичный ключ;
* `plan_runs`: + `actuals_upload_id` — на каком факте построен пересчёт.

Все изменения **дополняющие**: прежние колонки и их смысл сохранены.
