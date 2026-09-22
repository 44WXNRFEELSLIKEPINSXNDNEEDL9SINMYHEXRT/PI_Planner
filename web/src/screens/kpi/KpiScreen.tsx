import { Alert, Badge, Group, Paper, ScrollArea, SimpleGrid, Skeleton, Stack, Text, Title } from '@mantine/core'
import { num } from '../../api/wire'
import { AsOfLabel } from '../../components/common/AsOfLabel'
import { KpiStamp, toneOf } from '../../components/common/KpiStamp'
import { QueryError } from '../../components/common/QueryError'
import { sprintGridTemplate } from '../../components/common/sprintGrid'
import { useRun } from '../../hooks/useRun'
import { useKpiSnapshots } from '../../hooks/useViews'
import type {
  BusFactorKpiDetails,
  KpiSnapshotRow,
  PiPredictabilityDetails,
  SayDoDetails,
} from '../../types/views'

export function KpiScreen() {
  const { runId } = useRun()
  const query = useKpiSnapshots(runId)

  if (query.isPending) {
    return (
      <Stack gap="md" maw={1200}>
        <Title order={2}>KPI</Title>
        <Skeleton height={230} />
        <Skeleton height={250} />
      </Stack>
    )
  }

  if (query.error) {
    return (
      <Stack gap="md" maw={1200}>
        <Title order={2}>KPI</Title>
        <QueryError error={query.error} title="Не удалось загрузить KPI" />
      </Stack>
    )
  }

  const rows = query.data?.items ?? []
  const predictability = rows.filter((row) => row.kpi_code === 'pi_predictability')
  const sayDo = rows
    .filter((row) => row.kpi_code === 'say_do_ratio')
    .sort((a, b) => a.sprint_no - b.sprint_no)
  const busFactor = rows.find((row) => row.kpi_code === 'bus_factor')

  return (
    <Stack gap="lg" maw={1200}>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <div>
          <Title order={2}>KPI</Title>
          <Text c="dimmed" size="sm" mt={2}>
            Факт и прогноз показаны раздельно. Нормы получены вместе со значениями выбранного прогона.
          </Text>
        </div>
        <AsOfLabel iso={query.data?.as_of ?? null} />
      </Group>

      {rows.length === 0 ? (
        <Alert color="yellow" title="KPI ещё не рассчитаны">
          Для выбранного прогона нет снимков показателей.
        </Alert>
      ) : (
        <>
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
            <Predictability rows={predictability} />
            <BusFactor row={busFactor} />
          </SimpleGrid>
          <SayDo rows={sayDo} />
          <Alert color="yellow" variant="light" title="Как читать красные значения">
            Показатель по определению жёсткий: его нужно смотреть вместе с числом задач в квартале и списком ролей без людей.
          </Alert>
        </>
      )}
    </Stack>
  )
}

function Predictability({ rows }: { rows: KpiSnapshotRow[] }) {
  const actual = rows.find((row) => row.kind === 'actual')
  const forecast = rows.find((row) => row.kind === 'forecast')
  const norm = actual ?? forecast
  const details = (actual?.details ?? forecast?.details) as unknown as PiPredictabilityDetails | undefined

  return (
    <Section title="Выполнение квартала" note="Внешнее кольцо — прогноз, внутренняя дуга — факт.">
      {norm ? (
        <Group align="center" justify="space-around" wrap="wrap">
          <KpiStamp
            title="PI Predictability"
            forecast={forecast ? num(forecast.value) : null}
            actual={actual ? num(actual.value) : null}
            targetMin={norm.target_min === null ? null : num(norm.target_min)}
            targetMax={norm.target_max === null ? null : num(norm.target_max)}
            caption={details?.note}
          />
          <Stack gap={6} maw={290}>
            <MetricLine label="Факт" row={actual} />
            <MetricLine label="Прогноз" row={forecast} />
            {details?.committed_n !== undefined && (
              <Text size="sm">Инициатив в обязательстве: {details.committed_n}</Text>
            )}
            {details?.formula && <Text size="xs" c="dimmed">{details.formula}</Text>}
          </Stack>
        </Group>
      ) : (
        <Text size="sm" c="dimmed">Показатель отсутствует в этом прогоне.</Text>
      )}
    </Section>
  )
}

