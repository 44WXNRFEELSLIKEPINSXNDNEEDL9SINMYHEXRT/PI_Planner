/**
 * Загрузка датасета: POST /api/dataset. Стирает прежние прогоны и факт —
 * предупреждение об этом выведено прямо на карточке, а не спрятано в тексте.
 */
import { useState } from 'react'
import { Dropzone, MIME_TYPES } from '@mantine/dropzone'
import { Alert, List, Paper, Stack, Text, Title } from '@mantine/core'
import { useQueryClient } from '@tanstack/react-query'
import { notifications } from '@mantine/notifications'
import { uploadDataset } from '../../api/uploads'
import { ApiError } from '../../api/client'
import type { UploadErrorPayload } from '../../types/views'

export function DatasetDropzone({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [problems, setProblems] = useState<string[]>([])
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const queryClient = useQueryClient()

  async function handleFile(file: File) {
    setBusy(true)
    setProblems([])
    setErrorMessage(null)
    try {
      const result = await uploadDataset(file)
      await queryClient.invalidateQueries()
      notifications.show({
        color: 'teal',
        title: 'Датасет загружен',
        message: `${result.rows.tasks ?? '?'} задач, план построен: ${result.plan.in_quarter} в квартале, ${result.plan.not_in_quarter} перенесено`,
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
        <Title order={3}>Датасет</Title>
        <Text size="sm" c="dimmed">
          Загрузка начнёт новый цикл: прежние прогоны и загруженный факт будут
          стёрты, план построится заново.
        </Text>
        <Dropzone
          onDrop={(files) => files[0] && handleFile(files[0])}
          accept={[MIME_TYPES.xlsx]}
          maxFiles={1}
          loading={busy}
          multiple={false}
        >
          <Stack align="center" gap={4} py="md" style={{ pointerEvents: 'none' }}>
            <Text fw={500}>Перетащите файл .xlsx сюда или нажмите</Text>
            <Text size="sm" c="dimmed">
              выданный датасет ПочтаТеха
            </Text>
          </Stack>
        </Dropzone>
        {errorMessage && (
          <Alert color="red" variant="light" title="Датасет не принят">
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
