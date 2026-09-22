/**
 * Роли, которых нет в штате вовсе. Это найм, а не незаменимость, поэтому
 * отдельным блоком, а не точками на карте (docs/UI_DESIGN.md §5.4).
 */
import { Text } from '@mantine/core'
import type { RoleCoverageOrgRow } from '../../types/views'
import { fmtHours } from '../../api/wire'
import { muted } from './darkStyles'

export function HiringGap({ rows }: { rows: RoleCoverageOrgRow[] }) {
  const gap = rows.filter((r) => r.verdict.startsWith('НАЙМ'))
  if (gap.length === 0) {
    return (
      <Text size="sm" style={muted}>
        Все роли, которые нужны бэклогу, в штате есть.
      </Text>
    )
  }
  return (
    <div style={{ display: 'grid', gap: 6, fontSize: 14 }}>
      {gap.map((r) => (
        <div key={r.role_name} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, borderLeft: '3px solid var(--flare)', paddingLeft: 8 }}>
          <span>
            {r.role_name}
            <span style={muted}> · в штате {r.native_people} чел.</span>
          </span>
          <span className="mono">{fmtHours(r.demand_hh)}</span>
        </div>
      ))}
    </div>
  )
}
