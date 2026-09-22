/**
 * Гант на CSS grid (docs/UI_DESIGN.md §5.2). Каждая логическая строка —
 * `display:contents`-обёртка, дающая ровно N+2 ячейки: авто-раскладка грида
 * сама распределяет их по столбцам без ручного учёта номеров строк.
 */
import { Fragment } from 'react'
import { Badge, Text } from '@mantine/core'
import { sprintGridTemplate } from '../../components/common/sprintGrid'
import { fmtDateShort, num } from '../../api/wire'
import type {
  PlanAssignmentDetailRow,
  PlanTaskScheduleRow,
  PlanTaskSpRow,
  SprintRow,
  TaskRow,
} from '../../types/views'
import type { InitiativeGroup } from './usePlanData'

const DECISION_FLAG: Record<string, { glyph: string; color: string; title: string }> = {
  in_quarter: { glyph: 'ⓘ', color: 'var(--muted)', title: 'В квартале — открыть причину' },
  deferred_next_pi: { glyph: '⚑', color: 'var(--wax-text)', title: 'Перенесена — открыть причину' },
  cancelled: { glyph: '✕', color: 'var(--stamp)', title: 'Рекомендована к отмене — открыть причину' },
}

export function GanttGrid({
  sprints,
  groups,
  scheduleByTask,
  baselineByTask,
  hasBaseline,
  spByTask,
  assignmentsByTask,
  onSelect,
}: {
  sprints: SprintRow[]
  groups: InitiativeGroup[]
  scheduleByTask: Map<string, PlanTaskScheduleRow>
  baselineByTask: Map<string, PlanTaskScheduleRow>
  hasBaseline: boolean
  spByTask: Map<string, PlanTaskSpRow[]>
  assignmentsByTask: Map<string, PlanAssignmentDetailRow[]>
  onSelect: (task: TaskRow) => void
}) {
  const n = sprints.length
  const template = sprintGridTemplate(n)

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: template,
        minWidth: 720,
        borderTop: '1px solid var(--line)',
        borderLeft: '1px solid var(--line)',
      }}
    >
      {/* --- заголовок: номера и даты спринтов --------------------------- */}
      <div style={{ display: 'contents' }}>
        <HeaderCell>Инициатива / задача</HeaderCell>
        {sprints.map((s) => (
          <HeaderCell key={s.sprint_no} center>
            <div>{s.sprint_no}</div>
            <Text size="10px" c="dimmed">
              {fmtDateShort(s.start_date)}–{fmtDateShort(s.end_date)}
            </Text>
          </HeaderCell>
        ))}
        <HeaderCell />
      </div>

      {groups.map((group) => (
        <Fragment key={group.prodf_id}>
          <div style={{ display: 'contents' }}>
            <div
              style={{
                gridColumn: '1 / -1',
                background: 'var(--paper)',
                borderBottom: '1px solid var(--line)',
                borderRight: '1px solid var(--line)',
                padding: '6px 10px',
                display: 'flex',
                gap: 8,
                alignItems: 'center',
              }}
            >
              <Badge variant="outline" color="post" size="sm" className="mono">
                {group.priority_rung ?? '—'}
              </Badge>
              <Text size="sm" fw={500} className="mono">
                {group.prodf_id}
              </Text>
              <Text size="sm" c="dimmed" truncate>
                {group.title}
              </Text>
              <Text size="xs" c="dimmed" ml="auto">
                {group.team_id}
              </Text>
            </div>
          </div>

          {group.tasks.map((task) => {
            const schedule = scheduleByTask.get(task.task_id)
            const baseline = baselineByTask.get(task.task_id)
            const spRows = spByTask.get(task.task_id) ?? []
            const assignments = assignmentsByTask.get(task.task_id) ?? []
            const flag = schedule ? DECISION_FLAG[schedule.decision] : null

            return (
              <div style={{ display: 'contents' }} key={task.task_id}>
                <RowCell onClick={() => onSelect(task)}>
                  <Text size="sm" fw={500} className="mono">
                    {task.task_id}
                  </Text>
                  <Text size="xs" c="dimmed" truncate>
                    {task.summary}
                  </Text>
                </RowCell>

                {sprints.map((s) => {
                  const inQuarter =
                    schedule?.decision === 'in_quarter' &&
                    schedule.start_sprint !== null &&
                    schedule.end_sprint !== null &&
                    s.sprint_no >= schedule.start_sprint &&
                    s.sprint_no <= schedule.end_sprint
                  const isBaselineSprint =
                    hasBaseline &&
                    baseline?.decision === 'in_quarter' &&
                    baseline.start_sprint !== null &&
                    baseline.end_sprint !== null &&
                    s.sprint_no >= baseline.start_sprint &&
                    s.sprint_no <= baseline.end_sprint
                  const sp = spRows.find((r) => r.sprint_no === s.sprint_no)
                  const hasLoan = assignments.some((a) => a.sprint_no === s.sprint_no && a.is_loan)
                  const isDeferredGhost = schedule && schedule.decision !== 'in_quarter'

                  return (
                    <BarCell
                      key={s.sprint_no}
                      onClick={() => onSelect(task)}
                      inQuarter={Boolean(inQuarter)}
                      isBaselineSprint={Boolean(isBaselineSprint)}
                      hasLoan={hasLoan}
                      spLabel={spRows.length > 1 && sp ? num(sp.sp).toFixed(1).replace(/\.0$/, '') : null}
                      ghost={Boolean(isDeferredGhost)}
                    />
                  )
                })}

                <RowCell onClick={() => onSelect(task)} center>
                  {flag && (
                    <span title={flag.title} style={{ color: flag.color, fontSize: 13 }}>
                      {flag.glyph}
                    </span>
                  )}
                </RowCell>
              </div>
            )
          })}
        </Fragment>
      ))}
    </div>
  )
}

