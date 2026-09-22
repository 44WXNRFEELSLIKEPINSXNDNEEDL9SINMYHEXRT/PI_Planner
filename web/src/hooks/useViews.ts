/**
 * Типизированный хук на каждую витрину из белого списка сервера
 * (`app/views.py → SOURCES`). Все идут через `useView` (см. его комментарий):
 * это единственный способ гарантировать, что одна и та же витрина с одними
 * и теми же параметрами везде возвращает одинаковую форму данных.
 */
import { useView } from './useView'
import type { ViewParams } from '../api/client'
import type {
  ActualUploadRow,
  AlertRow,
  BusFactorRow,
  BusFactorSkillRow,
  DqSummaryRow,
  EngineerAbsenceRiskRow,
  EngineerRoleCoverageRow,
  InitiativeRow,
  KpiSnapshotRow,
  OrbitMapRow,
  PiFundFactorRow,
  PlanAssignmentDetailRow,
  PlanBaselineRow,
  PlanDiffRow,
  PlanRunRow,
  PlanTaskScheduleRow,
  PlanTaskSpRow,
  PlanViolationRow,
  RefDecisionReasonRow,
  RefResultOptionRow,
  RoleCoverageOrgRow,
  RoleDeficitEffectiveRow,
  RoleDeficitRow,
  SatelliteCapacityRow,
  SprintDeviationRow,
  SprintFundFactorRow,
  SprintRow,
  TaskBoardRow,
  TaskRemainingHhRow,
  TaskRow,
  TeamCapacitySpRow,
  TeamProfileRow,
  TeamRow,
} from '../types/views'

// --------------------------------------------------------------- доска задач
export const useTaskBoard = (p?: ViewParams) => useView<TaskBoardRow>('v_task_board', p)
export const useTasks = (p?: ViewParams) => useView<TaskRow>('tasks', p)
export const useTaskRemainingHh = (p?: ViewParams) => useView<TaskRemainingHhRow>('v_task_remaining_hh', p)

// -------------------------------------------------------------- план квартала
export const usePlanRuns = () => useView<PlanRunRow>('plan_runs', { limit: 500 })
export const usePlanSchedule = (runId?: number | null) =>
  useView<PlanTaskScheduleRow>('plan_task_schedule', { runId: runId ?? undefined, limit: 500 })
export const usePlanAssignments = (runId?: number | null) =>
  useView<PlanAssignmentDetailRow>('v_plan_assignment_detail', { runId: runId ?? undefined, limit: 5000 })
export const usePlanBaseline = (runId?: number | null) =>
  useView<PlanBaselineRow>('plan_baseline', { runId: runId ?? undefined, limit: 500 })
export const usePlanTaskSp = (runId?: number | null) =>
  useView<PlanTaskSpRow>('plan_task_sp', { runId: runId ?? undefined, limit: 500 })
export const useSprints = () => useView<SprintRow>('sprints', { limit: 20 })
export const useSprintFundFactor = () => useView<SprintFundFactorRow>('v_sprint_fund_factor', { limit: 20 })
export const usePiFundFactor = () => useView<PiFundFactorRow>('v_pi_fund_factor', { limit: 5 })

// ------------------------------------------------------------------- алерты
export const useAlerts = (runId?: number | null) => useView<AlertRow>('alerts', { runId: runId ?? undefined, limit: 500 })
export const usePlanViolations = (runId?: number | null) =>
  useView<PlanViolationRow>('v_plan_violations', { runId: runId ?? undefined, limit: 500 })
export const usePlanDiff = (runId?: number | null) =>
  useView<PlanDiffRow>('v_plan_diff', { runId: runId ?? undefined, limit: 500 })
export const useSprintDeviation = () => useView<SprintDeviationRow>('v_sprint_deviation', { limit: 500 })

// --------------------------------------------------------------------- KPI
export const useKpiSnapshots = (runId?: number | null) =>
  useView<KpiSnapshotRow>('kpi_snapshots', { runId: runId ?? undefined, limit: 100 })

// -------------------------------------------------------------- роли и ёмкость
export const useRoleDeficit = () => useView<RoleDeficitRow>('v_role_deficit', { limit: 500 })
export const useRoleDeficitEffective = () =>
  useView<RoleDeficitEffectiveRow>('v_role_deficit_effective', { limit: 500 })
export const useRoleCoverageOrg = () => useView<RoleCoverageOrgRow>('v_role_coverage_org', { limit: 100 })
export const useBusFactor = () => useView<BusFactorRow>('v_bus_factor', { limit: 100 })
export const useTeamCapacitySp = () => useView<TeamCapacitySpRow>('v_team_capacity_sp', { limit: 20 })
export const useTeams = () => useView<TeamRow>('teams', { limit: 20 })
export const useEngineerRoleCoverage = () =>
  useView<EngineerRoleCoverageRow>('v_engineer_role_coverage', { limit: 500 })
export const useDqSummary = () => useView<DqSummaryRow>('v_dq_summary', { limit: 100 })
export const useInitiatives = () => useView<InitiativeRow>('initiatives', { limit: 100 })
export const useRefResultOptions = () => useView<RefResultOptionRow>('ref_result_options', { limit: 50 })
export const useRefDecisionReasons = () => useView<RefDecisionReasonRow>('ref_decision_reasons', { limit: 50 })

// -------------------------------------------------------------- звёздная карта
export const useOrbitMap = () => useView<OrbitMapRow>('v_orbit_map', { limit: 200 })
export const useSatelliteCapacity = () => useView<SatelliteCapacityRow>('v_satellite_capacity', { limit: 5000 })
export const useBusFactorSkill = () => useView<BusFactorSkillRow>('v_bus_factor_skill', { limit: 500 })
export const useEngineerAbsenceRisk = (runId?: number | null) =>
  useView<EngineerAbsenceRiskRow>('v_engineer_absence_risk', { runId: runId ?? undefined, limit: 200 })
export const useTeamProfile = () => useView<TeamProfileRow>('v_team_profile', { limit: 20 })

// -------------------------------------------------------------------- факт
export const useActualUploads = () => useView<ActualUploadRow>('actual_uploads', { limit: 50 })
