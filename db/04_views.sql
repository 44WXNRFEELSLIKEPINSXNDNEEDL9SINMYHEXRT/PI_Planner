-- =====================================================================
--  ВИТРИНЫ. Вьюхи, не таблицы: пересчитываются сами, поддерживать нечего.
--  Первые пять — для планировщика, остальные — для бэкенда и фронта.
-- =====================================================================
BEGIN;

DROP VIEW IF EXISTS v_dq_summary, v_orbit_map, v_task_board, v_bus_factor,
     v_role_deficit, v_backlog_demand, v_role_supply_hh, v_satellite_capacity,
     v_task_remaining_hh, v_team_capacity_sp CASCADE;

DROP VIEW IF EXISTS v_sprint_fund_factor, v_pi_fund_factor CASCADE;

-- --------------------------------------------------------------------
--  МНОЖИТЕЛЬ ФОНДА. Единственный источник ответа «сколько ЧЧ даёт
--  ставка в этом спринте / за весь PI». Полный спринт — 1.0000,
--  короткий — length_days / 14 (ADR-017). Сейчас все шесть полные (ADR-025).
--  Дублировать эту арифметику по пяти вьюхам нельзя: разъедется, и
--  короткий спринт молча получит полный фонд.
-- --------------------------------------------------------------------
CREATE VIEW v_sprint_fund_factor AS
SELECT s.pi_id, s.sprint_no, s.start_date, s.end_date, s.length_days,
       ROUND(s.length_days::numeric / p.sprint_length_days, 4) AS factor
FROM sprints s
JOIN pi_periods p ON p.pi_id = s.pi_id;
COMMENT ON VIEW v_sprint_fund_factor IS
 'Фонд спринта = rate × fte_hours_per_sprint × factor. Короткий спринт даёт МЕНЬШЕ часов, '
 'а не «те же 80»: иначе фонд квартала вылез бы за дни календаря. Проверки ENGINEER_OVERLOAD '
 'и ORBIT_OVERLOAD берут фонд именно отсюда.';

CREATE VIEW v_pi_fund_factor AS
SELECT p.pi_id,
       p.sprint_length_days,
       SUM(s.length_days)                                           AS days_total,
       ROUND(SUM(s.length_days)::numeric / p.sprint_length_days, 4) AS factor
FROM pi_periods p
JOIN sprints s ON s.pi_id = p.pi_id
GROUP BY p.pi_id, p.sprint_length_days;
COMMENT ON VIEW v_pi_fund_factor IS
 'Фонд ставки за весь PI, выраженный в «полных спринтах»: 1.0000 ставки × 80 ЧЧ × factor. '
 'На Q3-2026: 84 дня / 14 = 6.0000, то есть 480 ЧЧ за квартал (ADR-025). '
 'Используется вместо `sprint_count` везде, где считается фонд за квартал.';

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
             * (SELECT factor FROM v_pi_fund_factor LIMIT 1), 2) AS available_sp_per_pi
FROM teams t
LEFT JOIN team_history h ON h.team_id = t.team_id
GROUP BY t.team_id, t.focus_factor;
COMMENT ON VIEW v_team_capacity_sp IS
 'history_points = 2 на команду: среднее шаткое, на защите оговорить. '
 'available_sp_per_sprint — фонд ОДНОГО ПОЛНОГО спринта; для короткого умножать на '
 'v_sprint_fund_factor.factor (так делает проверка SP_OVERFLOW).';

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
       s.length_days,
       ROUND(o.capacity_rate * p.fte_hours_per_sprint * f.factor, 2) AS hours_own
FROM engineer_orbits o
JOIN engineers  e ON e.engineer_id = o.engineer_id
JOIN sprints    s ON TRUE
JOIN pi_periods p ON p.pi_id = s.pi_id
JOIN v_sprint_fund_factor f ON f.pi_id = s.pi_id AND f.sprint_no = s.sprint_no;
COMMENT ON VIEW v_satellite_capacity IS
 'hours_own — фонд спутника на орбите в КОНКРЕТНОМ спринте: rate × 80 × factor спринта. '
 'В коротком спринте это length_days / 14 от обычного.';

