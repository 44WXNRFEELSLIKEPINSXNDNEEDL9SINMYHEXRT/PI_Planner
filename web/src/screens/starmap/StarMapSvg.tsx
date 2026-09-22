/**
 * SVG звёздной карты. Ядра — команды, точки — инженеры. Каждая точка —
 * кнопка с клавиатуры (Tab, Enter), подпись и уровень риска дублируются
 * текстом: цвет не единственный носитель смысла (docs/UI_DESIGN.md §9).
 */
import { useMemo, useState } from 'react'
import type { EngineerAbsenceRiskRow, OrbitMapRow } from '../../types/views'
import { CORE_R, VIEW_H, VIEW_W, computeLayout } from './layout'
import { LEVEL_COLOR, LEVEL_WORD, gradeRadius, shortTeam, starLevel } from './risk'

export function StarMapSvg({
  rows,
  absenceById,
  highlight,
  selectedId,
  onSelect,
}: {
  rows: OrbitMapRow[]
  absenceById: Map<string, EngineerAbsenceRiskRow>
  highlight: Set<string> | null
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const { cores, nodes } = useMemo(() => computeLayout(rows), [rows])
  const coreById = new Map(cores.map((c) => [c.team_id, c]))
  const [hover, setHover] = useState<string | null>(null)

  const dim = (id: string) => highlight !== null && !highlight.has(id)

  return (
    <svg
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      width="100%"
      role="group"
      aria-label={`Звёздная карта: ${cores.length} команд, ${nodes.length} инженеров`}
      style={{ display: 'block', maxHeight: '78vh' }}
    >
      {/* орбиты ядер */}
      {cores.map((c) => (
        <circle
          key={`orbit-${c.team_id}`}
          cx={c.x}
          cy={c.y}
          r={c.orbitR}
          fill="none"
          stroke="var(--orbit)"
          strokeOpacity={0.22}
          strokeDasharray="2 5"
        />
      ))}

      {/* связи инженер → ядро */}
      {nodes.map((n) =>
        n.row.teams.map((t) => {
          const core = coreById.get(t)
          if (!core) return null
          return (
            <line
              key={`link-${n.row.engineer_id}-${t}`}
              x1={core.x}
              y1={core.y}
              x2={n.x}
              y2={n.y}
              stroke="var(--orbit)"
              strokeWidth={n.shared ? 1.4 : 0.8}
              strokeOpacity={dim(n.row.engineer_id) ? 0.08 : n.shared ? 0.75 : 0.3}
              strokeDasharray={n.shared ? '5 3' : undefined}
            />
          )
        }),
      )}

      {/* ядра-команды */}
      {cores.map((c) => (
        <g key={`core-${c.team_id}`}>
          <circle cx={c.x} cy={c.y} r={CORE_R} fill="var(--field)" stroke="var(--star)" strokeWidth={1.5} />
          <text x={c.x} y={c.y - 2} textAnchor="middle" fontSize={12} fontWeight={600} fill="var(--star)">
            {shortTeam(c.team_id)}
          </text>
          <text x={c.x} y={c.y + 12} textAnchor="middle" fontSize={10} fill="var(--orbit)" fontFamily="var(--font-mono)">
            {c.members} чел.
          </text>
        </g>
      ))}

      {/* инженеры */}
      {nodes.map((n) => {
        const absence = absenceById.get(n.row.engineer_id)
        const level = starLevel(n.row, absence)
        const r = gradeRadius(n.row.grade)
        const color = LEVEL_COLOR[level]
        const isSel = selectedId === n.row.engineer_id
        const showLabel = level !== 'ok' || hover === n.row.engineer_id || isSel || (highlight?.has(n.row.engineer_id) ?? false)
        const faded = dim(n.row.engineer_id)
        const label = `${n.row.engineer_id}, ${n.row.role_name}, ${n.row.grade}, ${n.row.teams.join(' и ')}, ${LEVEL_WORD[level]}`
        return (
          <g
            key={n.row.engineer_id}
            role="button"
            tabIndex={0}
            aria-label={label}
            aria-pressed={isSel}
            onClick={() => onSelect(n.row.engineer_id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onSelect(n.row.engineer_id)
              }
            }}
            onMouseEnter={() => setHover(n.row.engineer_id)}
            onMouseLeave={() => setHover(null)}
            onFocus={() => setHover(n.row.engineer_id)}
            onBlur={() => setHover(null)}
            style={{ cursor: 'pointer', outline: 'none' }}
            opacity={faded ? 0.22 : 1}
          >
            <title>{label}</title>
            {/* область попадания больше самой точки */}
            <circle cx={n.x} cy={n.y} r={r + 7} fill="transparent" />
            {(isSel || hover === n.row.engineer_id) && (
              <circle cx={n.x} cy={n.y} r={r + 5} fill="none" stroke="var(--star)" strokeWidth={1.5} />
            )}
            {level === 'critical' && (
              <circle cx={n.x} cy={n.y} r={r + 3.5} fill="none" stroke={color} strokeWidth={1.5} />
            )}
            {n.shared ? (
              // парттаймер на двух орбитах — ромб, а не круг
              <rect
                x={n.x - r * 0.9}
                y={n.y - r * 0.9}
                width={r * 1.8}
                height={r * 1.8}
                transform={`rotate(45 ${n.x} ${n.y})`}
                fill={color}
                stroke="var(--void)"
                strokeWidth={1.5}
              />
            ) : (
              <circle cx={n.x} cy={n.y} r={r} fill={color} stroke="var(--void)" strokeWidth={1.5} />
            )}
            {showLabel && (
              <text
                x={n.x}
                y={n.y - r - 6}
                textAnchor="middle"
                fontSize={10.5}
                fontFamily="var(--font-mono)"
                fill="var(--star)"
                stroke="var(--void)"
                strokeWidth={3}
                paintOrder="stroke"
              >
                {n.row.engineer_id}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}
