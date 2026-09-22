/**
 * «Где отсутствие одного сотрудника создаёт риск» (ТЗ, docs/UI_DESIGN.md
 * §5.4): задачи текущего плана без замены, компетенции-одиночки, ёмкость по
 * спринтам. `hours_own` уже умножен на множитель спринта — не пересчитываем.
 */
import { Drawer, Stack, Text } from '@mantine/core'
import type { EngineerAbsenceRiskRow, OrbitMapRow, SatelliteCapacityRow } from '../../types/views'
import { fmtHours, num } from '../../api/wire'
import { LEVEL_COLOR, LEVEL_WORD, shortTeam, starLevel } from './risk'
import { muted } from './darkStyles'

export function EngineerDrawer({
  orbit,
  absence,
  capacity,
  onClose,
}: {
  orbit: OrbitMapRow | null
  absence: EngineerAbsenceRiskRow | undefined
  capacity: SatelliteCapacityRow[]
  onClose: () => void
}) {
  const level = orbit ? starLevel(orbit, absence) : 'ok'
  const bySprint = new Map<number, SatelliteCapacityRow[]>()
  capacity.forEach((c) => bySprint.set(c.sprint_no, [...(bySprint.get(c.sprint_no) ?? []), c]))
  const sprints = [...bySprint.keys()].sort((a, b) => a - b)

  return (
    <Drawer
      opened={orbit !== null}
      onClose={onClose}
      position="right"
      size="md"
      title={orbit && <span className="mono" style={{ fontWeight: 600 }}>{orbit.engineer_id}</span>}
      styles={{
        content: { background: 'var(--field)', color: 'var(--star)' },
        header: { background: 'var(--field)', color: 'var(--star)', borderBottom: '1px solid rgba(127,166,217,0.2)' },
        close: { color: 'var(--star)' },
      }}
    >
      {orbit && (
        <Stack gap="md" pt="sm">
          <div>
            <Text fw={500}>{orbit.role_name}</Text>
            <Text size="sm" style={muted}>
              {orbit.grade} · {orbit.teams.map(shortTeam).join(' и ')}
              {orbit.teams.length > 1 ? ' — ставка поделена между двумя командами' : ''}
            </Text>
          </div>

          <div style={{ borderLeft: `4px solid ${LEVEL_COLOR[level]}`, paddingLeft: 10 }}>
            <Text size="sm" fw={600} style={{ color: LEVEL_COLOR[level] }}>
              {LEVEL_WORD[level]}
            </Text>
            <Text size="sm">
              {absence?.risk ?? orbit.risk}. Специалистов этой роли в компании:{' '}
              <b className="mono">{absence?.role_bus_factor ?? orbit.bus_factor}</b>.
            </Text>
          </div>

          <Block title="Что встанет, если он выпадет">
            {absence && absence.tasks_without_backup.length > 0 ? (
              <Text size="sm">
                <span className="mono">{absence.tasks_without_backup.join(', ')}</span> —{' '}
                {num(absence.hours_without_backup) >= 1
                  ? `${fmtHours(absence.hours_without_backup)} в текущем плане некому передать.`
                  : 'работа почти закрыта, но довести её до конца, кроме него, некому.'}
              </Text>
            ) : (
              <Text size="sm" style={muted}>
                {absence && absence.planned_tasks.length > 0
                  ? `Задачи плана (${absence.planned_tasks.join(', ')}) есть кому подхватить.`
                  : 'В текущем плане на нём задач нет.'}
              </Text>
            )}
          </Block>

          {absence && absence.unique_critical_skills.length > 0 && (
            <Block title="Компетенции, которые не подхватит никто">
              <Tags items={absence.unique_critical_skills} color="var(--flare)" />
            </Block>
          )}
          {absence && absence.unique_skills.length > absence.unique_critical_skills.length && (
            <Block title="Компетенции только у него">
              <Tags
                items={absence.unique_skills.filter((s) => !absence.unique_critical_skills.includes(s))}
                color="var(--ember)"
              />
            </Block>
          )}

          <Block title={`Заявленный стек (${orbit.skills.length})`}>
            <Tags items={orbit.skills} color="var(--orbit)" />
          </Block>

          {sprints.length > 0 && (
            <Block title="Ёмкость по спринтам">
              <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: 13 }} className="tabular">
                <thead>
                  <tr style={muted}>
                    <th style={th}>Спринт</th>
                    {orbit.teams.map((t) => (
                      <th key={t} style={th}>
                        {shortTeam(t)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sprints.map((s) => (
                    <tr key={s}>
                      <td style={td} className="mono">
                        {s}
                      </td>
                      {orbit.teams.map((t) => {
                        const cell = bySprint.get(s)?.find((c) => c.team_id === t)
                        return (
                          <td key={t} style={td} className="mono">
                            {cell ? `${num(cell.hours_own)} ЧЧ` : '—'}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </Block>
          )}
        </Stack>
      )}
    </Drawer>
  )
}

const th: React.CSSProperties = { textAlign: 'left', fontWeight: 500, padding: '4px 6px', borderBottom: '1px solid rgba(127,166,217,0.25)' }
const td: React.CSSProperties = { padding: '4px 6px', borderBottom: '1px solid rgba(127,166,217,0.12)' }

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Stack gap={6}>
      <Text size="sm" fw={500}>
        {title}
      </Text>
      {children}
    </Stack>
  )
}

function Tags({ items, color }: { items: string[]; color: string }) {
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      {items.map((s) => (
        <span key={s} style={{ fontSize: 12, padding: '2px 8px', border: `1px solid ${color}`, borderRadius: 2 }}>
          {s}
        </span>
      ))}
    </div>
  )
}