-- --------------------------------------------------------------------
--  Предложение часов по роли: в разрезе ядра и по всей компании.
-- --------------------------------------------------------------------
CREATE VIEW v_role_supply_hh AS
SELECT r.role_id, r.canonical_name AS role_name, o.team_id,
       COUNT(DISTINCT o.engineer_id)                                     AS engineers,
       SUM(o.capacity_rate)                                              AS fte,
       ROUND(SUM(o.capacity_rate * p.fte_hours_per_sprint), 2)           AS hh_per_sprint,
       ROUND(SUM(o.capacity_rate * p.fte_hours_per_sprint)
             * (SELECT factor FROM v_pi_fund_factor LIMIT 1), 2)         AS hh_per_pi
FROM roles r
JOIN engineers       e ON e.role_id = r.role_id
JOIN engineer_orbits o ON o.engineer_id = e.engineer_id
CROSS JOIN pi_periods p
GROUP BY r.role_id, r.canonical_name, o.team_id;
COMMENT ON VIEW v_role_supply_hh IS
 'hh_per_sprint — фонд одного ПОЛНОГО спринта. hh_per_pi — фонд всего квартала: '
 '× v_pi_fund_factor.factor (84/14 = 6.0000), а НЕ × sprint_count, иначе короткий '
 'спринт подарил бы команде лишние дни фонда.';

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
COMMENT ON VIEW v_bus_factor IS
 'ПОКРЫТИЕ РОЛЕЙ: сколько инженеров на роль, включая роли без людей в штате (найм). '
 'Bus Factor по компетенциям, которого требует ТЗ, — v_bus_factor_skill (ADR-024).';

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
           SUM(o.capacity_rate * p.fte_hours_per_sprint)
           * (SELECT factor FROM v_pi_fund_factor LIMIT 1) AS hh
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
           SUM(e.total_capacity_rate * p.fte_hours_per_sprint)
           * (SELECT factor FROM v_pi_fund_factor LIMIT 1) AS hh
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

-- =====================================================================
--  ЗВЁЗДНАЯ КАРТА ПО ТЗ, ПРОФИЛИ КОМАНД, ПЕРЕСЧЁТ ПО ФАКТУ (ADR-021, 024)
--
--  ТЗ: «рассчитывать Bus Factor ПО КОМПЕТЕНЦИЯМ и выделять компетенции,
--  которыми владеет только один специалист… показать, где отсутствие одного
--  сотрудника создаёт риск»; «строить профили инженеров и команд»;
--  «пользователь должен понимать, какие отклонения вызвали изменения».
-- =====================================================================
BEGIN;

DROP VIEW IF EXISTS v_sprint_deviation, v_plan_diff, v_team_profile,
     v_engineer_absence_risk, v_bus_factor_skill CASCADE;

-- --------------------------------------------------------------------
--  Bus Factor по компетенциям: сколько инженеров заявили навык.
--  critical = навык есть у кого-то, чья роль нужна живому бэклогу —
--  связи «задача → навык» в датасете нет, поэтому критичность выводится
--  через роль носителя (ADR-024).
-- --------------------------------------------------------------------
CREATE VIEW v_bus_factor_skill AS
WITH holders AS (
    SELECT es.skill_id, e.engineer_id, e.role_id,
           (SELECT COUNT(*) FROM engineers x WHERE x.role_id = e.role_id) AS role_n
    FROM engineer_skills es JOIN engineers e ON e.engineer_id = es.engineer_id
), role_demand AS (
    SELECT role_id, SUM(demand_hh) AS demand_hh FROM v_backlog_demand GROUP BY role_id
)
SELECT s.skill_id,
       s.name                                                   AS skill_name,
       COUNT(DISTINCT h.engineer_id)::int                       AS bus_factor,
       ARRAY_AGG(DISTINCT h.engineer_id ORDER BY h.engineer_id) AS engineers,
       ARRAY(SELECT DISTINCT o.team_id FROM engineer_orbits o
              JOIN holders x ON x.engineer_id = o.engineer_id
              WHERE x.skill_id = s.skill_id ORDER BY 1)         AS teams,
       ARRAY(SELECT DISTINCT r.canonical_name FROM holders x
              JOIN roles r ON r.role_id = x.role_id
              WHERE x.skill_id = s.skill_id ORDER BY 1)         AS roles,
       COALESCE((SELECT SUM(d.demand_hh) FROM role_demand d
                  WHERE d.role_id IN (SELECT x.role_id FROM holders x
                                       WHERE x.skill_id = s.skill_id)), 0) AS roles_demand_hh,
       COALESCE(BOOL_OR(rd.demand_hh > 0), FALSE)               AS in_demand,
       (COUNT(DISTINCT h.engineer_id) = 1 AND MAX(h.role_n) = 1) AS sole_in_role,
       CASE
         WHEN COUNT(DISTINCT h.engineer_id) = 1 AND MAX(h.role_n) = 1
              THEN 'критично: работу не подхватит никто'
         WHEN COUNT(DISTINCT h.engineer_id) = 1 THEN 'единственный носитель'
         WHEN COUNT(DISTINCT h.engineer_id) = 2 THEN 'два носителя'
         ELSE 'ок' END                                          AS risk
