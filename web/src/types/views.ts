/**
 * Формы строк по каждой витрине API — КАК ОНИ ПРИХОДЯТ ПО СЕТИ, а не как
 * объявлены в базе. Это отдельный файл от `types/db.ts` (сгенерированного из
 * схемы PostgreSQL) намеренно: `numeric` в базе — это `number` в db.ts, но в
 * JSON-ответе сервер отдаёт его СТРОКОЙ (`json.dumps(default=str)` на Decimal,
 * см. docs/UI_SPEC.md §0.1). Использовать db.ts для строк ответа — готовый
 * баг: `"140.00" + 1` даст `"140.001"`, а не `141`.
 *
 * Числа-строки читаем через `num()` из `src/api/wire.ts`, а не через `+`.
 */

/** JSON-значение колонки jsonb/json. */
export type Json = string | number | boolean | null | Json[] | { [key: string]: Json }

/** `numeric` PostgreSQL в JSON — строка вида "140.00" или "-6.50". */
export type NumericString = string

export type TaskStatus = 'ToDo' | 'InProgress' | 'Done'
/**
 * Состояние задачи в разрезе прогона — домен ШИРЕ, чем у `tasks.status`:
 * `task_state.status` допускает ещё `Deferred` и `Cancelled`
 * (db/02_contract.sql, CHECK на `task_state`). В `v_plan_diff.status_at_run`
 * приходит именно он, и `Deferred` там — самое частое значение.
 */
export type TaskStateStatus = TaskStatus | 'Deferred' | 'Cancelled'
export type Grade = 'Junior' | 'Middle' | 'Senior'
export type Decision = 'in_quarter' | 'deferred_next_pi' | 'cancelled'
export type AlertLevel = 'red' | 'yellow' | 'orange'
export type AlertType = 'deadline_miss' | 'cascade_shift' | 'role_deficit'
export type ViolationSeverity = 'error' | 'warning'
export type KpiCode = 'pi_predictability' | 'say_do_ratio' | 'bus_factor'
export type KpiKind = 'forecast' | 'actual'
export type DiffCause = 'own_slip' | 'carry_over' | 'dependency' | 'capacity' | 'completed' | null
export type DiffChangeType =
  | 'unchanged'
  | 'newly_planned'
  | 'newly_deferred'
  | 'shifted_later'
  | 'shifted_earlier'
  | 'completed'

// ---------------------------------------------------------------- доска задач
export interface TaskBoardRow {
  task_id: string
  prodf_id: string
  br_id: string
  priority_rung: number | null
  team_id: string
  summary: string | null
  status: TaskStatus
  rung: number | null
  estimation_sp: number
  estimated_hh_effective: NumericString
  estimated_hh_declared: NumericString | null
  estimated_hh_matrix_total: NumericString | null
  estimate_disputed: boolean
  planned_start: string | null
  planned_end: string | null
  actual_start: string | null
  actual_end: string | null
  topo_order: number
  depth: number
  earliest_start_sprint: number | null
  on_critical_path: boolean
  remaining_hh: NumericString
  blocked_by: number
  blocks: number
}

export interface TaskRow {
  task_id: string
  prodf_id: string
  team_id: string
  summary: string | null
  status: TaskStatus
  rung: number | null
  estimation_sp: number
  estimated_hh_effective: NumericString
  estimated_hh_declared: NumericString | null
  estimated_hh_matrix_total: NumericString | null
  spent_time_declared: NumericString | null
  created_at: string | null
  planned_start: string | null
  planned_end: string | null
  actual_start: string | null
  actual_end: string | null
  result_planned: string | null
  result_customer: string | null
  result_executor: string | null
  result_final: string | null
  committed_week0: boolean
}

// --------------------------------------------------------------- звёздная карта
export interface OrbitMapRow {
  engineer_id: string
  role_name: string
  role_group: string
  grade: Grade
  total_capacity_rate: NumericString
  orbit_count: number
  teams: string[]
  skills: string[]
  bus_factor: number
  risk: string
}

export interface SatelliteCapacityRow {
  engineer_id: string
  team_id: string
  role_id: number
  grade: Grade
  pi_id: string
  sprint_no: number
  start_date: string
  end_date: string
  capacity_rate: NumericString
  is_shared_orbit: boolean
  length_days: number
  hours_own: NumericString
}

