/**
 * KPI (docs/UI_DESIGN.md §5.5, docs/UI_SPEC.md §2.4, §8.3).
 * - Прогноз и факт — РАЗНЫЕ строки (`kind`), показываются рядом: это прямое
 *   требование ТЗ.
 * - Нормы берутся только из строки (`target_min`/`target_max`), не зашиваются.
 * - Bus Factor — счётчик, а не процент: отдельная плашка-число.
 * - `details.note` и `details.method` — готовый русский текст, как есть.
 */
import { Group, Paper, ScrollArea, Skeleton, Stack, Text, Title } from '@mantine/core'
import { useRun } from '../../hooks/useRun'
import { useKpiSnapshots, useSprints } from '../../hooks/useViews'
import { QueryError, anyPending, firstError } from '../../components/common/QueryError'
import { AsOfLabel } from '../../components/common/AsOfLabel'
import { KpiStamp, toneOf } from '../../components/common/KpiStamp'
import { sprintGridTemplate } from '../../components/common/sprintGrid'
import { RISK_COLOR } from '../../components/common/RiskRail'
import { fmtDateShort, fmtSp, num } from '../../api/wire'
import type {
  BusFactorKpiDetails,
  KpiSnapshotRow,
  PiPredictabilityDetails,
  SayDoDetails,
} from '../../types/views'

const n = (v: string | null): number | null => (v === null ? null : num(v))

export function KpiScreen() {
  const { runId } = useRun()
  const kpiQ = useKpiSnapshots(runId)
  const sprintsQ = useSprints()
  const queries = [kpiQ, sprintsQ]
  const error = firstError(queries)

  if (anyPending(queries)) {
    return (
      <Stack gap="md" maw={1200}>
        <Title order={2}>KPI</Title>
        <Skeleton height={220} />
        <Skeleton height={220} />
      </Stack>
    )
  }
  if (error) {
    return (
      <Stack gap="md" maw={1200}>
        <Title order={2}>KPI</Title>
        <QueryError error={error} title="Не удалось загрузить KPI" />
      </Stack>
    )
  }

  const rows = kpiQ.data?.items ?? []
  const sprints = sprintsQ.data?.items ?? []
  const pred = rows.filter((r) => r.kpi_code === 'pi_predictability')
  const sayDo = rows.filter((r) => r.kpi_code === 'say_do_ratio')
  const bus = rows.find((r) => r.kpi_code === 'bus_factor') ?? null

  return (
    <Stack gap="lg" maw={1200}>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <div>
          <Title order={2}>KPI</Title>
          <Text c="dimmed" size="sm" mt={2} maw={760}>
            Тонкое кольцо штемпеля — прогноз по текущему плану, залитая дуга — факт по загруженным спринтам.
            Засечки на круге — границы нормы. Показатели по определению жёсткие: смотреть их вместе с числом задач в
            квартале и списком ролей без людей в штате.
          </Text>
        </div>
        <AsOfLabel iso={kpiQ.data?.as_of ?? null} />
      </Group>

      {rows.length === 0 ? (
        <Paper withBorder p="md">
          <Text size="sm" c="dimmed">
            В этом прогоне KPI не рассчитаны. Загрузите датасет — базовый план посчитает прогноз.
          </Text>
        </Paper>
      ) : (
        <>
          <PredictabilityBlock rows={pred} />
          <SayDoBlock rows={sayDo} sprints={sprints} />
          {bus && <BusFactorBlock row={bus} />}
        </>
      )}
    </Stack>
  )
}