function SayDo({ rows }: { rows: KpiSnapshotRow[] }) {
  return (
    <Section
      title="Выполнение плана спринта"
      note="Значение выше шкалы рисует полное кольцо; число в центре всегда остаётся точным."
    >
      {rows.length === 0 ? (
        <Text size="sm" c="dimmed">Say/Do Ratio отсутствует в этом прогоне.</Text>
      ) : (
        <ScrollArea type="auto" offsetScrollbars>
          <div style={{ minWidth: Math.max(720, rows.length * 150) }}>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: sprintGridTemplate(rows.length, '0px', '0px'),
                alignItems: 'start',
              }}
            >
              <div />
              {rows.map((row) => {
                const details = row.details as unknown as SayDoDetails
                return (
                  <Stack key={`${row.sprint_no}-${row.kind}`} align="center" gap={4}>
                    <Badge variant="light" color={row.kind === 'actual' ? 'teal' : 'gray'}>
                      спринт {row.sprint_no} · {row.kind === 'actual' ? 'факт' : 'прогноз'}
                    </Badge>
                    <KpiStamp
                      title="Say/Do"
                      forecast={row.kind === 'forecast' ? num(row.value) : null}
                      actual={row.kind === 'actual' ? num(row.value) : null}
                      targetMin={row.target_min === null ? null : num(row.target_min)}
                      targetMax={row.target_max === null ? null : num(row.target_max)}
                      domainMax={row.target_max === null ? 100 : num(row.target_max)}
                      caption={`${details.done_sp ?? '—'} из ${details.planned_sp ?? '—'} SP`}
                    />
                    {details.note && <Text size="xs" c="dimmed" ta="center" maw={180}>{details.note}</Text>}
                  </Stack>
                )
              })}
              <div />
            </div>
          </div>
        </ScrollArea>
      )}
    </Section>
  )
}

function BusFactor({ row }: { row?: KpiSnapshotRow }) {
  if (!row) {
    return <Section title="Bus Factor"><Text size="sm" c="dimmed">Показатель отсутствует в этом прогоне.</Text></Section>
  }
  const value = num(row.value)
  const min = row.target_min === null ? null : num(row.target_min)
  const max = row.target_max === null ? null : num(row.target_max)
  const details = row.details as unknown as BusFactorKpiDetails
  const tone = toneOf(value, min, max)
  const color = tone === 'ok' ? 'teal' : tone === 'warning' ? 'yellow' : 'red'

  return (
    <Section title="Незаменимость" note="Bus Factor — число носителей компетенции, не процент.">
      <Group align="flex-start" wrap="nowrap">
        <Stack gap={0} miw={120}>
          <Text className="mono" fz={54} lh={1} c={color}>{value}</Text>
          <Text size="sm" fw={500}>норма {min === null ? '—' : `≥ ${min}`}</Text>
          <Badge color={color} variant="light" mt="xs">{row.kind === 'actual' ? 'факт' : 'прогноз'}</Badge>
        </Stack>
        <Stack gap={6}>
          {details.method && <Text size="sm">{details.method}</Text>}
          <Text size="sm">Компетенций: {details.competencies_n ?? '—'}, с одним носителем: {details.single_holder_n ?? '—'}.</Text>
          {details.critical?.length > 0 && <Text size="xs" c="dimmed">Критические: {details.critical.join(', ')}</Text>}
          {details.roles_without_staff?.length > 0 && <Text size="xs" c="dimmed">Нет в штате: {details.roles_without_staff.join(', ')}</Text>}
          {details.note && <Text size="xs" c="dimmed">{details.note}</Text>}
        </Stack>
      </Group>
    </Section>
  )
}

function MetricLine({ label, row }: { label: string; row?: KpiSnapshotRow }) {
  return (
    <Group justify="space-between" gap="xl" wrap="nowrap">
      <Text size="sm" c="dimmed">{label}</Text>
      <Text className="mono" fw={600}>{row ? `${num(row.value).toFixed(2)}%` : '—'}</Text>
    </Group>
  )
}

function Section({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <Paper withBorder p="md" h="100%">
      <Stack gap="md">
        <div>
          <Title order={3}>{title}</Title>
          {note && <Text size="xs" c="dimmed" mt={2}>{note}</Text>}
        </div>
        {children}
      </Stack>
    </Paper>
  )
}
