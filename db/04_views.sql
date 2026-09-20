-- =====================================================================
--  ВИТРИНЫ. Вьюхи, не таблицы: пересчитываются сами, поддерживать нечего.
--  Первые пять — для планировщика, остальные — для бэкенда и фронта.
-- =====================================================================
BEGIN;

DROP VIEW IF EXISTS v_dq_summary, v_orbit_map, v_task_board, v_bus_factor,
     v_role_deficit, v_backlog_demand, v_role_supply_hh, v_satellite_capacity,
     v_task_remaining_hh, v_team_capacity_sp CASCADE;

-- --------------------------------------------------------------------
--  Ёмкость ядра в SP. Velocity × Focus Factor (онбординг, раздел 3А).
-- --------------------------------------------------------------------
CREATE VIEW v_team_capacity_sp AS
SELECT t.team_id,
       COUNT(h.*)                                            AS history_points,
       ROUND(AVG(h.velocity_achieved), 2)                    AS avg_velocity,
       t.focus_factor,
       ROUND(AVG(h.velocity_achieved) * t.focus_factor, 2)   AS available_sp_per_sprint,
       ROUND(AVG(h.velocity_achieved) * t.focus_factor
             * (SELECT sprint_count FROM pi_periods LIMIT 1), 2) AS available_sp_per_pi
FROM teams t
LEFT JOIN team_history h ON h.team_id = t.team_id
GROUP BY t.team_id, t.focus_factor;
COMMENT ON VIEW v_team_capacity_sp IS
 'history_points = 2 на команду: среднее шаткое, на защите оговорить.';

-- --------------------------------------------------------------------
--  Остаток часов по задаче и роли. Для InProgress факт берётся ТОЛЬКО
--  из task_role_spent — колонка tasks.spent_time у них пуста.
-- --------------------------------------------------------------------
CREATE VIEW v_task_remaining_hh AS
SELECT COALESCE(e.task_id, s.task_id)                            AS task_id,
       COALESCE(e.role_id, s.role_id)                            AS role_id,
       COALESCE(e.hours, 0)                                      AS estimated_hours,
       COALESCE(s.hours, 0)                                      AS spent_hours,
       GREATEST(COALESCE(e.hours, 0) - COALESCE(s.hours, 0), 0)  AS remaining_hours
FROM task_role_estimates e
FULL OUTER JOIN task_role_spent s
  ON s.task_id = e.task_id AND s.role_id = e.role_id;

-- --------------------------------------------------------------------
--  Фонд часов спутника на орбите в спринте.
--  hours_own      — обязательство перед своим ядром;
--  hours_lendable — то же самое, но доступное другим ядрам как заём,
--                   если своё ядро часы не выбрало (ADR-001).
-- --------------------------------------------------------------------
CREATE VIEW v_satellite_capacity AS
SELECT o.engineer_id, o.team_id, e.role_id, e.grade,
       s.pi_id, s.sprint_no, s.start_date, s.end_date,
       o.capacity_rate,
       (SELECT COUNT(*) > 1 FROM engineer_orbits x WHERE x.engineer_id = o.engineer_id) AS is_shared_orbit,
       ROUND(o.capacity_rate * p.fte_hours_per_sprint, 2) AS hours_own
FROM engineer_orbits o
JOIN engineers  e ON e.engineer_id = o.engineer_id
JOIN sprints    s ON TRUE
JOIN pi_periods p ON p.pi_id = s.pi_id;

-- --------------------------------------------------------------------
--  Предложение часов по роли: в разрезе ядра и по всей компании.
-- --------------------------------------------------------------------
CREATE VIEW v_role_supply_hh AS
SELECT r.role_id, r.canonical_name AS role_name, o.team_id,
       COUNT(DISTINCT o.engineer_id)                                     AS engineers,
       SUM(o.capacity_rate)                                              AS fte,
       ROUND(SUM(o.capacity_rate * p.fte_hours_per_sprint), 2)           AS hh_per_sprint,
       ROUND(SUM(o.capacity_rate * p.fte_hours_per_sprint * p.sprint_count), 2) AS hh_per_pi
FROM roles r
JOIN engineers       e ON e.role_id = r.role_id
JOIN engineer_orbits o ON o.engineer_id = e.engineer_id
CROSS JOIN pi_periods p
GROUP BY r.role_id, r.canonical_name, o.team_id;