FROM skills s
JOIN holders h ON h.skill_id = s.skill_id
LEFT JOIN role_demand rd ON rd.role_id = h.role_id
GROUP BY s.skill_id, s.name;
COMMENT ON VIEW v_bus_factor_skill IS
 'Bus Factor по компетенциям (ТЗ): число инженеров, заявивших навык. Градация риска: '
 '«критично» — единственный носитель, который ещё и единственный специалист своей роли '
 '(выпал — работу не подхватит никто, замещения ролей запрещены); «единственный носитель» — '
 'навык у одного, но роль есть у других. in_demand — роль носителя нужна живому бэклогу. '
 'Покрытие ролей (роли без людей в штате) — отдельно: v_bus_factor, v_role_coverage_org.';

-- --------------------------------------------------------------------
--  Где отсутствие одного сотрудника создаёт риск (по прогону).
--  tasks_without_backup — задачи прогона, которые инженер закрывает, а
--  второго человека с такой ролью в компании нет: он выпал — они встали.
-- --------------------------------------------------------------------
CREATE VIEW v_engineer_absence_risk AS
WITH role_n AS (
    SELECT role_id, COUNT(*)::int AS n FROM engineers GROUP BY role_id
), uniq AS (
    SELECT UNNEST(engineers) AS engineer_id, skill_name, sole_in_role AS critical
    FROM v_bus_factor_skill WHERE bus_factor = 1
), asg AS (
    SELECT run_id, engineer_id, task_id, SUM(hours) AS hours
    FROM plan_assignments GROUP BY run_id, engineer_id, task_id
)
SELECT r.run_id, e.engineer_id, ro.canonical_name AS role_name, e.grade,
       e.total_capacity_rate,
       ARRAY(SELECT o.team_id FROM engineer_orbits o
              WHERE o.engineer_id = e.engineer_id ORDER BY 1)            AS teams,
       rn.n                                                              AS role_bus_factor,
       ARRAY(SELECT u.skill_name FROM uniq u
              WHERE u.engineer_id = e.engineer_id ORDER BY 1)            AS unique_skills,
       ARRAY(SELECT u.skill_name FROM uniq u
              WHERE u.engineer_id = e.engineer_id AND u.critical ORDER BY 1) AS unique_critical_skills,
       COALESCE((SELECT SUM(a.hours) FROM asg a
                  WHERE a.run_id = r.run_id AND a.engineer_id = e.engineer_id), 0) AS planned_hours,
       ARRAY(SELECT a.task_id FROM asg a
              WHERE a.run_id = r.run_id AND a.engineer_id = e.engineer_id ORDER BY 1) AS planned_tasks,
       CASE WHEN rn.n = 1 THEN ARRAY(SELECT a.task_id FROM asg a
              WHERE a.run_id = r.run_id AND a.engineer_id = e.engineer_id ORDER BY 1)
            ELSE '{}'::text[] END                                        AS tasks_without_backup,
       CASE WHEN rn.n = 1 THEN COALESCE((SELECT SUM(a.hours) FROM asg a
              WHERE a.run_id = r.run_id AND a.engineer_id = e.engineer_id), 0)
            ELSE 0 END                                                   AS hours_without_backup,
       CASE WHEN rn.n = 1 AND EXISTS (SELECT 1 FROM asg a
                   WHERE a.run_id = r.run_id AND a.engineer_id = e.engineer_id)
                 THEN 'критично: работы встанут'
            WHEN rn.n = 1 THEN 'единственный по роли'
            WHEN EXISTS (SELECT 1 FROM uniq u WHERE u.engineer_id = e.engineer_id AND u.critical)
                 THEN 'единственный носитель компетенций'
            ELSE 'ок' END                                                AS risk
