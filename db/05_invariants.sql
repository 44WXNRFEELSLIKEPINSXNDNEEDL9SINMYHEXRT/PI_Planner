-- =====================================================================
--  ИНВАРИАНТЫ ПЛАНА — автоматическая приёмка результата планировщика.
--
--      SELECT * FROM v_plan_violations WHERE run_id = :run_id;
--      SELECT * FROM v_plan_violations WHERE run_id = :run_id AND severity = 'error';
--
--  КРИТЕРИЙ ПРИЁМКИ: нет строк с severity = 'error'.
--  severity = 'warning' план не отменяет, но обязан быть показан в UI
--  (замещение роли, выход за даты исходного плана, разрыв в окне задачи).
--  Каждая строка — нарушение правила из docs/PLANNER_SPEC.md, раздел 7.
--  Проверки только читают, ничего не меняют: гонять можно сколько угодно.
--
--  Проверок 29 (A..AC). Разбор ревью M2 и что из него закрыто —
--  docs/REVIEW_RESPONSE.md.
-- =====================================================================
BEGIN;

DROP VIEW IF EXISTS v_plan_violations CASCADE;

CREATE VIEW v_plan_violations AS

-- A. Ёмкость команды в SP (SP засчитываются в start_sprint) ------------
SELECT s.run_id, 'SP_OVERFLOW'::text AS check_code, 'error'::text AS severity,
       (t.team_id || ' / спринт ' || s.start_sprint)::text AS entity,
       ('запланировано ' || SUM(t.estimation_sp) || ' SP при ёмкости '
        || MAX(c.available_sp_per_sprint))::text AS detail
FROM plan_task_schedule s
JOIN tasks t             ON t.task_id = s.task_id
JOIN v_team_capacity_sp c ON c.team_id = t.team_id
WHERE s.decision = 'in_quarter' AND s.start_sprint IS NOT NULL
GROUP BY s.run_id, t.team_id, s.start_sprint
HAVING SUM(t.estimation_sp) > MAX(c.available_sp_per_sprint)

-- B. Перегрузка инженера: считать по СУММЕ ВСЕХ ОРБИТ ------------------
UNION ALL
SELECT a.run_id, 'ENGINEER_OVERLOAD', 'error',
       (a.engineer_id || ' / спринт ' || a.sprint_no)::text,
       ('назначено ' || SUM(a.hours) || ' ЧЧ при фонде '
        || MAX(e.total_capacity_rate * p.fte_hours_per_sprint)
        || ' (ставка ' || MAX(e.total_capacity_rate) || ')')::text
FROM plan_assignments a
JOIN engineers e ON e.engineer_id = a.engineer_id
CROSS JOIN pi_periods p
GROUP BY a.run_id, a.engineer_id, a.sprint_no
HAVING SUM(a.hours) > MAX(e.total_capacity_rate * p.fte_hours_per_sprint)

-- C. Инженер не умеет эту роль ----------------------------------------
UNION ALL
SELECT a.run_id, 'ROLE_NOT_COVERED', 'error',
       (a.engineer_id || ' -> ' || r.canonical_name)::text,
       ('задача ' || a.task_id || ': инженера нет в v_engineer_role_coverage '
        || 'для этой роли (ни родной, ни разрешённым замещением)')::text
FROM plan_assignments a
JOIN roles r ON r.role_id = a.role_id
WHERE NOT EXISTS (SELECT 1 FROM v_engineer_role_coverage c
                  WHERE c.engineer_id = a.engineer_id AND c.role_id = a.role_id)

-- D. Часы сняты с орбиты, на которой инженер не висит -------------------
UNION ALL
SELECT a.run_id, 'HOME_TEAM_NOT_ORBIT', 'error',
       (a.engineer_id || ' / ' || a.home_team_id)::text,
       ('задача ' || a.task_id || ': инженер не привязан к этой команде '
        || 'в engineer_orbits')::text
FROM plan_assignments a
WHERE NOT EXISTS (SELECT 1 FROM engineer_orbits o
                  WHERE o.engineer_id = a.engineer_id AND o.team_id = a.home_team_id)

-- E. serving_team_id должен совпадать с владельцем задачи ---------------
UNION ALL
SELECT a.run_id, 'SERVING_TEAM_MISMATCH', 'error',
       (a.task_id || ' / ' || a.serving_team_id)::text,
       ('задача принадлежит ' || t.team_id || ', а в назначении '
        || a.serving_team_id)::text
