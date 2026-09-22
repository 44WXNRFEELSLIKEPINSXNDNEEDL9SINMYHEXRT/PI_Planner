/**
 * Шесть колонок спринтов — несущая сетка интерфейса (docs/UI_DESIGN.md §1):
 * одна и та же логика колонок на плане, рисках и KPI, чтобы взгляд
 * переносился между экранами без перенастройки.
 */
export function sprintGridTemplate(
  count: number,
  labelWidth = '230px',
  tailWidth = '34px',
): string {
  return `${labelWidth} repeat(${Math.max(count, 1)}, minmax(58px, 1fr)) ${tailWidth}`
}
