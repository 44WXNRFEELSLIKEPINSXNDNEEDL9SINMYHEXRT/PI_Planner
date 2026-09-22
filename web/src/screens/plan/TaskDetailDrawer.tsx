/**
 * Причина решения — готовый текст (docs/UI_DESIGN.md §8): `reason_text`
 * показывается как есть, не пересказывается и не сокращается.
 */
import { Badge, Drawer, Group, List, Stack, Table, Text } from '@mantine/core'
import type { PlanAssignmentDetailRow, PlanTaskScheduleRow, TaskBoardRow, TaskRow } from '../../types/views'
import { fmtHours, fmtSp, isNegative } from '../../api/wire'

const DECISION_LABEL: Record<string, string> = {
  in_quarter: 'В квартале',
  deferred_next_pi: 'Перенесена',
  cancelled: 'Рекомендована к отмене',
}

const DECISION_COLOR: Record<string, string> = {
  in_quarter: 'var(--route-text)',
  deferred_next_pi: 'var(--wax-text)',
  cancelled: 'var(--stamp)',
}

export function TaskDetailDrawer({
  task,
  board,
  schedule,
  assignments,
  onClose,
}: {
  task: TaskRow | null
  board: TaskBoardRow | undefined
  schedule: PlanTaskScheduleRow | undefined
  assignments: PlanAssignmentDetailRow[]
  onClose: () => void
}) {
  return (
    <Drawer
      opened={task !== null}
      onClose={onClose}
      position="right"
      size="md"
      title={
        task && (
          <Group gap="xs">
            <Text fw={600} className="mono">
              {task.task_id}
            </Text>
            {schedule && (
              <Badge
                variant="light"
                styles={{ root: { color: DECISION_COLOR[schedule.decision], background: 'transparent', border: `1px solid ${DECISION_COLOR[schedule.decision]}` } }}
              >
                {DECISION_LABEL[schedule.decision] ?? schedule.decision}
              </Badge>
            )}
          </Group>
        )
      }
    >
      {task && (
        <Stack gap="md">
          <div>
            <Text fw={500}>{task.summary}</Text>
            <Text size="sm" c="dimmed">
              {task.prodf_id} · {task.team_id} · {fmtSp(task.estimation_sp)} SP
            </Text>
          </div>

          {schedule?.reason_text && (
            <Stack gap={4}>
              <Text size="sm" fw={500}>
                Почему это решение
              </Text>
              <Text size="sm">{schedule.reason_text}</Text>
            </Stack>
          )}

          {board && (
            <Stack gap={4}>
              <Text size="sm" fw={500}>
                Часы
              </Text>
              <Text size="sm" c="dimmed">
                Остаток: {fmtHours(board.remaining_hh)}
                {board.estimate_disputed && (
                  <>
                    {' '}
                    · оценка спорная: столбец матрицы {board.estimated_hh_effective} ЧЧ, в
                    исходнике {board.estimated_hh_declared} ЧЧ
                  </>
                )}
              </Text>
            </Stack>
          )}

          {assignments.length > 0 && (
            <Stack gap={4}>
              <Text size="sm" fw={500}>
                Кем закрыто
              </Text>
              <Table.ScrollContainer minWidth={360}>
                <Table verticalSpacing={4} fz="sm">
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Спринт</Table.Th>
                      <Table.Th>Инженер</Table.Th>
                      <Table.Th>Роль</Table.Th>
                      <Table.Th>Часы</Table.Th>
                      <Table.Th>Заём</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {assignments
                      .slice()
                      .sort((a, b) => a.sprint_no - b.sprint_no)
                      .map((a, i) => (
                        <Table.Tr key={i}>
                          <Table.Td className="mono">{a.sprint_no}</Table.Td>
                          <Table.Td className="mono">{a.engineer_id}</Table.Td>
                          <Table.Td>{a.served_role}</Table.Td>
                          <Table.Td className="tabular">{a.hours}</Table.Td>
                          <Table.Td>
                            {a.is_loan ? (
                              <Text size="xs" c="var(--wax-text)">
                                {a.home_team_id} → {a.serving_team_id}
                              </Text>
                            ) : (
                              '—'
                            )}
                          </Table.Td>
                        </Table.Tr>
                      ))}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            </Stack>
          )}

          {schedule?.reason_details && typeof schedule.reason_details === 'object' && (
            <ReasonDetails details={schedule.reason_details as Record<string, unknown>} />
          )}
        </Stack>
      )}
    </Drawer>
  )
}

function ReasonDetails({ details }: { details: Record<string, unknown> }) {
  const shortages = details.shortages
  const missingRoles = details.missing_roles
  const spBySprint = details.sp_by_sprint as Record<string, string> | undefined

  return (
    <Stack gap={8}>
      {Array.isArray(missingRoles) && missingRoles.length > 0 && (
        <Stack gap={2}>
          <Text size="sm" fw={500}>
            Роли, которых нет в штате
          </Text>
          <List size="sm">
            {missingRoles.map((m: { role: string; hours: string }, i: number) => (
              <List.Item key={i}>
                {m.role} — {fmtHours(m.hours)}
              </List.Item>
            ))}
          </List>
        </Stack>
      )}
      {Array.isArray(shortages) && shortages.length > 0 && (
        <Stack gap={2}>
          <Text size="sm" fw={500}>
            Не хватило часов
          </Text>
          <List size="sm">
            {shortages.map((s: { role: string; need_hh: string; free_hh: string }, i: number) => (
              <List.Item key={i}>
                {s.role}: нужно {fmtHours(s.need_hh)}, свободно {fmtHours(s.free_hh)}
              </List.Item>
            ))}
          </List>
        </Stack>
      )}
      {spBySprint && Object.keys(spBySprint).length > 1 && (
        <Stack gap={2}>
          <Text size="sm" fw={500}>
            Story Points по спринтам
          </Text>
          <Text size="sm" className="tabular">
            {Object.entries(spBySprint)
              .map(([sprint, sp]) => `спринт ${sprint}: ${isNegative(sp) ? sp : sp} SP`)
              .join(' · ')}
          </Text>
        </Stack>
      )}
    </Stack>
  )
}
