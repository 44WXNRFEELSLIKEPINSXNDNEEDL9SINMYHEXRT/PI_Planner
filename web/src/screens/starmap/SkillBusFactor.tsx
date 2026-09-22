/**
 * Bus Factor ПО КОМПЕТЕНЦИЯМ — главная витрина ТЗ (`v_bus_factor_skill`).
 * Градации риска приходят из базы готовыми строками; по умолчанию показаны
 * две опасные. Клик по строке подсвечивает носителей на карте.
 */
import { useState } from 'react'
import { Text } from '@mantine/core'
import type { BusFactorSkillRow } from '../../types/views'
import { fmtHours } from '../../api/wire'
import { chip, muted } from './darkStyles'
import { shortTeam } from './risk'

const GRADES = [
  { key: 'критично: работу не подхватит никто', label: 'критично', color: 'var(--flare)' },
  { key: 'единственный носитель', label: 'единственный носитель', color: 'var(--ember)' },
  { key: 'два носителя', label: 'два носителя', color: 'var(--orbit)' },
  { key: 'ок', label: 'три и больше', color: 'var(--bloom)' },
]

export function SkillBusFactor({
  rows,
  selectedSkill,
  onSelectSkill,
}: {
  rows: BusFactorSkillRow[]
  selectedSkill: number | null
  onSelectSkill: (row: BusFactorSkillRow | null) => void
}) {
  const [active, setActive] = useState<Set<string>>(new Set([GRADES[0].key, GRADES[1].key]))
  const counts = new Map<string, number>()
  rows.forEach((r) => counts.set(r.risk, (counts.get(r.risk) ?? 0) + 1))
  const shown = rows.filter((r) => active.has(r.risk))
  const toggle = (key: string) =>
    setActive((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 10 }}>
        {GRADES.map((g) => (
          <button key={g.key} type="button" aria-pressed={active.has(g.key)} onClick={() => toggle(g.key)} style={chip(active.has(g.key))}>
            <span style={{ display: 'inline-block', width: 8, height: 8, background: g.color, marginRight: 6 }} />
            {g.label} <span className="mono" style={muted}>{counts.get(g.key) ?? 0}</span>
          </button>
        ))}
      </div>
      <Text size="xs" style={muted} mb={8}>
        Показано {shown.length} из {rows.length} компетенций. Клик по строке — носители подсветятся на карте.
      </Text>
      <div style={{ maxHeight: 420, overflow: 'auto' }}>
        <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: 13 }}>
          <thead style={{ position: 'sticky', top: 0, background: 'var(--field)' }}>
            <tr style={muted}>
              <th style={th}>Компетенция</th>
              <th style={{ ...th, textAlign: 'right' }}>Носителей</th>
              <th style={th}>Кто</th>
              <th style={th}>Роль</th>
              <th style={{ ...th, textAlign: 'right' }}>Спрос роли</th>
              <th style={th}>Риск</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const grade = GRADES.find((g) => g.key === r.risk)
              const selected = selectedSkill === r.skill_id
              return (
                <tr
                  key={r.skill_id}
                  tabIndex={0}
                  aria-selected={selected}
                  onClick={() => onSelectSkill(selected ? null : r)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      onSelectSkill(selected ? null : r)
                    }
                  }}
                  style={{ cursor: 'pointer', background: selected ? 'rgba(232,236,245,0.10)' : undefined }}
                >
                  <td style={{ ...td, borderLeft: `3px solid ${grade?.color ?? 'transparent'}` }}>{r.skill_name}</td>
                  <td style={{ ...td, textAlign: 'right' }} className="mono">
                    {r.bus_factor}
                  </td>
                  <td style={td} className="mono">
                    {r.engineers.join(', ')}
                  </td>
                  <td style={td}>
                    {r.roles.join(', ')}
                    <span style={muted}> · {r.teams.map(shortTeam).join(', ')}</span>
                  </td>
                  <td style={{ ...td, textAlign: 'right' }} className="mono">
                    {r.in_demand ? fmtHours(r.roles_demand_hh) : '—'}
                  </td>
                  <td style={{ ...td, color: grade?.color }}>{r.risk}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const th: React.CSSProperties = { textAlign: 'left', fontWeight: 500, padding: '6px 8px', borderBottom: '1px solid rgba(127,166,217,0.3)' }
const td: React.CSSProperties = { padding: '6px 8px', borderBottom: '1px solid rgba(127,166,217,0.12)', verticalAlign: 'top' }
