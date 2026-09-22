import { Stack, Table, Text, Title } from '@mantine/core'
import { useActualUploads } from '../../hooks/useViews'
import { QueryState } from '../../components/common/QueryState'
import { num } from '../../api/wire'

function formatUploadedAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function UploadHistory() {
  const query = useActualUploads()

  return (
    <Stack gap="sm">
      <Title order={3}>История загрузок факта</Title>
      <QueryState
        query={query}
        skeletonRows={2}
        isEmpty={(e) => e.items.length === 0}
        emptyTitle="Факт ещё не загружали"
        emptyBody="После загрузки датасета здесь появится история загрузок факта по спринтам."
      >
        {(envelope) => (
          <Table.ScrollContainer minWidth={480}>
            <Table verticalSpacing="xs">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Спринт</Table.Th>
                  <Table.Th>Загружен</Table.Th>
                  <Table.Th>Файл</Table.Th>
                  <Table.Th>Итог</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {envelope.items.map((row) => (
                  <Table.Tr key={row.upload_id}>
                    <Table.Td className="mono">{row.sprint_no}</Table.Td>
                    <Table.Td>{formatUploadedAt(row.uploaded_at)}</Table.Td>
                    <Table.Td>
                      <Text size="sm" className="mono">
                        {row.source_file}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">
                        {row.summary.done} выполнено · {row.summary.in_progress} в работе ·{' '}
                        {num(row.summary.hours).toFixed(0)} ЧЧ
                      </Text>
                      {row.summary.warnings.length > 0 && (
                        <Text size="xs" c="var(--wax-text)">
                          {row.summary.warnings.join('; ')}
                        </Text>
                      )}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </QueryState>
    </Stack>
  )
}