export interface BusFactorSkillRow {
  skill_id: number
  skill_name: string
  bus_factor: number
  engineers: string[]
  teams: string[]
  roles: string[]
  roles_demand_hh: NumericString
  in_demand: boolean
  sole_in_role: boolean
  risk: string
}

export interface EngineerAbsenceRiskRow {
  run_id: number
  engineer_id: string
  role_name: string
  grade: Grade
  total_capacity_rate: NumericString
  teams: string[]
  role_bus_factor: number
  unique_skills: string[]
  unique_critical_skills: string[]
  planned_hours: NumericString
  planned_tasks: string[]
  tasks_without_backup: string[]
  hours_without_backup: NumericString
  risk: string
}

export interface TeamProfileRow {
  team_id: string
  members: number
  part_time_members: number
  fte: NumericString
  hours_per_sprint: NumericString
  avg_velocity: NumericString | null
  available_sp_per_sprint: NumericString | null
  available_sp_per_pi: NumericString | null
  roles_present: string[]
  roles_missing: string[]
  skills_n: number
  unique_skills: string[]
  live_tasks: number
  live_sp: NumericString
  live_hh: NumericString
}

// -------------------------------------------------------------- роли и ёмкость
export interface TeamCapacitySpRow {
  team_id: string
  history_points: number
  avg_velocity: NumericString
  focus_factor: NumericString
  available_sp_per_sprint: NumericString
  available_sp_per_pi: NumericString
}

export interface TeamRow {
  team_id: string
  focus_factor: NumericString
}

export interface RoleDeficitRow {
  team_id: string
  role_name: string
  demand_hh: NumericString
  supply_hh: NumericString
  gap_hh: NumericString
  verdict: string
}

export interface RoleDeficitEffectiveRow {
  team_id: string
  role_name: string
  demand_hh: NumericString
  supply_with_substitution_hh: NumericString
  gap_hh: NumericString
  verdict: string
}

export interface RoleCoverageOrgRow {
  role_name: string
  demand_hh: NumericString
  native_people: number
  people_incl_substitution: number
  supply_hh: NumericString
  gap_hh: NumericString
  verdict: string
}

export interface BusFactorRow {
  role_id: number
  role_name: string
  role_group: string
  bus_factor: number
  demand_hh: NumericString
  risk: string
}

// -------------------------------------------------------------------- календарь
export interface PiFundFactorRow {
  pi_id: string
  sprint_length_days: number
  days_total: number
  factor: NumericString
}

export interface SprintRow {
  pi_id: string
  sprint_no: number
  start_date: string
  end_date: string
  length_days: number
}

// ------------------------------------------------------------------ справочники
export interface InitiativeRow {
  prodf_id: string
  br_id: string
  title: string | null
  priority_rung: number | null
}

export interface RefDecisionReasonRow {
  code: string
  ord: number
  decision: Decision
  label: string
  legacy_reason: string | null
}

export interface DqSummaryRow {
  rule_code: string
  severity: 'info' | 'warning' | 'error'
  n: number
  example: string | null
}

// -------------------------------------------------------------- контракт прогона
export interface PlanRunRow {
  run_id: number
  pi_id: string
  as_of_sprint: number
  algorithm: string
  params: Json
  status: 'ok' | 'infeasible' | 'failed'
  note: string | null
  actuals_upload_id: number | null
  created_at: string
}

export interface PlanTaskScheduleRow {
  run_id: number
  task_id: string
  start_sprint: number | null
  end_sprint: number | null
  forecast_end_date: string | null
  decision: Decision
  decision_reason: string | null
  reason_code: string | null
  reason_text: string | null
  reason_details: Json
}

export interface PlanAssignmentDetailRow {
  run_id: number
  task_id: string
  sprint_no: number
  engineer_id: string
  hours: NumericString
  home_team_id: string
  serving_team_id: string
  is_loan: boolean
  served_role: string
  native_role: string
  is_substitution: boolean
  grade: Grade
}

export interface PlanTaskSpRow {
  run_id: number
  task_id: string
  sprint_no: number
  sp: NumericString
}

