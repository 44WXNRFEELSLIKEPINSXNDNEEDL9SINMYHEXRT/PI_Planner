-- =====================================================================
--  Приёмка M0 — проверки слоя данных ДС после заливки.
--  Запуск:
--    psql -h 127.0.0.1 -U postgres -d pi_planner -v ON_ERROR_STOP=1 -f tools/acceptance.sql
--  Ожидаемые значения и что делать при расхождении — docs/RUNBOOK.md, раздел 7.
--  Файл держим в UTF-8 (без BOM): в проверках есть кириллица.
--
--  Числа в комментариях — эталон для ETL 1.1.0 и календаря Q3-2026
--  (01.07..22.09, 6 спринтов по 14 дней, ADR-025). Смена календаря меняет 0а/0б,
--  3/4/5 не меняет: спрос считается из датасета, а не из спринтов.
-- =====================================================================

\echo '=== 0. Кодировка базы: ожидаем UTF8 ==='
SELECT pg_encoding_to_char(encoding) AS encoding
FROM pg_database WHERE datname = current_database();

\echo '=== 0а. Календарь PI: ожидаем 01.07..22.09.2026, 84 дня, 6 спринтов ==='
-- Фонд ставки за квартал = fte_hours_per_sprint × factor: 80 × 6.0000 = 480 ЧЧ (ADR-025).
SELECT p.pi_id, p.start_date, p.end_date, p.sprint_count,
       (SELECT SUM(length_days) FROM sprints s WHERE s.pi_id = p.pi_id) AS pi_days,
       f.factor                                                        AS fund_factor,
       ROUND(p.fte_hours_per_sprint * f.factor, 2)                     AS fund_hh_per_fte
FROM pi_periods p
JOIN v_pi_fund_factor f ON f.pi_id = p.pi_id;

\echo '--- 0б. Фонд по спринтам: все шесть полные (1.0000) ---'
SELECT sprint_no, start_date, end_date, length_days, factor
FROM v_sprint_fund_factor ORDER BY sprint_no;

\echo '=== 1. Счётчики последней загрузки (load_batches.row_counts) ==='
SELECT jsonb_pretty(row_counts)
FROM load_batches ORDER BY batch_id DESC LIMIT 1;

\echo '=== 1а. Живые счётчики по таблицам ==='
SELECT 'roles' AS tbl, COUNT(*) AS n FROM roles
UNION ALL SELECT 'tasks',                COUNT(*) FROM tasks
UNION ALL SELECT 'task_role_estimates',  COUNT(*) FROM task_role_estimates
UNION ALL SELECT 'task_dependencies',    COUNT(*) FROM task_dependencies
UNION ALL SELECT 'engineers',            COUNT(*) FROM engineers
UNION ALL SELECT 'engineer_orbits',      COUNT(*) FROM engineer_orbits
UNION ALL SELECT 'team_history',         COUNT(*) FROM team_history
UNION ALL SELECT 'dq_issues',            COUNT(*) FROM dq_issues
ORDER BY tbl;

\echo '=== 1б. Объекты схемы: ожидаем 29 таблиц + 17 вьюх ==='
-- 17 вьюх = 15 прежних + v_pi_fund_factor и v_sprint_fund_factor (ADR-017).
SELECT
  (SELECT COUNT(*) FROM information_schema.tables
    WHERE table_schema='public' AND table_type='BASE TABLE') AS tables,
  (SELECT COUNT(*) FROM information_schema.views
    WHERE table_schema='public')                            AS views;

\echo '=== 2. Качество данных: ожидаем 38 находок, 0 блокирующих ==='
SELECT COUNT(*)                                   AS findings,
       COUNT(*) FILTER (WHERE severity='error')   AS blocking,
       COUNT(*) FILTER (WHERE severity='warning') AS warnings,
       COUNT(*) FILTER (WHERE severity='info')    AS info
FROM dq_issues;

\echo '=== 3. Дефицит часов по связкам «команда × роль»: ожидаем ~2371 ЧЧ на 47 связках ==='
SELECT COUNT(*)              AS pairs,
       ROUND(SUM(gap_hh), 2) AS gap_hh
FROM v_role_deficit WHERE gap_hh > 0;

\echo '--- 3а. Природа дефицита: все 47 должны быть «роли нет в команде» ---'
SELECT verdict, COUNT(*) AS pairs, ROUND(SUM(gap_hh), 2) AS gap_hh
FROM v_role_deficit WHERE gap_hh > 0
GROUP BY verdict ORDER BY gap_hh DESC;

