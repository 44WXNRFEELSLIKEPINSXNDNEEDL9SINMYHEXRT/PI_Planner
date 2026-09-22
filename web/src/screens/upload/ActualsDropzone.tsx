/**
 * Загрузка факта спринта: POST /api/actuals?sprint=N. Заменяет факт этого
 * спринта и все более поздние — предупреждение показывается, только если
 * повторная загрузка что-то реально отменяет.
 */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Dropzone } from '@mantine/dropzone'
import { Alert, Anchor, Group, List, Paper, Skeleton, Stack, Text, Title } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useActualUploads, useSprints } from '../../hooks/useViews'
import { templateUrl, uploadActuals } from '../../api/uploads'
import { ApiError } from '../../api/client'
import type { UploadErrorPayload } from '../../types/views'

export function ActualsDropzone({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [problems, setProblems] = useState<string[]>([])
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const sprints = useSprints()
  const uploads = useActualUploads()
  const sprintRows = sprints.data?.items ?? []
  const uploadRows = uploads.data?.items ?? []

  if (sprints.isPending || uploads.isPending) {
    return (
      <Paper p="lg" withBorder>
        <Skeleton height={140} />
      </Paper>
    )
  }

  if (sprints.isError || !sprintRows.length) {
    return (
      <Paper p="lg" withBorder>
        <Stack gap={4}>
          <Title order={3}>Факт спринта</Title>
          <Text size="sm" c="dimmed">
            Сначала загрузите датасет — без него не с чем сравнивать факт.
          </Text>
        </Stack>
      </Paper>
    )
  }

  const sprintCount = sprintRows.length
  const lastUploaded = Math.max(0, ...uploadRows.map((u) => u.sprint_no))
  const nextSprint = Math.min(lastUploaded + 1, sprintCount)
  const quarterClosed = lastUploaded >= sprintCount

  async function handleFile(file: File) {
    setBusy(true)
    setProblems([])
    setErrorMessage(null)
    try {
      const result = await uploadActuals(file, nextSprint)
      await queryClient.invalidateQueries()
      notifications.show({
        color: 'teal',
        title: `Факт спринта ${nextSprint} загружен`,
        message: `План пересчитан: ${result.plan.in_quarter} задач в квартале, ${result.plan.alerts} алертов`,
      })
      onDone()
    } catch (err) {
      if (err instanceof ApiError && err.body) {
        const body = err.body as Partial<UploadErrorPayload>
        setErrorMessage(body.message ?? err.message)
        setProblems(body.problems ?? [])
      } else {
        setErrorMessage(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Paper p="lg" withBorder>
      <Stack gap="sm">
        <Title order={3}>Факт спринта</Title>
        {quarterClosed ? (
          <Text size="sm" c="dimmed">
            Факт загружен за все {sprintCount} спринтов квартала. Новый цикл начинается с
            загрузки следующего датасета.
          </Text>
        ) : (
          <>
            <Group justify="space-between" wrap="wrap">
              <Text size="sm">
                Следующий: <b className="mono">спринт {nextSprint}</b> из {sprintCount}
              </Text>
              <Anchor href={templateUrl(nextSprint)} download size="sm">
                Скачать шаблон
              </Anchor>
            </Group>
            <Dropzone
              onDrop={(files) => files[0] && handleFile(files[0])}
              accept={['text/csv', '.csv', '.xlsx']}
              maxFiles={1}
              loading={busy}
              multiple={false}
            >
              <Stack align="center" gap={4} py="md" style={{ pointerEvents: 'none' }}>
                <Text fw={500}>Перетащите файл .csv или .xlsx сюда или нажмите</Text>
                <Text size="sm" c="dimmed">
                  заполненный шаблон факта спринта {nextSprint}
                </Text>
              </Stack>
            </Dropzone>
            {lastUploaded >= nextSprint && (
              <Text size="xs" c="var(--wax-text)">
                Загрузка заменит факт спринта {nextSprint} и все более поздние.
              </Text>
            )}
          </>
        )}
        {errorMessage && (
          <Alert color="red" variant="light" title="Факт не принят">
            <Text size="sm">{errorMessage}</Text>
            {problems.length > 0 && (
              <List size="sm" mt={6}>
                {problems.map((p, i) => (
                  <List.Item key={i}>{p}</List.Item>
                ))}
              </List>
            )}
          </Alert>
        )}
      </Stack>
    </Paper>
  )
}