export interface AlertRow {
  alert_id: number
  run_id: number
  sprint_no: number
  level: AlertLevel
  alert_type: AlertType
  entity_type: 'task' | 'initiative' | 'team' | 'role' | 'engineer'
  entity_id: string
  message: string
  payload: Json
}

export interface AlertRoleDeficitPayload {
  role_id: number
  role_name: string
  sprint_no: number
  demand_hh: NumericString
  planned_hh: NumericString
  unmet_hh: NumericString
  supply_hh: NumericString
  tasks: string[]
  verdict: string
  reason: string
}

export interface AlertDeadlineMissPayload {
  deferred_tasks: string[]
  deferred_hh: NumericString
  baseline_committed: boolean
  threatened_goal: boolean
  reasons: Record<string, string | null>
  cancelled: string[]
}

export interface AlertCascadeShiftPayload {
  baseline_start_sprint: number
  new_start_sprint: number
  dependents: string[]
  cause: DiffCause
  cause_text: string
  reported_sprint: number
}

export interface KpiSnapshotRow {
  run_id: number
  sprint_no: number
  kpi_code: KpiCode
  value: NumericString
  target_min: NumericString | null
  target_max: NumericString | null
  details: Json
  kind: KpiKind
}

export interface PiPredictabilityDetails {
  formula: string
  committed_initiatives: string[]
  committed_n: number
  partial_initiatives: string[]
  baseline_source: string
  on_track_initiatives?: string[]
  completed_initiatives?: string[]
  reported_through_sprint?: number
  note: string
}

export interface SayDoDetails {
  formula: string
  planned_sp: NumericString
  done_sp: NumericString
  planned_tasks: string[]
  done_tasks: string[]
  note: string
}

export interface BusFactorKpiDetails {
  method: string
  competencies_n: number
  single_holder_n: number
  critical_n: number
  critical: string[]
  roles_without_staff: string[]
  note: string
}

export interface PlanViolationRow {
  run_id: number
  check_code: string
  severity: ViolationSeverity
  entity: string
  detail: string
}

// -------------------------------------------------------------- факт и отклонения
export interface ActualUploadSummary {
  tasks: number
  done: number
  in_progress: number
  hours: NumericString
  warnings: string[]
}

export interface ActualUploadRow {
  upload_id: number
  pi_id: string
  sprint_no: number
  source_file: string
  source_sha256: string
  uploaded_at: string
  summary: ActualUploadSummary
}

export interface PlanDiffRow {
  run_id: number
  prev_run_id: number | null
  reported_sprint: number | null
  task_id: string
  prodf_id: string
  team_id: string
  prev_decision: Decision | null
  prev_start: number | null
  prev_end: number | null
  new_decision: Decision | null
  new_start: number | null
  new_end: number | null
  status_at_run: TaskStateStatus | null
  change_type: DiffChangeType
  cause: DiffCause
  explanation: string | null
}

export interface SprintDeviationRow {
  upload_id: number
  sprint_no: number
  plan_run_id: number | null
  task_id: string
  team_id: string
  estimation_sp: number
  planned_start: number | null
  planned_end: number | null
  reported_status: TaskStatus | null
  planned_hours: NumericString
  spent_hours: NumericString
  deviation: string
}

/** Ответ загрузок: POST /api/dataset и POST /api/actuals. */
export interface PlanRunSummary {
  run_id: number
  as_of_sprint: number
  status: 'ok' | 'infeasible' | 'failed'
  note: string
  in_quarter: number
  not_in_quarter: number
  alerts: number
  violations_error: number
}

export interface DatasetUploadResult {
  dataset: string
  sha256: string
  rows: Record<string, number>
  data_quality: { error: number; warning: number; info: number }
  plan: PlanRunSummary
}

export interface ActualsUploadResult {
  upload_id: number
  sprint_no: number
  file: string
  replaced_sprints: number[]
  baseline_created_run_id: number | null
  summary: {
    tasks: number
    done: number
    in_progress: number
    hours: NumericString
    warnings: string[]
  }
  plan: PlanRunSummary
}

export interface UploadErrorPayload {
  error: 'bad_upload'
  message: string
  problems: string[]
}