// ------------------------------------------------ процент выполнения квартала
function PredictabilityBlock({ rows }: { rows: KpiSnapshotRow[] }) {
  const forecast = rows.find((r) => r.kind === 'forecast')
  const actual = rows.find((r) => r.kind === 'actual')
  const any = actual ?? forecast
  if (!any) return null
  const fd = forecast?.details as unknown as PiPredictabilityDetails | undefined
  const ad = actual?.details as unknown as PiPredictabilityDetails | undefined
  const d = ad ?? fd

  return (
    <Paper withBorder p="md">
      <Title order={3}>Процент выполнения квартального плана</Title>
      <Text size="xs" c="dimmed" mt={2} mb="md">
        {d?.formula}
      </Text>
      <Group align="flex-start" gap="xl" wrap="wrap">
        <KpiStamp
          title="Инициативы квартала"
          forecast={forecast ? num(forecast.value) : null}
          actual={actual ? num(actual.value) : null}
          targetMin={n(any.target_min)}
          targetMax={n(any.target_max)}
          caption={`норма ${normText(any)}`}
        />
        <Stack gap="sm" style={{ flex: 1, minWidth: 260 }} maw={640}>
          <Pair label="Прогноз" row={forecast} />
          <Pair label="Факт" row={actual} emptyText="Факт появится после загрузки первого спринта." />
          {d && (
            <Stack gap={4}>
              <Text size="sm">
                Первоначальный план обещал завершить <b className="mono">{d.committed_n}</b>{' '}
                {plural(d.committed_n, 'инициативу', 'инициативы', 'инициатив')}:{' '}
                <span className="mono">{d.committed_initiatives.join(', ') || '—'}</span>.
              </Text>
              {ad?.completed_initiatives && (
                <Text size="sm">
                  Завершены по факту: <span className="mono">{ad.completed_initiatives.join(', ') || 'пока ни одна'}</span>.
                </Text>
              )}
              {fd?.on_track_initiatives && (
                <Text size="sm">
                  По плану успевают: <span className="mono">{fd.on_track_initiatives.join(', ') || 'ни одна'}</span>.
                </Text>
              )}
              {d.partial_initiatives.length > 0 && (
                <Text size="sm" c="dimmed">
                  Частично в плане, в знаменатель не входят: <span className="mono">{d.partial_initiatives.join(', ')}</span>.
                </Text>
              )}
            </Stack>
          )}
        </Stack>
      </Group>
    </Paper>
  )
}

function Pair({ label, row, emptyText }: { label: string; row?: KpiSnapshotRow; emptyText?: string }) {
  if (!row) {
    return emptyText ? (
      <Text size="sm" c="dimmed">
        {label}: {emptyText}
      </Text>
    ) : null
  }
  const tone = toneOf(num(row.value), n(row.target_min), n(row.target_max))
  const note = (row.details as { note?: string } | null)?.note
  return (
    <div style={{ borderLeft: `4px solid ${RISK_COLOR[toneToRisk(tone)]}`, paddingLeft: 10 }}>
      <Text size="sm" component="div">
        {label}: <b className="mono">{num(row.value).toFixed(2)}%</b>{' '}
        <span style={{ color: RISK_COLOR[toneToRisk(tone)] }}>{TONE_WORD[tone]}</span>
      </Text>
      {note && (
        <Text size="xs" c="dimmed" component="div">
          {note}
        </Text>
      )}
    </div>
  )
}

// ------------------------------------------------- выполнение плана спринта
function SayDoBlock({ rows, sprints }: { rows: KpiSnapshotRow[]; sprints: { sprint_no: number; start_date: string; end_date: string }[] }) {
  if (rows.length === 0 || sprints.length === 0) return null
  const bySprint = new Map(rows.map((r) => [r.sprint_no, r]))
  const lastActual = [...rows].filter((r) => r.kind === 'actual').sort((a, b) => b.sprint_no - a.sprint_no)[0]
  const formula = (rows[0].details as unknown as SayDoDetails | null)?.formula
  // Шкала чуть выше верхней нормы, чтобы засечка 105% была видна; всё, что выше, — полное кольцо.
  const maxNorm = Math.max(...rows.map((r) => n(r.target_max) ?? 100))
  const domainMax = Math.ceil((maxNorm * 1.15) / 10) * 10

  return (
    <Paper withBorder p="md">
      <Title order={3}>Выполнение плана спринта</Title>
      <Text size="xs" c="dimmed" mt={2} mb="md">
        {formula}. {lastActual ? `Факт загружен по спринт ${lastActual.sprint_no}, дальше — прогноз.` : 'Факта пока нет — всё прогноз.'}{' '}
        Значение выше шкалы рисует полное кольцо, число в центре — точное.
      </Text>
      <ScrollArea type="auto">
        <div style={{ display: 'grid', gridTemplateColumns: sprintGridTemplate(sprints.length, '110px', '0px'), minWidth: 900 }}>
          <Cell head>Спринт</Cell>
          {sprints.map((s) => (
            <Cell key={s.sprint_no} head center>
              <div className="mono">{s.sprint_no}</div>
              <Text size="10px" c="dimmed">
                {fmtDateShort(s.start_date)}–{fmtDateShort(s.end_date)}
              </Text>
            </Cell>
          ))}
          <div />

          <Cell>
            <Text size="xs" c="dimmed">
              прогноз / факт
            </Text>
          </Cell>
          {sprints.map((s) => {
            const r = bySprint.get(s.sprint_no)
            if (!r) return <Cell key={s.sprint_no} center>—</Cell>
            const d = r.details as unknown as SayDoDetails
            const v = num(r.value)
            return (
              <Cell key={s.sprint_no} center>
                <div style={{ transform: 'scale(0.82)', transformOrigin: 'top center', height: 150 }}>
                  <KpiStamp
                    title={r.kind === 'actual' ? 'факт' : 'прогноз'}
                    forecast={r.kind === 'forecast' ? v : null}
                    actual={r.kind === 'actual' ? v : null}
                    targetMin={n(r.target_min)}
                    targetMax={n(r.target_max)}
                    domainMax={domainMax}
                  />
                </div>
                <Text size="xs" className="mono tabular">
                  {fmtSp(d.done_sp)} из {fmtSp(d.planned_sp)} SP
                </Text>
                {num(d.planned_sp) === 0 && (
                  <Text size="10px" c="dimmed">
                    план был пуст
                  </Text>
                )}
              </Cell>
            )
          })}
          <div />
        </div>
      </ScrollArea>
    </Paper>
  )
}

