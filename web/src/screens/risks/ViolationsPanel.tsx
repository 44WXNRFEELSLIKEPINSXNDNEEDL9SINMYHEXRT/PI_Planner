/**
 * Предупреждения приёмки контракта — `v_plan_violations` (docs/UI_SPEC.md §2.3).
 * Пусто = зелёная плашка «предупреждений нет», а не пустое место и не ошибка
 * загрузки. `PLANNED_END_OVERSAIL` выглядит как срыв, но им не является: даты в
 * исходнике — история, план по ним не строился (ADR-016). Это нужно сказать
 * словами, иначе с цифрами будут спорить (docs/UI_SPEC.md §4).
 */
import { Alert, Group, Stack, Table, Text } from '@mantine/core'
import type { PlanViolationRow } from '../../types/views'

const CHECK_EXPLAINED: Record<string, string> = {
  PLANNED_END_OVERSAIL:
    'прогноз позже исходного срока; срок в исходнике — факт истории, а не обещание (ADR-016)',
}

export function ViolationsPanel({ rows }: { rows: PlanViolationRow[] }) {
  if (rows.length === 0) {
    return (
      <Alert color="teal" variant="light" title="Предупреждений нет">
        Контракт планировщика выполнен целиком: ни одна проверка приёмки не сработала.
      </Alert>
    )
  }

  const errors = rows.filter((r) => r.severity === 'error')
  const codes = [...new Set(rows.map((r) => r.check_code))]

  return (
    <Stack gap="sm">
      <Alert
        color={errors.length > 0 ? 'red' : 'yellow'}
        variant="light"
        title={errors.length > 0 ? `Ошибок контракта: ${errors.length}` : 'Ошибок контракта нет'}
      >
        <Text size="sm">
          Предупреждений: {rows.length - errors.length}. Это не срыв плана — приёмка сообщает, что
          прогноз расходится со сроками из исходного файла.
        </Text>
        {codes.some((c) => CHECK_EXPLAINED[c]) && (
          <Stack gap={2} mt={6}>
            {codes
              .filter((c) => CHECK_EXPLAINED[c])
              .map((c) => (
                <Text key={c} size="xs" c="dimmed">
                  <span className="mono">{c}</span> — {CHECK_EXPLAINED[c]}
                </Text>
              ))}
          </Stack>
        )}
      </Alert>

      <Table.ScrollContainer minWidth={520}>
        <Table verticalSpacing={4} fz="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Проверка</Table.Th>
              <Table.Th>Уровень</Table.Th>
              <Table.Th>Сущность</Table.Th>
              <Table.Th>Подробность</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {rows.map((row) => (
              <Table.Tr key={`${row.check_code}-${row.entity}`}>
                <Table.Td className="mono">{row.check_code}</Table.Td>
                <Table.Td>
                  <Group gap={6} wrap="nowrap">
                    <span
                      aria-hidden
                      style={{
                        width: 8,
                        height: 8,
                        borderRadius: 4,
                        flexShrink: 0,
                        background: row.severity === 'error' ? 'var(--stamp)' : 'var(--wax)',
                      }}
                    />
                    <Text size="xs">{row.severity === 'error' ? 'ошибка' : 'предупреждение'}</Text>
                  </Group>
                </Table.Td>
                <Table.Td className="mono">{row.entity}</Table.Td>
                <Table.Td>{row.detail}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Stack>
  )
}
