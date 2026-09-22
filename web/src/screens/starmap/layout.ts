/**
 * Детерминированная раскладка звёздной карты (docs/UI_DESIGN.md §5.4).
 * Никакой физики: одни и те же данные всегда дают одну и ту же картинку.
 *
 * - Ядра-команды стоят по окружности. Порядок выбирается перебором всех
 *   перестановок так, чтобы команды, между которыми делят инженеров, были
 *   соседями: тогда связь парттаймера не тянется через центр карты. При
 *   равенстве берётся лексикографически меньший порядок — результат
 *   однозначен.
 * - Инженеры одной команды — по дуге вокруг своего ядра, дуга развёрнута
 *   наружу от центра карты.
 * - Инженеры на двух орбитах — между двумя ядрами, с небольшим
 *   перпендикулярным сдвигом, если таких на одной паре несколько.
 */
import type { OrbitMapRow } from '../../types/views'

export const VIEW_W = 1000
export const VIEW_H = 820
const CX = VIEW_W / 2
const CY = VIEW_H / 2
const CORE_RING = 245
export const CORE_R = 30

export interface CoreNode {
  team_id: string
  x: number
  y: number
  angle: number
  orbitR: number
  members: number
}

export interface EngineerNode {
  row: OrbitMapRow
  x: number
  y: number
  shared: boolean
}

function permutations<T>(items: T[]): T[][] {
  if (items.length <= 1) return [items]
  const out: T[][] = []
  items.forEach((item, i) => {
    const rest = [...items.slice(0, i), ...items.slice(i + 1)]
    for (const p of permutations(rest)) out.push([item, ...p])
  })
  return out
}

function circularDistance(a: number, b: number, n: number): number {
  const d = Math.abs(a - b)
  return Math.min(d, n - d)
}

export function orderTeams(teams: string[], pairs: [string, string][]): string[] {
  const sorted = [...teams].sort()
  if (sorted.length === 0) return []
  if (sorted.length > 8) return sorted // перебор 9! уже дорог; при 6 командах — 120 вариантов
  const [first, ...rest] = sorted
  let best = sorted
  let bestCost = Infinity
  for (const p of permutations(rest)) {
    const order = [first, ...p]
    const index = new Map(order.map((t, i) => [t, i]))
    const cost = pairs.reduce(
      (sum, [a, b]) => sum + circularDistance(index.get(a) ?? 0, index.get(b) ?? 0, order.length),
      0,
    )
    if (cost < bestCost || (cost === bestCost && order.join() < best.join())) {
      best = order
      bestCost = cost
    }
  }
  return best
}

export function computeLayout(rows: OrbitMapRow[]): { cores: CoreNode[]; nodes: EngineerNode[] } {
  if (rows.length === 0) return { cores: [], nodes: [] }
  const teamSet = new Set<string>()
  rows.forEach((r) => r.teams.forEach((t) => teamSet.add(t)))
  const pairs = rows
    .filter((r) => r.teams.length === 2)
    .map((r) => [...r.teams].sort() as [string, string])
  const order = orderTeams([...teamSet], pairs)

  const singles = new Map<string, OrbitMapRow[]>()
  for (const r of rows) {
    if (r.teams.length === 1) {
      const list = singles.get(r.teams[0]) ?? []
      list.push(r)
      singles.set(r.teams[0], list)
    }
  }

  const cores: CoreNode[] = order.map((team_id, i) => {
    const angle = -90 + (i * 360) / order.length
    const rad = (angle * Math.PI) / 180
    const count = singles.get(team_id)?.length ?? 0
    return {
      team_id,
      angle,
      x: CX + CORE_RING * Math.cos(rad),
      y: CY + CORE_RING * Math.sin(rad),
      orbitR: Math.min(104, 62 + count * 4),
      members: rows.filter((r) => r.teams.includes(team_id)).length,
    }
  })
  const coreById = new Map(cores.map((c) => [c.team_id, c]))

  const nodes: EngineerNode[] = []
  for (const core of cores) {
    const list = (singles.get(core.team_id) ?? []).slice().sort((a, b) => a.engineer_id.localeCompare(b.engineer_id))
    const n = list.length
    const step = n > 1 ? Math.min(180 / (n - 1), 40) : 0
    const start = core.angle - (step * (n - 1)) / 2
    list.forEach((row, i) => {
      const rad = ((start + i * step) * Math.PI) / 180
      nodes.push({ row, shared: false, x: core.x + core.orbitR * Math.cos(rad), y: core.y + core.orbitR * Math.sin(rad) })
    })
  }

  const byPair = new Map<string, OrbitMapRow[]>()
  for (const r of rows.filter((x) => x.teams.length === 2)) {
    const key = [...r.teams].sort().join('|')
    const list = byPair.get(key) ?? []
    list.push(r)
    byPair.set(key, list)
  }
  for (const [key, list] of byPair) {
    const [a, b] = key.split('|').map((t) => coreById.get(t))
    if (!a || !b) continue
    const mx = (a.x + b.x) / 2
    const my = (a.y + b.y) / 2
    const len = Math.hypot(b.x - a.x, b.y - a.y) || 1
    const px = -(b.y - a.y) / len
    const py = (b.x - a.x) / len
    list
      .sort((x, y) => x.engineer_id.localeCompare(y.engineer_id))
      .forEach((row, i) => {
        const offset = (i - (list.length - 1) / 2) * 26
        nodes.push({ row, shared: true, x: mx + px * offset, y: my + py * offset })
      })
  }

  return { cores, nodes }
}