FROM plan_assignments a
JOIN tasks t ON t.task_id = a.task_id
WHERE a.serving_team_id <> t.team_id

-- F. Нарушен зазор между зависимыми задачами ---------------------------
UNION ALL
SELECT sb.run_id, 'DEPENDENCY_VIOLATED', 'error',
       (d.blocking_task_id || ' -> ' || d.blocked_task_id)::text,
       ('блокирующая стартует в спринте ' || sa.start_sprint
        || ', блокируемая в ' || sb.start_sprint
        || ', требуется зазор ' || d.min_gap_sprints)::text
FROM task_dependencies d
JOIN plan_task_schedule sa ON sa.task_id = d.blocking_task_id
JOIN plan_task_schedule sb ON sb.task_id = d.blocked_task_id AND sb.run_id = sa.run_id
WHERE sa.decision = 'in_quarter' AND sb.decision = 'in_quarter'
  AND sb.start_sprint < sa.start_sprint + d.min_gap_sprints

-- G. Назначение вне окна задачи ----------------------------------------
UNION ALL
SELECT a.run_id, 'ASSIGNMENT_OUTSIDE_WINDOW', 'error',
       (a.task_id || ' / спринт ' || a.sprint_no)::text,
       ('окно задачи ' || s.start_sprint || '..' || s.end_sprint)::text
FROM plan_assignments a
JOIN plan_task_schedule s ON s.run_id = a.run_id AND s.task_id = a.task_id
WHERE s.start_sprint IS NOT NULL
  AND (a.sprint_no < s.start_sprint OR a.sprint_no > s.end_sprint)

-- H. Задача взята в квартал, но часы по роли недоданы -------------------
UNION ALL
SELECT s.run_id, 'UNDER_ALLOCATED', 'error',
       (s.task_id || ' / ' || r.canonical_name)::text,
       ('нужно ' || rm.remaining_hours || ' ЧЧ, назначено '
        || COALESCE(al.h, 0))::text
FROM plan_task_schedule s
JOIN v_task_remaining_hh rm ON rm.task_id = s.task_id
JOIN roles r ON r.role_id = rm.role_id
LEFT JOIN (SELECT run_id, task_id, role_id, SUM(hours) AS h
             FROM plan_assignments GROUP BY 1, 2, 3) al
       ON al.run_id = s.run_id AND al.task_id = s.task_id AND al.role_id = rm.role_id
WHERE s.decision = 'in_quarter' AND rm.remaining_hours > 0
  AND COALESCE(al.h, 0) < rm.remaining_hours

-- I. Задача in_quarter вообще без назначений ---------------------------
UNION ALL
SELECT s.run_id, 'IN_QUARTER_WITHOUT_ASSIGNMENTS', 'error', s.task_id::text,
       'решение in_quarter, но ни одного назначения'::text
FROM plan_task_schedule s
WHERE s.decision = 'in_quarter'
  AND NOT EXISTS (SELECT 1 FROM plan_assignments a
                  WHERE a.run_id = s.run_id AND a.task_id = s.task_id)

-- J. Перенесённая задача с назначениями --------------------------------
UNION ALL
SELECT s.run_id, 'DEFERRED_WITH_ASSIGNMENTS', 'error', s.task_id::text,
       ('решение ' || s.decision || ', но назначения есть')::text
FROM plan_task_schedule s
WHERE s.decision <> 'in_quarter'
  AND EXISTS (SELECT 1 FROM plan_assignments a
              WHERE a.run_id = s.run_id AND a.task_id = s.task_id)

-- K. Задача Done попала в план -----------------------------------------
UNION ALL
SELECT s.run_id, 'DONE_TASK_SCHEDULED', 'error', s.task_id::text,
       'задача уже Done, планировать её не нужно'::text
FROM plan_task_schedule s
JOIN tasks t ON t.task_id = s.task_id
WHERE t.status = 'Done'

-- L. Спринт за пределами квартала --------------------------------------
UNION ALL
SELECT a.run_id, 'SPRINT_OUT_OF_PI', 'error',
       ('спринт ' || a.sprint_no)::text,
       ('в квартале всего ' || p.sprint_count || ' спринтов')::text
FROM plan_assignments a
CROSS JOIN pi_periods p
WHERE a.sprint_no > p.sprint_count

