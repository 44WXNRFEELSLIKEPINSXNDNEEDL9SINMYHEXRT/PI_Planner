/**
 * «Спина квартала» для рисков: те же шесть колонок, что на ганте
 * (docs/UI_DESIGN.md §1 — одна сетка на плане, рисках и KPI, чтобы взгляд
 * переносился между экранами без перенастройки). Показывает, где именно
 * в квартале концентрируются риски; клик по спринту отбирает ленту ниже.
 */
import { useMemo } from 'react'
import { Group, Text } from '@mantine/core'
import { sprintGridTemplate } from '../../components/common/sprintGrid'
import { fmtDateShort } from '../../api/wire'
import { ALERT_WORD, LEVEL_COLOR } from './labels'
import type { AlertLevel, AlertRow, AlertType, SprintRow } from '../../types/views'

/** Красный тяжелее оранжевого, оранжевый — жёлтого. */
const SEVERITY: Record<AlertLevel, number> = { red: 0, orange: 1, yellow: 2 }

interface TypeTally {
  alert_type: AlertType
  level: AlertLevel
  n: number
}

export function RiskSpine({
  sprints,
  alerts,
  selected,
  onSelect,
}: {
  sprints: SprintRow[]
  alerts: AlertRow[]
  selected: number | null
  onSelect: (sprint: number | null) => void
}) {
  const bySprint = useMemo(() => {
    const map = new Map<number, AlertRow[]>()
    for (const alert of alerts) {
      const list = map.get(alert.sprint_no) ?? []
      list.push(alert)
      map.set(alert.sprint_no, list)
    }
    return map
  }, [alerts])

  const tallyOf = (rows: AlertRow[]): TypeTally[] => {
    const map = new Map<AlertType, TypeTally>()
    for (const row of rows) {
      const prev = map.get(row.alert_type)
      if (!prev) {
        map.set(row.alert_type, { alert_type: row.alert_type, level: row.level, n: 1 })
      } else {
        prev.n += 1
        if (SEVERITY[row.level] < SEVERITY[prev.level]) prev.level = row.level
      }
    }
    return [...map.values()].sort((a, b) => SEVERITY[a.level] - SEVERITY[b.level])
  }

  const legend = tallyOf(alerts)

  return (
    <div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: sprintGridTemplate(sprints.length, '150px', '46px'),
          minWidth: 620,
          borderTop: '1px solid var(--line)',
          borderLeft: '1px solid var(--line)',
        }}
      >
        <div style={{ display: 'contents' }}>
          <HeadCell>Спринт</HeadCell>
          {sprints.map((s) => (
            <HeadCell key={s.sprint_no} center>
              <div>{s.sprint_no}</div>
              <Text size="10px" c="dimmed">
                {fmtDateShort(s.start_date)}–{fmtDateShort(s.end_date)}
              </Text>
            </HeadCell>
          ))}
          <HeadCell center>всего</HeadCell>
        </div>

        <div style={{ display: 'contents' }}>
          <BodyCell>
            <Text size="xs" c="dimmed">
              {selected === null ? 'нажмите на спринт, чтобы отобрать ленту' : 'нажмите ещё раз, чтобы снять отбор'}
            </Text>
          </BodyCell>

          {sprints.map((s) => {
            const rows = bySprint.get(s.sprint_no) ?? []
            const tally = tallyOf(rows)
            const active = selected === s.sprint_no
            return (
              <SprintCell
                key={s.sprint_no}
                active={active}
                count={rows.length}
                onClick={() => onSelect(active ? null : s.sprint_no)}
                label={
                  rows.length === 0
                    ? `спринт ${s.sprint_no}: рисков нет`
                    : `спринт ${s.sprint_no}: ` + tally.map((t) => `${t.n} ${ALERT_WORD[t.alert_type]}`).join(', ')
                }
              >
                {tally.length === 0 ? (
                  <Text size="xs" c="dimmed">
                    нет
                  </Text>
                ) : (
                  tally.map((t) => (
                    <Group key={t.alert_type} gap={4} wrap="nowrap" justify="center">
                      <Dot color={LEVEL_COLOR[t.level]} />
                      <Text size="xs" className="mono tabular">
                        {t.n}
                      </Text>
                    </Group>
                  ))
                )}
              </SprintCell>
            )
          })}

          <BodyCell center>
            <Text size="sm" className="mono tabular">
              {alerts.length}
            </Text>
          </BodyCell>
        </div>
      </div>

      {legend.length > 0 && (
        <Group gap="lg" mt="xs" wrap="wrap">
          {legend.map((t) => (
            <Group key={t.alert_type} gap={6} wrap="nowrap">
              <Dot color={LEVEL_COLOR[t.level]} />
              <Text size="xs" c="dimmed">
                {ALERT_WORD[t.alert_type]}
              </Text>
            </Group>
          ))}
        </Group>
      )}
    </div>
  )
}

function Dot({ color }: { color: string }) {
  return (
    <span
      aria-hidden
      style={{ width: 8, height: 8, borderRadius: 4, background: color, display: 'inline-block', flexShrink: 0 }}
    />
  )
}

function HeadCell({ children, center }: { children?: React.ReactNode; center?: boolean }) {
  return (
    <div
      style={{
        padding: '8px 10px',
        borderBottom: '2px solid var(--ink)',
        borderRight: '1px solid var(--line)',
        textAlign: center ? 'center' : 'left',
        fontSize: 12,
        fontWeight: 500,
        color: 'var(--muted)',
        background: 'var(--surface)',
      }}
    >
      {children}
    </div>
  )
}

function BodyCell({ children, center }: { children?: React.ReactNode; center?: boolean }) {
  return (
    <div
      style={{
        padding: '8px 10px',
        borderBottom: '1px solid var(--line)',
        borderRight: '1px solid var(--line)',
        background: 'var(--surface)',
        textAlign: center ? 'center' : 'left',
        minWidth: 0,
      }}
    >
      {children}
    </div>
  )
}

function SprintCell({
  children,
  active,
  count,
  onClick,
  label,
}: {
  children: React.ReactNode
  active: boolean
  count: number
  onClick: () => void
  label: string
}) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      aria-pressed={active}
      style={{
        all: 'unset',
        cursor: 'pointer',
        boxSizing: 'border-box',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 2,
        minHeight: 46,
        padding: '6px 4px',
        borderBottom: '1px solid var(--line)',
        borderRight: '1px solid var(--line)',
        background: active ? 'var(--paper)' : 'var(--surface)',
        boxShadow: active ? 'inset 0 0 0 2px var(--ink)' : 'none',
      }}
    >
      {children}
      {count > 0 && (
        <Text size="10px" c="dimmed" className="tabular">
          {count}
        </Text>
      )}
    </button>
  )
}