FROM plan_runs r
CROSS JOIN engineers e
JOIN roles ro  ON ro.role_id = e.role_id
JOIN role_n rn ON rn.role_id = e.role_id;
COMMENT ON VIEW v_engineer_absence_risk IS
 'Профиль инженера + «что будет, если он выпадет» в данном прогоне. Замещения ролей '
 'запрещены организаторами (ADR-010), поэтому замена = другой инженер той же роли.';

-- --------------------------------------------------------------------
--  Профиль команды: состав, роли, дыры, компетенции, ёмкость, бэклог.
-- --------------------------------------------------------------------
CREATE VIEW v_team_profile AS
WITH mem AS (
    SELECT o.team_id, o.engineer_id, o.capacity_rate, e.role_id
    FROM engineer_orbits o JOIN engineers e ON e.engineer_id = o.engineer_id
), fte_h AS (SELECT fte_hours_per_sprint AS h FROM pi_periods LIMIT 1)
SELECT tm.team_id,
       (SELECT COUNT(*) FROM mem m WHERE m.team_id = tm.team_id)::int                 AS members,
       (SELECT COUNT(*) FROM mem m WHERE m.team_id = tm.team_id
                                     AND m.capacity_rate < 1)::int                     AS part_time_members,
       (SELECT COALESCE(SUM(m.capacity_rate), 0) FROM mem m WHERE m.team_id = tm.team_id) AS fte,
       (SELECT COALESCE(SUM(m.capacity_rate), 0) FROM mem m WHERE m.team_id = tm.team_id)
         * (SELECT h FROM fte_h)                                                       AS hours_per_sprint,
       c.avg_velocity, c.available_sp_per_sprint, c.available_sp_per_pi,
       ARRAY(SELECT DISTINCT r.canonical_name FROM mem m JOIN roles r ON r.role_id = m.role_id
              WHERE m.team_id = tm.team_id ORDER BY 1)                                 AS roles_present,
       ARRAY(SELECT d.role_name FROM v_backlog_demand d
              WHERE d.team_id = tm.team_id
                AND NOT EXISTS (SELECT 1 FROM mem m
                                 WHERE m.team_id = d.team_id AND m.role_id = d.role_id)
              ORDER BY 1)                                                              AS roles_missing,
       (SELECT COUNT(DISTINCT es.skill_id) FROM mem m
          JOIN engineer_skills es ON es.engineer_id = m.engineer_id
         WHERE m.team_id = tm.team_id)::int                                            AS skills_n,
       ARRAY(SELECT DISTINCT b.skill_name FROM v_bus_factor_skill b
              JOIN mem m ON m.engineer_id = ANY (b.engineers)
              WHERE m.team_id = tm.team_id AND b.bus_factor = 1 ORDER BY 1)            AS unique_skills,
       (SELECT COUNT(*) FROM tasks t WHERE t.team_id = tm.team_id
                                       AND t.status IN ('ToDo','InProgress'))::int     AS live_tasks,
       (SELECT COALESCE(SUM(t.estimation_sp), 0) FROM tasks t
         WHERE t.team_id = tm.team_id AND t.status IN ('ToDo','InProgress'))           AS live_sp,
       (SELECT COALESCE(SUM(d.demand_hh), 0) FROM v_backlog_demand d
         WHERE d.team_id = tm.team_id)                                                 AS live_hh
FROM teams tm
LEFT JOIN v_team_capacity_sp c ON c.team_id = tm.team_id;
COMMENT ON VIEW v_team_profile IS
 'Профиль команды для звёздной карты. roles_missing — роли, которые нужны бэклогу команды, '
 'но в ней нет ни одного инженера (закрываются займом или наймом).';