-- M. Живая задача не попала в план вовсе --------------------------------
UNION ALL
SELECT r.run_id, 'TASK_MISSING_FROM_PLAN', 'error', t.task_id::text,
       ('статус ' || t.status || ', но решения по задаче нет')::text
FROM plan_runs r
CROSS JOIN tasks t
WHERE t.status IN ('ToDo', 'InProgress')
  AND NOT EXISTS (SELECT 1 FROM plan_task_schedule s
                  WHERE s.run_id = r.run_id AND s.task_id = t.task_id)

-- N. Базовый прогон без зафиксированной базовой линии --------------------
UNION ALL
SELECT r.run_id, 'BASELINE_MISSING', 'error', ('прогон ' || r.run_id)::text,
       'as_of_sprint = 0, но plan_baseline пуст — KPI посчитать будет нечем'::text
FROM plan_runs r
WHERE r.as_of_sprint = 0
  AND NOT EXISTS (SELECT 1 FROM plan_baseline b WHERE b.run_id = r.run_id)

-- O. Замещение использовано — предупреждение, не ошибка ------------------
UNION ALL
SELECT d.run_id, 'SUBSTITUTION_USED', 'warning',
       (d.engineer_id || ': ' || d.native_role || ' -> ' || d.served_role)::text,
       ('задача ' || d.task_id || ', спринт ' || d.sprint_no || ', ' || d.hours
        || ' ЧЧ. Правило ещё не подтверждено авторами (ADR-009) — '
        || 'показать в UI явно')::text
FROM v_plan_assignment_detail d
WHERE d.is_substitution
  -- только РАЗРЕШЁННЫЕ замещения: недопустимые уже пойманы как ROLE_NOT_COVERED,
  -- дублировать их предупреждением не нужно
  AND EXISTS (SELECT 1 FROM v_engineer_role_coverage c
              JOIN roles rr ON rr.role_id = c.role_id
              WHERE c.engineer_id = d.engineer_id
                AND rr.canonical_name = d.served_role
                AND NOT c.is_native)

-- P. Перенесённая блокирующая оставила потомка в квартале ----------------
-- Именно эта дыра была в ревью M2 (пункт 1): раньше проверка F смотрела
-- только пару «обе в квартале», и цепочка вида SRV-4091 -> SRV-4092
-- проходила приёмку, хотя предшественник уехал в следующий PI.
UNION ALL
SELECT sb.run_id, 'DEPENDENCY_BLOCKER_DEFERRED', 'error',
       (d.blocking_task_id || ' -> ' || d.blocked_task_id)::text,
       ('блокирующая ' || sa.decision || ' (' || COALESCE(sa.decision_reason, '—')
        || '), а блокируемая стоит в квартале')::text
FROM task_dependencies d
JOIN plan_task_schedule sa ON sa.task_id = d.blocking_task_id
JOIN plan_task_schedule sb ON sb.task_id = d.blocked_task_id AND sb.run_id = sa.run_id
WHERE sb.decision = 'in_quarter' AND sa.decision <> 'in_quarter'

-- Q. Старт раньше графа (task_sequence.earliest_start_sprint) ------------
UNION ALL
SELECT s.run_id, 'START_BEFORE_EARLIEST', 'error', s.task_id::text,
       ('старт в спринте ' || s.start_sprint || ', а граф разрешает не раньше '
        || COALESCE(q.earliest_start_sprint, 1))::text
FROM plan_task_schedule s
LEFT JOIN task_sequence q ON q.task_id = s.task_id
WHERE s.decision = 'in_quarter'
  AND s.start_sprint < COALESCE(q.earliest_start_sprint, 1)

-- R. Окно задачи выходит за квартал --------------------------------------
-- Проверка L ловит только назначения; сама граница расписания не проверялась.
UNION ALL
SELECT s.run_id, 'WINDOW_OUTSIDE_PI', 'error', s.task_id::text,
       ('окно ' || s.start_sprint || '..' || s.end_sprint
        || ' при ' || p.sprint_count || ' спринтах')::text
FROM plan_task_schedule s
CROSS JOIN pi_periods p
WHERE s.decision = 'in_quarter'
  AND (s.end_sprint > p.sprint_count OR s.start_sprint > p.sprint_count)