-- --------------------------------------------------------------------
--  Потребность живого бэклога по ядру и роли за квартал.
-- --------------------------------------------------------------------
CREATE VIEW v_backlog_demand AS
SELECT t.team_id, r.role_id, r.canonical_name AS role_name,
       COUNT(DISTINCT t.task_id)          AS tasks,
       ROUND(SUM(rm.remaining_hours), 2)  AS demand_hh
FROM v_task_remaining_hh rm
JOIN tasks t ON t.task_id = rm.task_id
JOIN roles r ON r.role_id = rm.role_id
WHERE t.status IN ('ToDo', 'InProgress')
  AND rm.remaining_hours > 0
GROUP BY t.team_id, r.role_id, r.canonical_name;

-- --------------------------------------------------------------------
--  ГЛАВНАЯ ВИТРИНА: дефицит по связке «ядро × роль».
--  Именно она показывает, что без займов (ADR-001) план нерешаем.
-- --------------------------------------------------------------------
CREATE VIEW v_role_deficit AS
SELECT COALESCE(d.team_id, s.team_id)       AS team_id,
       COALESCE(d.role_name, s.role_name)   AS role_name,
       COALESCE(d.demand_hh, 0)             AS demand_hh,
       COALESCE(s.hh_per_pi, 0)             AS supply_hh,
       COALESCE(d.demand_hh, 0) - COALESCE(s.hh_per_pi, 0) AS gap_hh,
       CASE WHEN COALESCE(s.hh_per_pi, 0) = 0 AND COALESCE(d.demand_hh, 0) > 0
                 THEN 'роли нет в команде'
            WHEN COALESCE(d.demand_hh, 0) > COALESCE(s.hh_per_pi, 0)
                 THEN 'не хватает часов'
            ELSE 'покрыто' END              AS verdict
FROM v_backlog_demand d
FULL OUTER JOIN v_role_supply_hh s
  ON s.team_id = d.team_id AND s.role_id = d.role_id;

-- --------------------------------------------------------------------
--  Bus Factor. Роли без единого инженера тоже попадают сюда —
--  это самая опасная категория, её нельзя терять во INNER JOIN.
-- --------------------------------------------------------------------
CREATE VIEW v_bus_factor AS
SELECT r.role_id, r.canonical_name AS role_name, r.role_group,
       COUNT(DISTINCT e.engineer_id)                       AS bus_factor,
       COALESCE(ROUND(MAX(d.demand_hh), 2), 0)            AS demand_hh,
       CASE WHEN COUNT(DISTINCT e.engineer_id) = 0 THEN 'НЕТ В ШТАТЕ'
            WHEN COUNT(DISTINCT e.engineer_id) = 1 THEN 'КРИТИЧНО (BF=1)'
            ELSE 'ок' END                                  AS risk
FROM roles r
LEFT JOIN engineers e ON e.role_id = r.role_id
LEFT JOIN (SELECT role_id, SUM(demand_hh) AS demand_hh
             FROM v_backlog_demand GROUP BY role_id) d ON d.role_id = r.role_id
GROUP BY r.role_id, r.canonical_name, r.role_group;

-- --------------------------------------------------------------------
--  Доска задач — денормализованная, чтобы бэкенд не джойнил руками.
-- --------------------------------------------------------------------
CREATE VIEW v_task_board AS
SELECT t.task_id, t.prodf_id, i.br_id, i.priority_rung, t.team_id, t.summary,
       t.status, t.rung, t.estimation_sp,
       t.estimated_hh_effective, t.estimated_hh_declared, t.estimated_hh_matrix_total,
       (t.estimated_hh_effective IS DISTINCT FROM t.estimated_hh_declared) AS estimate_disputed,
       t.planned_start, t.planned_end, t.actual_start, t.actual_end,
       q.topo_order, q.depth, q.earliest_start_sprint, q.on_critical_path,
       COALESCE(rm.remaining_hh, 0) AS remaining_hh,
       (SELECT COUNT(*) FROM task_dependencies d WHERE d.blocked_task_id  = t.task_id) AS blocked_by,
       (SELECT COUNT(*) FROM task_dependencies d WHERE d.blocking_task_id = t.task_id) AS blocks
FROM tasks t
JOIN initiatives i ON i.prodf_id = t.prodf_id
LEFT JOIN task_sequence q ON q.task_id = t.task_id
LEFT JOIN (SELECT task_id, SUM(remaining_hours) AS remaining_hh
             FROM v_task_remaining_hh GROUP BY task_id) rm ON rm.task_id = t.task_id;