-- --------------------------------------------------------------------
--  Что изменилось между прогоном и предыдущим — и почему.
--  cause: completed | own_slip (сама не закрылась к отчётному спринту) |
--  dependency (сдвинулась блокирующая) | capacity (ресурсы перераспределены)
-- --------------------------------------------------------------------
CREATE VIEW v_plan_diff AS
WITH pairs AS (
    SELECT r.run_id, r.as_of_sprint, r.actuals_upload_id,
           (SELECT u.sprint_no FROM actual_uploads u
             WHERE u.upload_id = r.actuals_upload_id)                         AS reported_sprint,
           (SELECT MAX(p.run_id) FROM plan_runs p
             WHERE p.run_id < r.run_id AND p.status = 'ok')                   AS prev_run_id
    FROM plan_runs r
), base AS (
    SELECT pr.run_id, pr.prev_run_id, pr.reported_sprint, t.task_id, t.prodf_id, t.team_id,
           p.decision AS prev_decision, p.start_sprint AS prev_start, p.end_sprint AS prev_end,
           c.decision AS new_decision,  c.start_sprint AS new_start,  c.end_sprint AS new_end,
           ts.status  AS status_at_run,
           CASE
             WHEN c.task_id IS NULL AND ts.status = 'Done'                     THEN 'completed'
             WHEN p.decision = 'in_quarter' AND c.decision <> 'in_quarter'     THEN 'newly_deferred'
             WHEN p.decision <> 'in_quarter' AND c.decision = 'in_quarter'     THEN 'newly_planned'
             WHEN p.decision IS NULL AND c.decision IS NOT NULL                THEN 'newly_planned'
             WHEN c.decision = 'in_quarter' AND c.end_sprint > p.end_sprint    THEN 'shifted_later'
             WHEN c.decision = 'in_quarter' AND c.end_sprint < p.end_sprint    THEN 'shifted_earlier'
             ELSE 'unchanged' END AS change_type
    FROM pairs pr
    JOIN tasks t ON TRUE
    LEFT JOIN plan_task_schedule p ON p.run_id = pr.prev_run_id AND p.task_id = t.task_id
    LEFT JOIN plan_task_schedule c ON c.run_id = pr.run_id      AND c.task_id = t.task_id
    LEFT JOIN task_state ts        ON ts.run_id = pr.run_id     AND ts.task_id = t.task_id
    WHERE pr.prev_run_id IS NOT NULL
      AND (p.task_id IS NOT NULL OR c.task_id IS NOT NULL)
)
SELECT b.*,
       CASE
         WHEN b.change_type = 'completed' THEN 'completed'
         WHEN b.change_type IN ('unchanged','shifted_earlier','newly_planned') THEN NULL
         WHEN b.reported_sprint IS NOT NULL AND b.prev_decision = 'in_quarter'
              AND b.prev_end <= b.reported_sprint AND b.status_at_run <> 'Done' THEN 'own_slip'
         -- задача шла через закрытый спринт и по плану должна была продолжаться:
         -- это не отклонение, а перенос остатка на следующий спринт
         WHEN b.reported_sprint IS NOT NULL AND b.prev_decision = 'in_quarter'
              AND b.prev_start <= b.reported_sprint AND b.prev_end > b.reported_sprint
              THEN 'carry_over'
         WHEN EXISTS (SELECT 1 FROM task_dependencies d JOIN base bb
                        ON bb.run_id = b.run_id AND bb.task_id = d.blocking_task_id
                       WHERE d.blocked_task_id = b.task_id
                         AND bb.change_type IN ('shifted_later','newly_deferred')) THEN 'dependency'
         ELSE 'capacity' END AS cause,
       CASE
         WHEN b.change_type = 'completed' THEN 'выполнена по факту'
         WHEN b.change_type = 'unchanged' THEN NULL
         WHEN b.change_type = 'newly_planned'   THEN 'освободился ресурс — задача вошла в квартал'
         WHEN b.change_type = 'shifted_earlier' THEN 'сдвинулась раньше: освободились часы или ёмкость'
         WHEN b.reported_sprint IS NOT NULL AND b.prev_decision = 'in_quarter'
              AND b.prev_end <= b.reported_sprint AND b.status_at_run <> 'Done'
              THEN 'не закрыта к концу спринта ' || b.reported_sprint || ' — остаток переносится дальше'
         WHEN b.reported_sprint IS NOT NULL AND b.prev_decision = 'in_quarter'
              AND b.prev_start <= b.reported_sprint AND b.prev_end > b.reported_sprint
              THEN 'работа продолжается: спринт ' || b.reported_sprint
                   || ' закрыт, остаток запланирован дальше'
         WHEN EXISTS (SELECT 1 FROM task_dependencies d JOIN base bb
                        ON bb.run_id = b.run_id AND bb.task_id = d.blocking_task_id
                       WHERE d.blocked_task_id = b.task_id
                         AND bb.change_type IN ('shifted_later','newly_deferred'))
              THEN 'сдвинулась блокирующая задача ' || (SELECT string_agg(d.blocking_task_id, ', ')
                     FROM task_dependencies d WHERE d.blocked_task_id = b.task_id)
         ELSE 'ёмкость и часы заняты задачами, перешедшими из закрытых спринтов' END AS explanation
