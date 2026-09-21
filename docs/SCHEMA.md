# Схема БД: что читать бэкенду

**29 таблиц + 15 вьюх**, схема `public`. Бэкенду нужны не все — ниже только то,
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
| `sprints` | 6 | Сетка квартала: номер спринта, даты начала и конца. |
| `initiatives` | 15 | Инициативы PRODF со скорингом. |
| `ref_result_options` | 8 | Справочник «Варианты выбора цели» — выпадашки в UI планирования. |
| `v_dq_summary` | — | Сводка по качеству исходных данных. Годится отдельным экраном «Диагностика». |

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

## 3. Внутренняя кухня ETL — бэкенду не нужно

`load_batches`, `dq_issues`, `role_aliases`, `task_sequence`,
`task_role_estimates`, `task_role_spent`, `skills`, `engineer_skills`,
`team_history`, `pi_periods`. Лежат в той же БД, читать можно, но наружу
отдавать нечего.

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