-- S. Перенос без причины -------------------------------------------------
UNION ALL
SELECT s.run_id, 'DEFERRED_WITHOUT_REASON', 'error', s.task_id::text,
       ('решение ' || s.decision || ' без причины: UI не объяснит перенос')::text
FROM plan_task_schedule s
WHERE s.decision <> 'in_quarter' AND s.decision_reason IS NULL

-- T. Базовая линия неполна -----------------------------------------------
-- Раньше проверялось лишь «есть хотя бы одна строка» (проверка N).
UNION ALL
SELECT r.run_id, 'BASELINE_INCOMPLETE', 'error', ('прогон ' || r.run_id)::text,
       ('живых задач ' || live.n || ', строк baseline ' || b.n
        || ', нулевых SP ' || b.zero_sp)::text
FROM plan_runs r
CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM tasks WHERE status IN ('ToDo','InProgress')) live
CROSS JOIN LATERAL (
    SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE planned_sp <= 0) AS zero_sp
    FROM plan_baseline WHERE run_id = r.run_id
) b
WHERE r.as_of_sprint = 0 AND (b.n <> live.n OR b.zero_sp > 0)

-- U. Базовая линия записана пересчётом ------------------------------------
UNION ALL
SELECT b.run_id, 'BASELINE_ON_REPLAN', 'error', ('прогон ' || b.run_id)::text,
       ('as_of_sprint = ' || r.as_of_sprint
        || ': обещание Недели 0 фиксируется один раз и дальше не меняется')::text
FROM plan_baseline b
JOIN plan_runs r ON r.run_id = b.run_id
WHERE r.as_of_sprint > 0

-- V. Базовая линия разошлась с каноническим прогоном ----------------------
-- Канонический прогон — первый удачный с as_of_sprint = 0 (ADR-004);
-- именно на него смотрит BASELINE_STARTS_SQL в планировщике.
UNION ALL
SELECT b.run_id, 'BASELINE_MUTATED', 'error', b.task_id::text,
       ('planned_sp ' || b.planned_sp || ' против ' || c.planned_sp
        || ' в каноническом прогоне ' || c.run_id)::text
FROM plan_baseline b
JOIN plan_baseline c ON c.task_id = b.task_id AND c.run_id <> b.run_id
WHERE c.run_id = (SELECT MIN(run_id) FROM plan_runs WHERE as_of_sprint = 0 AND status = 'ok')
  AND (b.planned_sp <> c.planned_sp OR b.committed <> c.committed)

-- W. KPI посчитаны не полностью ------------------------------------------
UNION ALL
SELECT r.run_id, 'KPI_INCOMPLETE', 'error', ('прогон ' || r.run_id)::text,
       ('pi_predictability ' || k.pred || ' (нужно 1), say_do_ratio ' || k.say_do
        || ' (нужно ' || p.sprint_count || '), bus_factor ' || k.bf
        || ' (нужно 1), NULL-значений ' || k.nulls)::text
FROM plan_runs r
CROSS JOIN pi_periods p
CROSS JOIN LATERAL (
    SELECT COUNT(*) FILTER (WHERE kpi_code = 'pi_predictability') AS pred,
           COUNT(*) FILTER (WHERE kpi_code = 'say_do_ratio')      AS say_do,
           COUNT(*) FILTER (WHERE kpi_code = 'bus_factor')        AS bf,
           COUNT(*) FILTER (WHERE value IS NULL)                  AS nulls
    FROM kpi_snapshots WHERE run_id = r.run_id
) k
WHERE k.pred <> 1 OR k.say_do <> p.sprint_count OR k.bf <> 1 OR k.nulls > 0

-- X. Часы списаны с орбиты сверх её бюджета ------------------------------
-- Проверка B считает СУММУ всех орбит и потому не видит перекос:
-- при ставках 0.5 + 0.5 формально можно списать все 80 ЧЧ с одной орбиты.
UNION ALL
SELECT a.run_id, 'ORBIT_OVER_BUDGET', 'error',
       (a.engineer_id || ' / ' || a.home_team_id || ' / спринт ' || a.sprint_no)::text,
       ('назначено ' || SUM(a.hours) || ' ЧЧ, бюджет орбиты '
        || MAX(o.capacity_rate * p.fte_hours_per_sprint) || ' ЧЧ')::text