\echo '=== 4. Наём по компании: ожидаем 6 ролей и 665 ЧЧ (замещения отклонены, ADR-010) ==='
SELECT role_name,
       ROUND(demand_hh, 2) AS demand_hh,
       ROUND(supply_hh, 2) AS supply_hh,
       ROUND(gap_hh, 2)    AS gap_hh,
       verdict
FROM v_role_coverage_org
WHERE verdict LIKE 'НАЙМ%'
ORDER BY gap_hh DESC;

SELECT COUNT(*)              AS hiring_roles,
       ROUND(SUM(gap_hh), 2) AS hiring_hh
FROM v_role_coverage_org WHERE verdict LIKE 'НАЙМ%';

\echo '--- 4а. Роли вне штата: те же 6 ролей и 665 ЧЧ, закрывать нечем ---'
SELECT COUNT(*) FILTER (WHERE bus_factor = 0)                          AS roles_not_in_staff,
       ROUND(SUM(demand_hh) FILTER (WHERE bus_factor = 0), 2)          AS demand_no_staff_hh
FROM v_bus_factor WHERE demand_hh > 0;

\echo '--- 4б. Строгий режим: замещений нет, покрытие только нативными ролями ---'
SELECT (SELECT COUNT(*) FROM role_substitutions WHERE status <> 'rejected')  AS active_substitutions,
       (SELECT COUNT(*) FROM v_engineer_role_coverage)                       AS coverage_rows,
       (SELECT COUNT(*) FROM v_engineer_role_coverage WHERE NOT is_native)   AS substitution_rows;

\echo '=== 5. Bus Factor: ожидаем 8 ролей с BF=1 и спрос 1429 ЧЧ ==='
SELECT COUNT(*) FILTER (WHERE bus_factor = 1)                          AS bf1_roles,
       ROUND(SUM(demand_hh) FILTER (WHERE bus_factor = 1), 2)          AS bf1_demand_hh
FROM v_bus_factor WHERE demand_hh > 0;

\echo '--- 5а. Кириллица доехала: вердикт должен читаться, не «????» ---'
SELECT verdict FROM v_role_coverage_org WHERE verdict LIKE 'НАЙМ%' LIMIT 1;

\echo '=== 6. Детерминизм сида: sha256 исходного xlsx ==='
-- Сверить с sha256 в шапке build/seed.sql и с результатом свежего
-- `uv run python etl/load.py` (файл не должен измениться).
SELECT source_file, source_sha256, etl_version, pi_start, loaded_at
FROM load_batches ORDER BY batch_id DESC LIMIT 1;

\echo '=== 6а. Приёмка плана: ошибок нет ни в одном прогоне (warning допустим) ==='
SELECT (SELECT COUNT(*) FROM v_plan_violations WHERE severity = 'error')   AS errors,
       (SELECT COUNT(*) FROM v_plan_violations WHERE severity = 'warning') AS warnings,
       (SELECT COUNT(*) FROM plan_runs)                                   AS runs;

\echo '=== 7. Ёмкость в SP: перегруженных команд нет, Team-Platform загружена на 95% ==='
SELECT team_id,
       ROUND(avg_velocity, 2)           AS avg_velocity,
       focus_factor,
       ROUND(available_sp_per_sprint, 2) AS sp_per_sprint
FROM v_team_capacity_sp
ORDER BY sp_per_sprint DESC;

\echo '--- 7а. Ёмкость ядра за квартал: × 6.0000 (84/14) ---'
SELECT team_id,
       ROUND(available_sp_per_sprint, 2) AS sp_per_sprint,
       ROUND(available_sp_per_pi, 2)     AS sp_per_pi
FROM v_team_capacity_sp
ORDER BY sp_per_sprint DESC;

\echo '--- 7б. Фонд часов по спринтам: у полных спринтов фонд одинаковый ---'
-- Ровно то, что видит планировщик: 80 ЧЧ × ставка × factor спринта.
SELECT s.sprint_no, s.length_days,
       ROUND(SUM(s.hours_own), 2) AS fund_hh
FROM v_satellite_capacity s
GROUP BY s.sprint_no, s.length_days
ORDER BY s.sprint_no;

\echo '--- 7в. Часы по спринтам последнего прогона (для сверки глазами) ---'
SELECT a.run_id, a.sprint_no,
       ROUND(SUM(a.hours), 2)        AS assigned_hh,
       COUNT(DISTINCT a.engineer_id) AS engineers
FROM plan_assignments a
GROUP BY a.run_id, a.sprint_no
ORDER BY a.run_id, a.sprint_no;