function Cell({ children, head, center }: { children?: React.ReactNode; head?: boolean; center?: boolean }) {
  return (
    <div
      style={{
        padding: '8px 6px',
        borderBottom: head ? '2px solid var(--ink)' : '1px solid var(--line)',
        textAlign: center ? 'center' : 'left',
        fontSize: 12,
        fontWeight: head ? 500 : 400,
        color: head ? 'var(--muted)' : undefined,
      }}
    >
      {children}
    </div>
  )
}

// ---------------------------------------------------------------- Bus Factor
function BusFactorBlock({ row }: { row: KpiSnapshotRow }) {
  const d = row.details as unknown as BusFactorKpiDetails
  const value = num(row.value)
  const min = n(row.target_min)
  const tone = toneOf(value, min, n(row.target_max))
  const color = RISK_COLOR[toneToRisk(tone)]
  return (
    <Paper withBorder p="md">
      <Title order={3}>Bus Factor</Title>
      <Text size="xs" c="dimmed" mt={2} mb="md">
        Счётчик людей, а не процент. Подробная карта — на экране «Звёздная карта».
      </Text>
      <Group align="flex-start" gap="xl" wrap="wrap">
        <div style={{ borderLeft: `4px solid ${color}`, paddingLeft: 12, minWidth: 150 }}>
          <Text className="mono" style={{ fontSize: 44, lineHeight: 1, fontWeight: 600 }}>
            {fmtSp(value)}
          </Text>
          <Text size="sm" mt={6} style={{ color }}>
            {TONE_WORD[tone]}
          </Text>
          <Text size="xs" c="dimmed">
            норма {normText(row, '')}
          </Text>
        </div>
        <Stack gap={6} style={{ flex: 1, minWidth: 260 }} maw={720}>
          <Text size="sm">Как считается: {d.method}.</Text>
          <Text size="sm">
            Компетенций <b className="mono">{d.competencies_n}</b>, с одним носителем{' '}
            <b className="mono">{d.single_holder_n}</b>, критичных <b className="mono">{d.critical_n}</b>.
          </Text>
          {d.roles_without_staff.length > 0 && (
            <Text size="sm">
              Ролей без людей в штате: <b className="mono">{d.roles_without_staff.length}</b> ({d.roles_without_staff.join(', ')}).
            </Text>
          )}
          {d.critical.length > 0 && (
            <details>
              <summary style={{ cursor: 'pointer', fontSize: 14 }}>Критичные компетенции ({d.critical.length})</summary>
              <Text size="sm" mt={6}>
                {d.critical.join(', ')}
              </Text>
            </details>
          )}
          <Text size="xs" c="dimmed">
            {d.note}
          </Text>
        </Stack>
      </Group>
    </Paper>
  )
}

// ------------------------------------------------------------------ helpers
type Tone = ReturnType<typeof toneOf>
const TONE_WORD: Record<Tone, string> = { ok: 'в норме', warning: 'выше нормы', critical: 'ниже нормы' }
function toneToRisk(t: Tone): Tone {
  return t
}

function normText(row: KpiSnapshotRow, unit = '%'): string {
  const lo = n(row.target_min)
  const hi = n(row.target_max)
  if (lo !== null && hi !== null) return `${fmtSp(lo)}–${fmtSp(hi)}${unit}`
  if (lo !== null) return `не ниже ${fmtSp(lo)}${unit}`
  if (hi !== null) return `не выше ${fmtSp(hi)}${unit}`
  return 'не задана'
}

function plural(k: number, one: string, few: string, many: string): string {
  const m10 = k % 10
  const m100 = k % 100
  if (m10 === 1 && m100 !== 11) return one
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few
  return many
}
