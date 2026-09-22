/**
 * Факт спринта против плана, который действовал в этом спринте
 * (`v_sprint_deviation`, docs/UI_SPEC.md §8.4). `deviation` — готовая строка
 * из витрины: «в срок», «не закрыта в срок», «раньше плана». Плановые и
 * фактические часы показаны рядом, чтобы отклонение можно было проверить руками.
 */
import { Table, Text } from '@mantine/core'
import { RISK_COLOR } from '../../components/common/RiskRail'
import { deviationTone } from './labels'
import { fmtHours, fmtSp } from '../../api/wire'
import type { SprintDeviationRow } from '../../types/views'

export function SprintDeviation({ rows }: { rows: SprintDeviationRow[] }) {
  if (rows.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        Факт ещё не загружали — сравнивать не с чем. Отклонения появятся после первой загрузки
        факта спринта.
      </Text>
    )
  }

  return (
    <Table.ScrollContainer minWidth={760}>
      <Table verticalSpacing={4} fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Спринт</Table.Th>
            <Table.Th>Задача</Table.Th>
            <Table.Th>Команда</Table.Th>
            <Table.Th>SP</Table.Th>
            <Table.Th>План спринтов</Table.Th>
            <Table.Th>Статус по факту</Table.Th>
            <Table.Th>План ЧЧ</Table.Th>
            <Table.Th>Факт ЧЧ</Table.Th>
            <Table.Th>Отклонение</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => (
            <Table.Tr key={`${row.upload_id}-${row.task_id}`}>
              <Table.Td className="mono">{row.sprint_no}</Table.Td>
              <Table.Td className="mono">{row.task_id}</Table.Td>
              <Table.Td>{row.team_id}</Table.Td>
              <Table.Td className="tabular">{fmtSp(row.estimation_sp)}</Table.Td>
              <Table.Td className="tabular">
                {row.planned_start !== null && row.planned_end !== null
                  ? `${row.planned_start}–${row.planned_end}`
                  : '—'}
              </Table.Td>
              <Table.Td>{row.reported_status ?? '—'}</Table.Td>
              <Table.Td className="tabular">{fmtHours(row.planned_hours)}</Table.Td>
              <Table.Td className="tabular">{fmtHours(row.spent_hours)}</Table.Td>
              <Table.Td>
                <Text size="xs" fw={500} style={{ color: RISK_COLOR[deviationTone(row.deviation)] }}>
                  {row.deviation}
                </Text>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  )
}