function HeaderCell({ children, center }: { children?: React.ReactNode; center?: boolean }) {
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

function RowCell({
  children,
  onClick,
  center,
}: {
  children?: React.ReactNode
  onClick: () => void
  center?: boolean
}) {
  return (
    <button
      onClick={onClick}
      style={{
        all: 'unset',
        cursor: 'pointer',
        display: 'block',
        padding: '5px 10px',
        borderBottom: '1px solid var(--line)',
        borderRight: '1px solid var(--line)',
        background: 'var(--surface)',
        minWidth: 0,
        textAlign: center ? 'center' : 'left',
      }}
    >
      {children}
    </button>
  )
}

function BarCell({
  onClick,
  inQuarter,
  isBaselineSprint,
  hasLoan,
  spLabel,
  ghost,
}: {
  onClick: () => void
  inQuarter: boolean
  isBaselineSprint: boolean
  hasLoan: boolean
  spLabel: string | null
  ghost: boolean
}) {
  return (
    <button
      onClick={onClick}
      style={{
        all: 'unset',
        cursor: 'pointer',
        position: 'relative',
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'center',
        height: 34,
        borderBottom: '1px solid var(--line)',
        borderRight: '1px solid var(--line)',
        background: 'var(--surface)',
      }}
    >
      {ghost && (
        <div
          style={{
            position: 'absolute',
            left: 0,
            right: 0,
            top: '50%',
            borderTop: '1px dashed var(--muted)',
          }}
        />
      )}
      {inQuarter && (
        <div
          style={{
            position: 'absolute',
            left: 2,
            right: 2,
            top: 6,
            bottom: isBaselineSprint ? 8 : 4,
            borderRadius: 1,
            background: hasLoan
              ? 'repeating-linear-gradient(45deg, var(--ink) 0, var(--ink) 3px, transparent 3px, transparent 6px)'
              : 'var(--ink)',
          }}
        />
      )}
      {isBaselineSprint && (
        <div
          style={{
            position: 'absolute',
            left: 2,
            right: 2,
            bottom: 2,
            height: 2,
            background: 'var(--muted)',
          }}
        />
      )}
      {spLabel && (
        <Text size="9px" className="mono tabular" style={{ position: 'relative', color: '#fff', zIndex: 1, marginBottom: 8 }}>
          {spLabel}
        </Text>
      )}
    </button>
  )
}
