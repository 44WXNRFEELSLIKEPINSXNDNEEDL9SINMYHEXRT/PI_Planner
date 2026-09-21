-- =====================================================================
--  НЕГАТИВНЫЙ ТЕСТ ИНВАРИАНТОВ.
--
--  Проверки, которые всегда молчат, ничего не доказывают: на живых прогонах
--  v_plan_violations пуст, и «пусто» одинаково выглядит у работающей проверки
--  и у опечатки в ней. Поэтому здесь собирается ЗАВЕДОМО БИТЫЙ прогон — внутри
--  транзакции, с ROLLBACK в конце: база после теста не меняется.
--
--  Запуск:
--    psql -h 127.0.0.1 -U postgres -d pi_planner -v ON_ERROR_STOP=1 -f tools/negative_test.sql
--  Ожидаемый набор кодов и разбор — docs/RUNBOOK.md, раздел 9.
--  Файл держим в UTF-8 (без BOM): в выводе есть кириллица.
--
--  Что именно ломаем (код проверки в скобках):
--    blocking    — перенесена, а блокируемая стоит в квартале      (P)
--    blocked     — окно 3..9 при 7 спринтах                        (R)
--    early       — старт в 1-м спринте раньше графа                (Q)
--    reasonless  — перенос без decision_reason                     (S)
--    over        — 9999 ЧЧ одному исполнителю в одном спринте      (B, X)
--    все назначения — в спринте 1 при as_of_sprint = 3             (Y)
--    task_state  — одна строка вместо 45                           (Z)
--    alerts      — ни одного при 30 переносах                      (AA)
--    kpi_snapshots — ни одной строки                               (W)
--  Остальные живые задачи получают честный перенос с причиной M2, иначе тест
--  ловил бы ещё и «задача без решения» (M) и перестал быть точечным.
-- =====================================================================
BEGIN;

CREATE TEMP TABLE neg (kind TEXT PRIMARY KEY, task_id TEXT NOT NULL, team_id TEXT NOT NULL)
ON COMMIT DROP;

-- Живое ребро графа: A блокирует B.
INSERT INTO neg (kind, task_id, team_id)
SELECT 'blocking', d.blocking_task_id, t.team_id
FROM task_dependencies d
JOIN tasks t  ON t.task_id = d.blocking_task_id  AND t.status <> 'Done'
JOIN tasks kt ON kt.task_id = d.blocked_task_id  AND kt.status <> 'Done'
ORDER BY d.blocking_task_id, d.blocked_task_id
LIMIT 1;

INSERT INTO neg (kind, task_id, team_id)
SELECT 'blocked', d.blocked_task_id, t.team_id
FROM task_dependencies d
JOIN tasks t ON t.task_id = d.blocked_task_id
WHERE d.blocking_task_id = (SELECT task_id FROM neg WHERE kind = 'blocking')
ORDER BY d.blocked_task_id
LIMIT 1;

-- Задача, которую граф разрешает планировать не раньше 2-го спринта.
INSERT INTO neg (kind, task_id, team_id)
SELECT 'early', l.task_id, l.team_id
FROM tasks l
JOIN task_sequence q ON q.task_id = l.task_id
WHERE l.status IN ('ToDo', 'InProgress')
  AND q.earliest_start_sprint > 1
  AND l.task_id NOT IN (SELECT task_id FROM neg)
ORDER BY l.task_id
LIMIT 1;

INSERT INTO neg (kind, task_id, team_id)
SELECT 'reasonless', l.task_id, l.team_id
FROM tasks l
WHERE l.status IN ('ToDo', 'InProgress')
  AND l.task_id NOT IN (SELECT task_id FROM neg)
ORDER BY l.task_id
LIMIT 1;

INSERT INTO neg (kind, task_id, team_id)
SELECT 'over', l.task_id, l.team_id
FROM tasks l
WHERE l.status IN ('ToDo', 'InProgress')
  AND l.task_id NOT IN (SELECT task_id FROM neg)
ORDER BY l.task_id DESC
LIMIT 1;

\echo '=== 0. Кого выбрали в «плохие» ==='
SELECT kind, task_id, team_id FROM neg ORDER BY kind;

-- Пересчёт на начало 3-го спринта: спринты 1..2 уже закрыты, поэтому любое
-- назначение в них — нарушение (Y).
INSERT INTO plan_runs (pi_id, as_of_sprint, algorithm, params, status, note)
SELECT pi_id, 3, 'negative-test',
       '{"purpose": "negative test of v_plan_violations; rolled back"}'::jsonb,
       'ok', 'синтетический битый прогон: tools/negative_test.sql'
FROM pi_periods
RETURNING run_id AS bad
\gset

