/**
 * Экран рисков (docs/UI_DESIGN.md §5.3, docs/UI_SPEC.md §2.3 и §8.4).
 * Здесь четыре разные вещи, и смешивать их нельзя:
 *  - `alerts` — что под угрозой в этом прогоне;
 *  - `v_plan_diff` — что изменилось с прошлого прогона и почему;
 *  - `v_sprint_deviation` — как факт разошёлся с планом своего спринта;
 *  - `v_plan_violations` — предупреждения приёмки контракта (пусто = зелёная
 *    плашка, а не ошибка).
 * Сообщения и объяснения приходят из базы готовым русским текстом и
 * показываются как есть.
 */
import { useState } from 'react'
import { Group, Paper, Skeleton, Stack, Text, Title } from '@mantine/core'
import { useRun } from '../../hooks/useRun'
import {
  useAlerts,
  usePlanDiff,
  usePlanViolations,
  useRefDecisionReasons,
  useSprintDeviation,
  useSprints,
} from '../../hooks/useViews'
import { QueryError, anyPending, firstError } from '../../components/common/QueryError'
import { AsOfLabel } from '../../components/common/AsOfLabel'
import { fmtDateShort } from '../../api/wire'
import { RiskSpine } from './RiskSpine'
import { AlertCard } from './AlertCard'
import { PlanChanges } from './PlanChanges'
import { SprintDeviation } from './SprintDeviation'
import { ViolationsPanel } from './ViolationsPanel'
import { ALERT_WORD } from './labels'
import type { AlertRow, AlertType } from '../../types/views'

