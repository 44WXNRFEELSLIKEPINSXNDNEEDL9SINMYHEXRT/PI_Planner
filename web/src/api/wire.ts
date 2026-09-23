/**
 * Чтение значений так, как их реально шлёт сервер (docs/UI_SPEC.md §0.1).
 * `numeric` PostgreSQL приходит строкой — использовать `+row.x` нельзя.
 */
import type { NumericString } from '../types/views'

/** "140.00" → 140. Пусто/null → 0 — только там, где ноль осмыслен как значение. */
export function num(value: NumericString | null | undefined): number {
  if (value === null || value === undefined || value === '') return 0
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

/** Отрицательное значение по ЗНАКУ СТРОКИ, а не после parseFloat. */
export function isNegative(value: NumericString | null | undefined): boolean {
  return typeof value === 'string' && value.trim().startsWith('-')
}

/** Часы для показа: без хвостовых нулей после запятой, разделитель — пробел на тысячах. */
export function fmtHours(value: NumericString | null | undefined): string {
  const n = num(value)
  const rounded = Math.round(n * 100) / 100
  return `${trimZeros(rounded)} ЧЧ`
}

/** Story Points: то же форматирование, без единицы (SP пишут рядом с числом отдельно). */
export function fmtSp(value: NumericString | number | null | undefined): string {
  const n = typeof value === 'number' ? value : num(value)
  return trimZeros(Math.round(n * 100) / 100)
}

function trimZeros(n: number): string {
  const s = n.toFixed(2)
  return s.replace(/\.?0+$/, '') || '0'
}

/** ГГГГ-ММ-ДД → ДД.ММ. Короткая дата для тесных колонок ганта и лент. */
export function fmtDateShort(value: string | null | undefined): string {
  if (!value) return '—'
  const [, m, d] = value.split('-')
  return m && d ? `${d}.${m}` : value
}

/** ГГГГ-ММ-ДД → ДД.ММ.ГГГГ. */
export function fmtDate(value: string | null | undefined): string {
  if (!value) return '—'
  const [y, m, d] = value.split('-')
  return y && m && d ? `${d}.${m}.${y}` : value
}