FROM base b;
COMMENT ON VIEW v_plan_diff IS
 'Сравнение прогона с предыдущим: что изменилось (change_type) и почему (cause, explanation). '
 'Ответ на требование ТЗ «какие отклонения вызвали изменения».';

-- --------------------------------------------------------------------
--  Отклонения факта спринта от плана, действовавшего в этом спринте.
-- --------------------------------------------------------------------
CREATE VIEW v_sprint_deviation AS
WITH plan_in_force AS (
    SELECT u.upload_id, u.sprint_no,
           (SELECT MAX(r.run_id) FROM plan_runs r
             WHERE r.status = 'ok' AND r.as_of_sprint <= u.sprint_no
               AND (r.actuals_upload_id IS NULL OR r.actuals_upload_id IN
                    (SELECT x.upload_id FROM actual_uploads x WHERE x.sprint_no < u.sprint_no))
           ) AS run_id
    FROM actual_uploads u
)
SELECT f.upload_id, f.sprint_no, f.run_id AS plan_run_id, s.task_id, t.team_id, t.estimation_sp,
       s.start_sprint AS planned_start, s.end_sprint AS planned_end,
       a.status       AS reported_status,
       COALESCE((SELECT SUM(x.hours) FROM plan_assignments x
                  WHERE x.run_id = f.run_id AND x.task_id = s.task_id
                    AND x.sprint_no = f.sprint_no), 0)                    AS planned_hours,
       COALESCE((SELECT SUM(x.hours) FROM task_actual_spent x
                  WHERE x.upload_id = f.upload_id AND x.task_id = s.task_id), 0) AS spent_hours,
       CASE
         WHEN a.status = 'Done' AND s.end_sprint =  f.sprint_no THEN 'в срок'
         WHEN a.status = 'Done' AND (s.end_sprint > f.sprint_no OR s.decision <> 'in_quarter')
              THEN 'раньше плана'
         WHEN s.decision = 'in_quarter' AND s.end_sprint <= f.sprint_no
              AND COALESCE(a.status, t.status) <> 'Done' THEN 'не закрыта в срок'
         WHEN s.decision = 'in_quarter' AND s.start_sprint <= f.sprint_no
              AND a.task_id IS NULL THEN 'нет данных по задаче'
         ELSE 'по плану' END                                              AS deviation
FROM plan_in_force f
JOIN plan_task_schedule s ON s.run_id = f.run_id
JOIN tasks t ON t.task_id = s.task_id
LEFT JOIN task_actuals a ON a.upload_id = f.upload_id AND a.task_id = s.task_id
WHERE s.decision = 'in_quarter' AND s.start_sprint <= f.sprint_no
   OR a.task_id IS NOT NULL;
COMMENT ON VIEW v_sprint_deviation IS
 'По каждой загрузке факта: задачи, которые план держал в этом спринте, и что с ними на деле. '
 '«не закрыта в срок» — источник жёлтых и красных алертов следующего пересчёта.';

COMMIT;
