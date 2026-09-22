/**
 * Карточка одного риска. `message` показывается как есть: это готовый русский
 * текст из базы, пересказывать и сокращать его нельзя (docs/UI_SPEC.md §2.3,
 * docs/UI_DESIGN.md §8). Ниже — разбор из `payload`: что именно не хватает и
 * что с этим делать.
 */
import { Group, Stack, Text } from '@mantine/core'
import { RiskRail, levelFromAlert } from '../../components/common/RiskRail'
import { ALERT_WORD } from './labels'
import { fmtHours } from '../../api/wire'
import type {
  AlertCascadeShiftPayload,
  AlertDeadlineMissPayload,
  AlertRoleDeficitPayload,
  AlertRow,
  Json,
} from '../../types/views'

const ENTITY_WORD: Record<AlertRow['entity_type'], string> = {
  task: 'задача',
  initiative: 'инициатива',
  team: 'команда',
  role: 'роль',
  engineer: 'инженер',
}

/** `payload` в витрине объявлен как `Json` — форма зависит от `alert_type`. */
function payloadOf<T>(value: Json): T | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as unknown as T)
    : null
}

export function AlertCard({
  alert,
  reasonLabels,
}: {
  alert: AlertRow
  /** Код причины → русская формулировка из `ref_decision_reasons`. */
  reasonLabels: Map<string, string>
}) {
  return (
    <RiskRail level={levelFromAlert(alert.level)} label={ALERT_WORD[alert.alert_type]}>
      <Stack gap={4}>
        <Group gap="xs" wrap="wrap">
          <Text size="sm" fw={500} className="mono">
            {alert.entity_id}
          </Text>
          <Text size="xs" c="dimmed">
            {ENTITY_WORD[alert.entity_type]} · спринт {alert.sprint_no}
          </Text>
        </Group>
        <Text size="sm">{alert.message}</Text>
        {alert.alert_type === 'role_deficit' && (
          <RoleDeficit p={payloadOf<AlertRoleDeficitPayload>(alert.payload)} />
        )}
        {alert.alert_type === 'deadline_miss' && (
          <DeadlineMiss p={payloadOf<AlertDeadlineMissPayload>(alert.payload)} reasonLabels={reasonLabels} />
        )}
        {alert.alert_type === 'cascade_shift' && (
          <CascadeShift p={payloadOf<AlertCascadeShiftPayload>(alert.payload)} />
        )}
      </Stack>
    </RiskRail>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Group gap={6} wrap="nowrap" align="flex-start">
      <Text size="xs" c="dimmed" style={{ flexShrink: 0, minWidth: 132 }}>
        {label}
      </Text>
      <Text size="xs" style={{ minWidth: 0 }}>
        {children}
      </Text>
    </Group>
  )
}

function TaskList({ tasks }: { tasks: string[] }) {
  return (
    <Text size="xs" className="mono">
      {tasks.join(', ')}
    </Text>
  )
}

function RoleDeficit({ p }: { p: AlertRoleDeficitPayload | null }) {
  if (!p) return null
  return (
    <Stack gap={2}>
      <Row label="Вердикт">{p.verdict}</Row>
      <Row label="Часы">
        <span className="tabular">
          нужно {fmtHours(p.demand_hh)}, в плане {fmtHours(p.planned_hh)}, не закрыто{' '}
          {fmtHours(p.unmet_hh)}, доступно {fmtHours(p.supply_hh)}
        </span>
      </Row>
      {p.tasks.length > 0 && <Row label="Ждут этот ресурс"><TaskList tasks={p.tasks} /></Row>}
      {p.reason && <Row label="Почему нечем закрыть">{p.reason}</Row>}
    </Stack>
  )
}

function DeadlineMiss({
  p,
  reasonLabels,
}: {
  p: AlertDeadlineMissPayload | null
  reasonLabels: Map<string, string>
}) {
  if (!p) return null
  const reasons = Object.entries(p.reasons ?? {})
  return (
    <Stack gap={2}>
      <Row label="Перенесено задач">
        <span className="tabular">{p.deferred_tasks.length}</span> на {fmtHours(p.deferred_hh)}
      </Row>
      {p.deferred_tasks.length > 0 && <Row label="Список"><TaskList tasks={p.deferred_tasks} /></Row>}
      {reasons.length > 0 && (
        <Stack gap={0} style={{ minWidth: 0 }}>
          {reasons.map(([task, code]) => (
            <Group key={task} gap={6} wrap="nowrap" align="flex-start">
              <Text size="xs" className="mono" style={{ flexShrink: 0 }}>
                {task}
              </Text>
              <Text size="xs" c="dimmed" style={{ minWidth: 0 }}>
                {code ? (reasonLabels.get(code) ?? code) : '—'}
              </Text>
            </Group>
          ))}
        </Stack>
      )}
      {p.cancelled.length > 0 && <Row label="Отменено"><TaskList tasks={p.cancelled} /></Row>}
      <Row label="Обещана в базовой линии">
        {p.baseline_committed
          ? 'да — инициатива была в плане Недели 0, поэтому входит в знаменатель KPI'
          : 'нет — в плане Недели 0 инициатива не была обещана, знаменатель KPI её не включает'}
      </Row>
      <Row label="Квартальная цель">{p.threatened_goal ? 'под угрозой' : 'не под угрозой'}</Row>
    </Stack>
  )
}

function CascadeShift({ p }: { p: AlertCascadeShiftPayload | null }) {
  if (!p) return null
  return (
    <Stack gap={2}>
      <Row label="Сдвиг">
        <span className="tabular">
          спринт {p.baseline_start_sprint} стал спринтом {p.new_start_sprint}
        </span>
      </Row>
      <Row label="Причина">{p.cause_text}</Row>
      {p.dependents.length > 0 && <Row label="Тянет за собой"><TaskList tasks={p.dependents} /></Row>}
      <Row label="По факту сообщено">
        <span className="tabular">спринт {p.reported_sprint}</span>
      </Row>
    </Stack>
  )
}
