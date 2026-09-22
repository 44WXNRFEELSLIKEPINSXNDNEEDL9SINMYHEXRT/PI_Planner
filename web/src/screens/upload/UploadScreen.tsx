import { SimpleGrid, Stack, Text, Title } from '@mantine/core'
import { DatasetDropzone } from './DatasetDropzone'
import { ActualsDropzone } from './ActualsDropzone'
import { UploadHistory } from './UploadHistory'

export function UploadScreen({ onOpenPlan }: { onOpenPlan: () => void }) {
  return (
    <Stack gap="xl" maw={1100}>
      <div>
        <Title order={2}>Загрузка данных</Title>
        <Text c="dimmed" mt={4}>
          Загрузите выданный датасет — план построится автоматически. Раз в две
          недели загружайте факт закрытого спринта: сервис пересчитает
          остаток плана.
        </Text>
      </div>

      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg">
        <DatasetDropzone onDone={onOpenPlan} />
        <ActualsDropzone onDone={onOpenPlan} />
      </SimpleGrid>

      <UploadHistory />
    </Stack>
  )
}