\echo '=== 1. Расписание: решение по каждой живой задаче ==='
-- «Толпа» — все живые задачи той же команды, что и blocked-задача. Их ставим
-- в 4-й спринт: суммарные SP не влезают в ёмкость команды → срабатывает A
-- (SP_OVERFLOW), а по 1 ЧЧ на задачу — ещё и H (UNDER_ALLOCATED).
CREATE TEMP TABLE crowd ON COMMIT DROP AS
SELECT t.task_id
FROM tasks t
WHERE t.status IN ('ToDo', 'InProgress')
  AND t.team_id = (SELECT team_id FROM neg WHERE kind = 'blocked')
  AND t.task_id NOT IN (SELECT task_id FROM neg);

CREATE TEMP TABLE neg_plan ON COMMIT DROP AS
SELECT t.task_id,
       t.team_id,
       CASE WHEN n.kind IN ('blocking', 'reasonless') THEN 'deferred_next_pi'
            WHEN n.kind IS NOT NULL                   THEN 'in_quarter'
            WHEN c.task_id IS NOT NULL                THEN 'in_quarter'
            ELSE 'deferred_next_pi' END AS decision,
       CASE WHEN n.kind = 'blocked'       THEN 3
            WHEN n.kind = 'early'         THEN 1
            WHEN n.kind = 'over'          THEN 3
            WHEN c.task_id IS NOT NULL    THEN 4
            ELSE NULL END AS start_sprint,
       CASE WHEN n.kind = 'blocked'       THEN 9
            WHEN n.kind = 'early'         THEN 1
            WHEN n.kind = 'over'          THEN 3
            WHEN c.task_id IS NOT NULL    THEN 4
            ELSE NULL END AS end_sprint,
       CASE WHEN n.kind = 'reasonless'    THEN NULL   -- S: перенос без причины
            WHEN n.kind IS NOT NULL       THEN 'M2'
            WHEN c.task_id IS NOT NULL    THEN NULL
            ELSE 'M2' END AS decision_reason,
       (c.task_id IS NOT NULL) AS crowd
FROM tasks t
LEFT JOIN neg n  ON n.task_id = t.task_id
LEFT JOIN crowd c ON c.task_id = t.task_id
WHERE t.status IN ('ToDo', 'InProgress');

INSERT INTO plan_task_schedule
    (run_id, task_id, start_sprint, end_sprint, forecast_end_date, decision, decision_reason)
SELECT :bad, p.task_id, p.start_sprint, p.end_sprint, NULL, p.decision, p.decision_reason
FROM neg_plan p;

\echo '=== 2. Назначения: 9999 ЧЧ в закрытый спринт и по 1 ЧЧ на задачу-толпу ==='
-- Исполнитель — реальная пара «инженер × его родная роль» из витрины покрытия
-- (ADR-012), орбита — его же: так молчат ROLE_NOT_COVERED и HOME_TEAM_NOT_ORBIT,
-- а срабатывают именно B (ENGINEER_OVERLOAD), X (ORBIT_OVER_BUDGET),
-- Y (ASSIGNMENT_IN_CLOSED_SPRINT) и A/H на «толпе».
INSERT INTO plan_assignments
    (run_id, task_id, sprint_no, engineer_id, role_id, hours, home_team_id, serving_team_id)
SELECT :bad, p.task_id,
       CASE WHEN p.crowd THEN 4 ELSE 1 END,
       c.engineer_id, c.role_id,
       CASE WHEN p.crowd THEN 1 ELSE 9999 END,
       (SELECT o.team_id FROM engineer_orbits o
        WHERE o.engineer_id = c.engineer_id ORDER BY o.team_id LIMIT 1),
       p.team_id
FROM neg_plan p
CROSS JOIN LATERAL (
    SELECT c.engineer_id, c.role_id
    FROM v_engineer_role_coverage c
    WHERE c.is_native
    ORDER BY c.engineer_id
    LIMIT 1
) c
WHERE p.decision = 'in_quarter';

\echo '=== 3. Слепок состояния: одна строка вместо всех задач ==='
INSERT INTO task_state
    (run_id, task_id, as_of_sprint, status, remaining_hh, remaining_sp, forecast_end_sprint)
SELECT :bad, t.task_id, 3, 'Deferred', 0, 0, NULL
FROM tasks t ORDER BY t.task_id LIMIT 1;

-- Ни алертов, ни KPI, ни базовой линии — намеренно.

\echo '=== 4. Что сработало (ожидаем P, Q, R, S, W, X, Y, Z, AA и сопутствующие) ==='
SELECT check_code, severity, COUNT(*) AS rows, MIN(entity) AS example
FROM v_plan_violations
WHERE run_id = :bad
GROUP BY check_code, severity
ORDER BY severity, check_code;

\echo '=== 5. Итог по битому прогону ==='
SELECT COUNT(*)                                   AS violations,
       COUNT(*) FILTER (WHERE severity = 'error') AS errors,
       COUNT(DISTINCT check_code)                 AS codes
FROM v_plan_violations WHERE run_id = :bad;

\echo '=== 6. Откат: база остаётся такой, какой была ==='
ROLLBACK;

\echo '--- После ROLLBACK битого прогона в plan_runs нет ---'
SELECT COUNT(*) AS negative_runs_left
FROM plan_runs WHERE algorithm = 'negative-test';