FROM plan_assignments a
JOIN engineer_orbits o ON o.engineer_id = a.engineer_id AND o.team_id = a.home_team_id
CROSS JOIN pi_periods p
GROUP BY a.run_id, a.engineer_id, a.home_team_id, a.sprint_no
HAVING SUM(a.hours) > MAX(o.capacity_rate * p.fte_hours_per_sprint)

-- Y. Назначение в закрытый спринт ----------------------------------------
-- Пересчёт на начало спринта k не имеет права планировать в 1..k-1.
UNION ALL
SELECT a.run_id, 'ASSIGNMENT_IN_CLOSED_SPRINT', 'error',
       (a.task_id || ' / спринт ' || a.sprint_no)::text,
       ('пересчёт на начало спринта ' || r.as_of_sprint
        || ': прошлые спринты закрыты')::text
FROM plan_assignments a
JOIN plan_runs r ON r.run_id = a.run_id
WHERE r.as_of_sprint > 0 AND a.sprint_no < r.as_of_sprint

-- Z. Слепок состояния неполный -------------------------------------------
UNION ALL
SELECT r.run_id, 'STATE_SNAPSHOT_INCOMPLETE', 'error', ('прогон ' || r.run_id)::text,
       ('строк task_state ' || st.n || ' из ' || t.n || ' задач, чужих as_of_sprint '
        || st.foreign)::text
FROM plan_runs r
CROSS JOIN LATERAL (
    SELECT COUNT(*) AS n,
           COUNT(*) FILTER (WHERE as_of_sprint <> r.as_of_sprint) AS foreign
    FROM task_state WHERE run_id = r.run_id
) st
CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM tasks) t
WHERE st.n <> t.n OR st.foreign > 0

-- AA. Есть переносы, но нет ни одного алерта -----------------------------
UNION ALL
SELECT r.run_id, 'ALERTS_MISSING', 'error', ('прогон ' || r.run_id)::text,
       'есть переносы, но ни одного алерта: заказчик не увидит риск'::text
FROM plan_runs r
WHERE EXISTS (SELECT 1 FROM plan_task_schedule s
              WHERE s.run_id = r.run_id AND s.decision <> 'in_quarter')
  AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.run_id = r.run_id)

-- AB. Прогноз выходит за даты исходного плана ----------------------------
-- WARNING, не ошибка: `tasks.planned_start/planned_end` — исторический план,
-- а не обязательство (ADR-016). Но сигнал полезен: 3 задачи из 7 в прогоне 2
-- заканчиваются позже своей исходной даты.
UNION ALL
SELECT s.run_id, 'PLANNED_END_OVERSAIL', 'warning', s.task_id::text,
       ('прогноз до ' || s.forecast_end_date || ', а в исходном плане конец '
        || t.planned_end)::text
FROM plan_task_schedule s
JOIN tasks t ON t.task_id = s.task_id
WHERE s.decision = 'in_quarter'
  AND t.planned_end IS NOT NULL
  AND s.forecast_end_date IS NOT NULL
  AND s.forecast_end_date > t.planned_end

-- AC. В окне задачи есть спринт без назначений ---------------------------
-- WARNING: разрывы разрешены (задача ждёт конкретную роль), но их надо видеть.
-- Задача вообще без назначений сюда не попадает — у неё свой код (I).
UNION ALL
SELECT s.run_id, 'WINDOW_HAS_GAP', 'warning', s.task_id::text,
       ('в окне ' || s.start_sprint || '..' || s.end_sprint
        || ' есть спринт без назначений: задача ждёт роль')::text
FROM plan_task_schedule s
WHERE s.decision = 'in_quarter'
  AND EXISTS (SELECT 1 FROM plan_assignments a
              WHERE a.run_id = s.run_id AND a.task_id = s.task_id)
  AND EXISTS (SELECT 1
              FROM generate_series(s.start_sprint::int, s.end_sprint::int) g
              WHERE NOT EXISTS (SELECT 1 FROM plan_assignments a
                                WHERE a.run_id = s.run_id AND a.task_id = s.task_id
                                  AND a.sprint_no = g));

COMMENT ON VIEW v_plan_violations IS
 'Приёмка плана: нет строк с severity = error. Строки severity = warning план не '
 'отменяют, но требуют отображения в UI. Правила — docs/PLANNER_SPEC.md, раздел 7; '
 'разбор ревью M2 — docs/REVIEW_RESPONSE.md.';

COMMIT;
