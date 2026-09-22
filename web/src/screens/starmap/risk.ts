/**
 * Уровень риска инженера для карты. Источник — `v_engineer_absence_risk`
 * (что встанет, если человек выпадет), запасной — `v_orbit_map.risk`
 * (bus factor его роли). Слово печатается рядом с цветом всегда.
 */
import type { EngineerAbsenceRiskRow, OrbitMapRow } from '../../types/views'

export type StarLevel = 'critical' | 'single' | 'ok'

export const LEVEL_COLOR: Record<StarLevel, string> = {
  critical: 'var(--flare)',
  single: 'var(--ember)',
  ok: 'var(--bloom)',
}

export const LEVEL_WORD: Record<StarLevel, string> = {
  critical: 'работы встанут',
  single: 'незаменим',
  ok: 'есть замена',
}

export function starLevel(orbit: OrbitMapRow, absence: EngineerAbsenceRiskRow | undefined): StarLevel {
  if (absence) {
    if (absence.tasks_without_backup.length > 0 || absence.risk.startsWith('критично')) return 'critical'
    if (absence.risk !== 'ок' || absence.unique_critical_skills.length > 0) return 'single'
    return 'ok'
  }
  return orbit.risk.toLowerCase().startsWith('критично') ? 'single' : 'ok'
}

/** Радиус точки — грейд. */
export function gradeRadius(grade: string): number {
  if (grade === 'Senior') return 10
  if (grade === 'Middle') return 8
  return 6
}

export function shortTeam(team: string): string {
  return team.replace(/^Team-/, '')
}