-- --------------------------------------------------------------------
--  Звёздная карта — отдаётся фронту как есть, без трансформации.
-- --------------------------------------------------------------------
CREATE VIEW v_orbit_map AS
SELECT e.engineer_id, r.canonical_name AS role_name, r.role_group, e.grade,
       e.total_capacity_rate,
       (SELECT COUNT(*) FROM engineer_orbits x WHERE x.engineer_id = e.engineer_id) AS orbit_count,
       ARRAY(SELECT o.team_id FROM engineer_orbits o
              WHERE o.engineer_id = e.engineer_id ORDER BY o.team_id)                AS teams,
       ARRAY(SELECT s.name FROM engineer_skills es JOIN skills s ON s.skill_id = es.skill_id
              WHERE es.engineer_id = e.engineer_id ORDER BY s.name)                  AS skills,
       bf.bus_factor, bf.risk
FROM engineers e
JOIN roles r        ON r.role_id = e.role_id
JOIN v_bus_factor bf ON bf.role_id = e.role_id;

-- --------------------------------------------------------------------
CREATE VIEW v_dq_summary AS
SELECT rule_code, severity, COUNT(*) AS n,
       MIN(detail) AS example
FROM dq_issues GROUP BY rule_code, severity ORDER BY
     CASE severity WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, COUNT(*) DESC;

COMMIT;

-- =====================================================================
--  ЗАМЕЩЕНИЕ РОЛЕЙ (ADR-009).
--  Добавлено отдельным блоком: базовые витрины выше остаются «строгими»,
--  чтобы всегда можно было сравнить картину с замещением и без него.
-- =====================================================================
BEGIN;

DROP VIEW IF EXISTS v_plan_assignment_detail, v_role_deficit_effective,
     v_role_coverage_org, v_engineer_role_coverage CASCADE;

-- --------------------------------------------------------------------
--  ГЛАВНЫЙ ВХОД ДЛЯ ПЛАНИРОВЩИКА: какие роли может закрыть инженер.
--  Родная роль + разрешённые замещения. Строки с status='rejected'
--  не попадают. Бэкенду достаточно читать только эту вьюху.
-- --------------------------------------------------------------------
CREATE VIEW v_engineer_role_coverage AS
SELECT e.engineer_id, e.role_id, r.canonical_name AS role_name,
       TRUE  AS is_native, 1.00::numeric(3,2) AS efficiency,
       'основная роль'::text AS basis, 'confirmed'::text AS status
FROM engineers e JOIN roles r ON r.role_id = e.role_id
UNION ALL
SELECT e.engineer_id, s.required_role_id, r.canonical_name,
       FALSE, s.efficiency, s.rationale, s.status
FROM engineers e
JOIN role_substitutions s ON s.covering_role_id = e.role_id
JOIN roles r ON r.role_id = s.required_role_id
WHERE s.status <> 'rejected'
  AND CASE e.grade WHEN 'Senior' THEN 3 WHEN 'Middle' THEN 2 ELSE 1 END
   >= CASE s.min_grade WHEN 'Senior' THEN 3 WHEN 'Middle' THEN 2 ELSE 1 END;
COMMENT ON VIEW v_engineer_role_coverage IS
 'Кто какую роль может закрывать. is_native=false — замещение, показывать в UI явно. '
 'efficiency — множитель часов (сейчас везде 1.00). Планировщик ВПРАВЕ игнорировать неродные строки.';

-- --------------------------------------------------------------------
--  Дефицит с учётом замещения — рядом со строгим v_role_deficit.
-- --------------------------------------------------------------------
CREATE VIEW v_role_deficit_effective AS
WITH supply AS (
    SELECT c.role_id, o.team_id,
           SUM(o.capacity_rate * p.fte_hours_per_sprint * p.sprint_count) AS hh
    FROM v_engineer_role_coverage c
    JOIN engineer_orbits o ON o.engineer_id = c.engineer_id
    CROSS JOIN pi_periods p
    GROUP BY c.role_id, o.team_id
)
SELECT COALESCE(d.team_id, s.team_id)     AS team_id,
       COALESCE(d.role_name, r.canonical_name) AS role_name,
       COALESCE(d.demand_hh, 0)           AS demand_hh,
       COALESCE(s.hh, 0)                  AS supply_with_substitution_hh,
       COALESCE(d.demand_hh, 0) - COALESCE(s.hh, 0) AS gap_hh,
       CASE WHEN COALESCE(d.demand_hh, 0) <= COALESCE(s.hh, 0) THEN 'покрыто'
            WHEN COALESCE(s.hh, 0) = 0 THEN 'НЕ ЗАКРЫТЬ НИКЕМ — нужен наём'
            ELSE 'не хватает часов' END   AS verdict
