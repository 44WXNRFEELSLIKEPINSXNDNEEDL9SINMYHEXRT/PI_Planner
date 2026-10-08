-- СГЕНЕРИРОВАНО tools/gen_migration_stamps.py — не править руками.
-- Миграции, уже учтённые в базовой схеме db/01…05. Контрольная сумма `baseline`
-- означает «применена установкой», tools/migrate.py такие миграции пропускает.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (version, checksum) VALUES
  ('0001_schema_migrations', 'baseline'),
  ('0002_actual_date_events', 'baseline'),
  ('0005_task_role_etc', 'baseline'),
  ('0006_completed_sp', 'baseline'),
  ('0024_authoritative_estimate', 'baseline'),
  ('0037_historical_deviations', 'baseline'),
  ('0045_plan_role_demand_snapshot', 'baseline'),
  ('0046_refresh_plan_invariants', 'baseline'),
  ('0047_multi_orbit_assignment', 'baseline'),
  ('0048_dependency_mode_invariants', 'baseline'),
  ('0049_kpi_calculation_status', 'baseline'),
  ('0050_remaining_pi_role_fund', 'baseline'),
  ('0051_task_skill_requirements', 'baseline'),
  ('0052_canonical_skill_aliases', 'baseline'),
  ('0053_demanded_skill_risk', 'baseline'),
  ('0054_absence_backup_capacity', 'baseline'),
  ('0055_plan_diff_fidelity', 'baseline'),
  ('0056_historical_role_demand_view', 'baseline'),
  ('0057_plan_task_progress', 'baseline'),
  ('0058_actual_report_coverage', 'baseline'),
  ('0059_actual_report_issues', 'baseline'),
  ('0060_live_dependency_graph', 'baseline'),
  ('0061_plan_goal_outcomes', 'baseline'),
  ('0062_current_pi_calendar_default', 'baseline'),
  ('0063_provisional_remaining', 'baseline'),
  ('0064_schema_alignment', 'baseline'),
  ('0065_auth_audit', 'baseline'),
  ('0066_team_velocity_observed', 'baseline'),
  ('0067_graph_horizon_reason', 'baseline'),
  ('0068_initiative_business_priority', 'baseline'),
  ('0069_sprint_forecast_accuracy', 'baseline'),
  ('0070_dq_issue_worklist', 'baseline'),
  ('0071_source_trace', 'baseline'),
  ('0072_pi_contexts', 'baseline'),
  ('0073_engineer_calendar_qualifications', 'baseline'),
  ('0074_upload_revisions', 'baseline'),
  ('0075_assistant_foundation', 'baseline'),
  ('0076_assistant_knowledge', 'baseline'),
  ('0077_assistant_kb_document_uniqueness', 'baseline')
ON CONFLICT (version) DO NOTHING;

-- The seed is loaded after db/01_schema.sql. Register its first generation now.
INSERT INTO public.assistant_dataset_generations
    (generation_id, schema_name, pi_id, scenario_id, source_sha256)
SELECT gen_random_uuid(), current_schema(), p.pi_id, 'main',
       COALESCE((SELECT b.source_sha256 FROM load_batches b
                 ORDER BY b.batch_id DESC LIMIT 1), 'unloaded')
FROM pi_periods p
WHERE NOT EXISTS (SELECT 1 FROM public.assistant_dataset_generations g
                  WHERE g.schema_name = current_schema() AND g.active)
ORDER BY p.pi_id LIMIT 1;
