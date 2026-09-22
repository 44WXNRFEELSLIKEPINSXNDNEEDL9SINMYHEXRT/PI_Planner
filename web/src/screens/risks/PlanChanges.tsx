/**
 * «Что изменилось и почему» — `v_plan_diff` (docs/UI_SPEC.md §8.4). По каждой
 * задаче: что изменилось (`change_type`) и почему (`cause`). `explanation` —
 * готовая фраза из витрины, показывается как есть и не пересказывается.
 * Строки `unchanged` по умолчанию скрыты: их большинство, и они не отвечают
 * на вопрос руководителя «что поехало».
 */
import { useState } from 'react'
import { Badge, Group, Stack, Switch, Text } from '@mantine/core'
import { CAUSE_WORD, CHANGE_WORD, STATUS_WORD } from './labels'
import type { Decision, PlanDiffRow } from '../../types/views'

const DECISION_WORD: Record<Decision, string> = {
  in_quarter: 'в квартале',
  deferred_next_pi: 'перенесена',
  cancelled: 'отменена',
}

function planWord(decision: Decision | null, start: number | null, end: number | null): string {
  if (!decision) return '—'
  if (decision === 'in_quarter' && start !== null && end !== null) {
    return `${DECISION_WORD[decision]}, спринты ${start}–${end}`
  }
  return DECISION_WORD[decision]
}

export function PlanChanges({ rows }: { rows: PlanDiffRow[] }) {
  const [showUnchanged, setShowUnchanged] = useState(false)
  const changed = rows.filter((r) => r.change_type !== 'unchanged')
  const shown = showUnchanged ? rows : changed
  const first = rows[0]

  if (rows.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        Это первый прогон — сравнивать не с чем. Различия появятся после первой загрузки факта.
      </Text>
    )
  }

  return (
    <Stack gap="sm">
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Text size="sm" c="dimmed">
          {first.prev_run_id !== null
            ? `Сравнение с прогоном ${first.prev_run_id}`
            : 'Сравнение с предыдущим прогоном'}
          {first.reported_sprint !== null ? ` · факт сообщён за спринт ${first.reported_sprint}` : ''}
          {' · '}
          изменилось {changed.length} из {rows.length} задач
        </Text>
        <Switch
          size="xs"
          checked={showUnchanged}
          onChange={(e) => setShowUnchanged(e.currentTarget.checked)}
          label={`показать и ${rows.length - changed.length} без изменений`}
        />
      </Group>

      <div style={{ border: '1px solid var(--line)', background: 'var(--surface)' }}>
        {shown.map((row) => (
          <div
            key={`${row.task_id}-${row.change_type}`}
            style={{ borderBottom: '1px solid var(--line)', padding: '8px 10px' }}
          >
            <Group gap="xs" wrap="wrap" align="center">
              <Text size="sm" fw={500} className="mono">
                {row.task_id}
              </Text>
              <Text size="xs" c="dimmed" className="mono">
                {row.prodf_id} · {row.team_id}
              </Text>
              {/* `textTransform: none` — дефолтный капс Mantine здесь запрещён
                  (docs/UI_DESIGN.md §6). */}
              <Badge variant="outline" size="sm" styles={{ label: { textTransform: 'none' } }}>
                {CHANGE_WORD[row.change_type]}
              </Badge>
              {row.cause && (
                <Text size="xs" c="var(--wax-text)">
                  {CAUSE_WORD[row.cause]}
                </Text>
              )}
              {row.status_at_run && (
                <Text size="xs" c="dimmed">
                  {STATUS_WORD[row.status_at_run]}
                </Text>
              )}
            </Group>
            <Text size="xs" c="dimmed" className="tabular" mt={2}>
              было: {planWord(row.prev_decision, row.prev_start, row.prev_end)} · стало:{' '}
              {planWord(row.new_decision, row.new_start, row.new_end)}
            </Text>
            {row.explanation && (
              <Text size="sm" mt={2}>
                {row.explanation}
              </Text>
            )}
          </div>
        ))}
      </div>
    </Stack>
  )
}