FROM v_backlog_demand d
FULL OUTER JOIN supply s ON s.team_id = d.team_id AND s.role_id = d.role_id
LEFT JOIN roles r ON r.role_id = s.role_id;
COMMENT ON VIEW v_role_deficit_effective IS
 'Сравнивать с v_role_deficit (строгим). Разница между ними — ровно то, что даёт замещение.';

-- --------------------------------------------------------------------
--  ИТОГОВЫЙ СРЕЗ ПО КОМПАНИИ: что не закрыть НИКЕМ И НИГДЕ.
--  v_role_deficit_effective смотрит по командам и потому смешивает
--  «некому закрыть» с «человек в другой команде» — второе лечится
--  займами (ADR-001), а не наймом. Эта вьюха отвечает на вопрос найма.
-- --------------------------------------------------------------------
CREATE VIEW v_role_coverage_org AS
WITH demand AS (
    SELECT role_id, SUM(demand_hh) AS demand_hh FROM v_backlog_demand GROUP BY role_id
), supply AS (
    SELECT c.role_id,
           COUNT(DISTINCT c.engineer_id)                                   AS people,
           COUNT(DISTINCT c.engineer_id) FILTER (WHERE c.is_native)        AS native_people,
           SUM(e.total_capacity_rate * p.fte_hours_per_sprint * p.sprint_count) AS hh
    FROM v_engineer_role_coverage c
    JOIN engineers e ON e.engineer_id = c.engineer_id
    CROSS JOIN pi_periods p
    GROUP BY c.role_id
)
SELECT r.canonical_name                      AS role_name,
       COALESCE(d.demand_hh, 0)              AS demand_hh,
       COALESCE(s.native_people, 0)          AS native_people,
       COALESCE(s.people, 0)                 AS people_incl_substitution,
       COALESCE(s.hh, 0)                     AS supply_hh,
       COALESCE(d.demand_hh, 0) - COALESCE(s.hh, 0) AS gap_hh,
       CASE WHEN COALESCE(d.demand_hh, 0) = 0                  THEN 'спроса нет'
            WHEN COALESCE(s.hh, 0) = 0                         THEN 'НАЙМ: закрыть некем'
            WHEN COALESCE(d.demand_hh, 0) > COALESCE(s.hh, 0)  THEN 'НАЙМ: не хватает часов'
            WHEN COALESCE(s.native_people, 0) = 0              THEN 'только замещением'
            ELSE 'покрыто' END               AS verdict
FROM roles r
LEFT JOIN demand d ON d.role_id = r.role_id
LEFT JOIN supply s ON s.role_id = r.role_id;
COMMENT ON VIEW v_role_coverage_org IS
 'Срез по всей компании: где нужен НАЙМ, а где хватит займов между командами. '
 'verdict=«только замещением» — роль держится исключительно на неродных исполнителях, '
 'это риск, показывать в UI.';

-- --------------------------------------------------------------------
--  Назначения с пометкой замещения. Контракт plan_assignments не
--  трогаем — признак выводится джойном, бэкенду писать ничего лишнего.
-- --------------------------------------------------------------------
CREATE VIEW v_plan_assignment_detail AS
SELECT a.run_id, a.task_id, a.sprint_no, a.engineer_id, a.hours,
       a.home_team_id, a.serving_team_id, a.is_loan,
       rq.canonical_name AS served_role,
       rn.canonical_name AS native_role,
       (a.role_id <> e.role_id) AS is_substitution,
       e.grade
FROM plan_assignments a
JOIN engineers e ON e.engineer_id = a.engineer_id
JOIN roles rq ON rq.role_id = a.role_id
JOIN roles rn ON rn.role_id = e.role_id;
COMMENT ON VIEW v_plan_assignment_detail IS
 'is_substitution — инженер работает не по своей роли. Обязательно показывать в UI: '
 '«всё спланировалось» без ответа «кем» на защите не проходит.';

COMMIT;
