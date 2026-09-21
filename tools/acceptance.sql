-- =====================================================================
--  Приёмка M0 — шесть проверок слоя данных ДС после заливки.
--  Запуск:
--    psql -h 127.0.0.1 -U postgres -d pi_planner -v ON_ERROR_STOP=1 -f tools/acceptance.sql
--  Ожидаемые значения и что делать при расхождении — docs/RUNBOOK.md, раздел 7.
--  Файл держим в UTF-8 (без BOM): в проверках есть кириллица.
-- =====================================================================

\echo '=== 0. Кодировка базы: ожидаем UTF8 ==='
SELECT pg_encoding_to_char(encoding) AS encoding
FROM pg_database WHERE datname = current_database();

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

\echo '=== 1б. Объекты схемы: ожидаем 29 таблиц + 15 вьюх ==='
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

\echo '=== 6а. Приёмка плана: нарушений нет ни в одном прогоне ==='
SELECT (SELECT COUNT(*) FROM v_plan_violations) AS violations,
       (SELECT COUNT(*) FROM plan_runs)        AS runs;

\echo '=== 7. Ёмкость в SP: перегружена только Team-Platform (104%) ==='
SELECT team_id,
       ROUND(avg_velocity, 2)           AS avg_velocity,
       focus_factor,
       ROUND(available_sp_per_sprint, 2) AS sp_per_sprint
FROM v_team_capacity_sp
ORDER BY sp_per_sprint DESC;