export function RisksScreen() {
  const { runId } = useRun()
  const [sprint, setSprint] = useState<number | null>(null)

  const alertsQ = useAlerts(runId)
  const diffQ = usePlanDiff(runId)
  const violationsQ = usePlanViolations(runId)
  const deviationQ = useSprintDeviation()
  const sprintsQ = useSprints()
  const reasonsQ = useRefDecisionReasons()

  const queries = [alertsQ, diffQ, violationsQ, deviationQ, sprintsQ]
  const error = firstError(queries)

  if (anyPending(queries)) {
    return (
      <Stack gap="md" maw={1200}>
        <Title order={2}>Риски</Title>
        <Skeleton height={96} />
        <Skeleton height={320} />
      </Stack>
    )
  }

  if (error) {
    return (
      <Stack gap="md" maw={1200}>
        <Title order={2}>Риски</Title>
        <QueryError error={error} title="Не удалось загрузить риски" />
      </Stack>
    )
  }

  const alerts = alertsQ.data?.items ?? []
  const sprints = sprintsQ.data?.items ?? []
  const reasonLabels = new Map((reasonsQ.data?.items ?? []).map((r) => [r.code, r.label]))
  const shown = sprint === null ? alerts : alerts.filter((a) => a.sprint_no === sprint)

  const tally = new Map<AlertType, number>()
  for (const a of alerts) tally.set(a.alert_type, (tally.get(a.alert_type) ?? 0) + 1)
  const summary = [...tally.entries()].map(([type, n]) => `${n} — ${ALERT_WORD[type]}`).join(', ')

  const known = new Set(sprints.map((s) => s.sprint_no))
  const groups = sprints
    .map((s) => ({ sprint: s, rows: shown.filter((a) => a.sprint_no === s.sprint_no) }))
    .filter((g) => g.rows.length > 0)
  const outside = shown.filter((a) => !known.has(a.sprint_no))

  return (
    <Stack gap="lg" maw={1200}>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <div>
          <Title order={2}>Риски</Title>
          <Text c="dimmed" size="sm" mt={2}>
            {alerts.length > 0
              ? `${alerts.length} ${plural(alerts.length)} в этом прогоне: ${summary}.`
              : 'Прогон не породил ни одного алерта.'}
          </Text>
        </div>
        <AsOfLabel iso={alertsQ.data?.as_of ?? null} />
      </Group>

      <Section
        title="Где в квартале риск"
        note="Те же шесть колонок, что на ганте: видно, на какой спринт приходится нагрузка. Клик по спринту отбирает ленту."
      >
        {sprints.length > 0 && alerts.length > 0 ? (
          <RiskSpine sprints={sprints} alerts={alerts} selected={sprint} onSelect={setSprint} />
        ) : (
          <Text size="sm" c="dimmed">
            {alerts.length === 0
              ? 'Рисков нет — план этого прогона выполним целиком.'
              : 'Календарь спринтов не загружен: загрузите датасет.'}
          </Text>
        )}
      </Section>

      <Section
        title="Лента рисков"
        note={
          sprint === null
            ? 'Группировка по спринтам. Уровень напечатан словом рядом с линейкой, а не только цветом.'
            : `Отобран спринт ${sprint}: показано ${shown.length} из ${alerts.length}.`
        }
      >
        {shown.length === 0 ? (
          <Text size="sm" c="dimmed">
            В этом отборе рисков нет.
          </Text>
        ) : (
          <Stack gap="md">
            {groups.map((group) => (
              <Stack key={group.sprint.sprint_no} gap={6}>
                <Group gap="xs" wrap="nowrap">
                  <Text size="sm" fw={500}>
                    Спринт {group.sprint.sprint_no}
                  </Text>
                  <Text size="xs" c="dimmed" className="mono">
                    {fmtDateShort(group.sprint.start_date)}–{fmtDateShort(group.sprint.end_date)}
                  </Text>
                  <Text size="xs" c="dimmed" ml="auto" className="tabular">
                    {group.rows.length}
                  </Text>
                </Group>
                {group.rows.map((alert) => (
                  <AlertFrame key={alert.alert_id} alert={alert} reasonLabels={reasonLabels} />
                ))}
              </Stack>
            ))}
            {outside.length > 0 && (
              <Stack gap={6}>
                <Text size="sm" fw={500}>
                  Вне календаря квартала
                </Text>
                {outside.map((alert) => (
                  <AlertFrame key={alert.alert_id} alert={alert} reasonLabels={reasonLabels} />
                ))}
              </Stack>
            )}
          </Stack>
        )}
      </Section>

      <Section
        title="Что изменилось и почему"
        note="Пересчёт после загрузки факта: какие задачи поехали и что именно их сдвинуло."
      >
        <PlanChanges rows={diffQ.data?.items ?? []} />
      </Section>

      <Section
        title="Факт против плана спринта"
        note="Факт сравнивается с планом, который действовал в том спринте, а не с текущим."
      >
        <SprintDeviation rows={deviationQ.data?.items ?? []} />
      </Section>

      <Section
        title="Предупреждения приёмки"
        note="Проверки контракта планировщика. Отсутствие ошибок означает, что план согласован с ограничениями."
      >
        <ViolationsPanel rows={violationsQ.data?.items ?? []} />
      </Section>
    </Stack>
  )
}

function AlertFrame({ alert, reasonLabels }: { alert: AlertRow; reasonLabels: Map<string, string> }) {
  return (
    <div
      style={{
        border: '1px solid var(--line)',
        background: 'var(--surface)',
        padding: '8px 10px',
      }}
    >
      <AlertCard alert={alert} reasonLabels={reasonLabels} />
    </div>
  )
}

function Section({
  title,
  note,
  children,
}: {
  title: string
  note?: string
  children: React.ReactNode
}) {
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <div>
          <Title order={3}>{title}</Title>
          {note && (
            <Text size="xs" c="dimmed" mt={2}>
              {note}
            </Text>
          )}
        </div>
        {children}
      </Stack>
    </Paper>
  )
}

function plural(n: number): string {
  const mod10 = n % 10
  const mod100 = n % 100
  if (mod10 === 1 && mod100 !== 11) return 'риск'
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'риска'
  return 'рисков'
}
