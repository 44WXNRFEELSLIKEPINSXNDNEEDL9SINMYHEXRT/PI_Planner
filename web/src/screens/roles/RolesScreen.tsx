import { useState } from 'react'
import { Badge, Group, Paper, ScrollArea, SimpleGrid, Skeleton, Stack, Switch, Table, Text, Title } from '@mantine/core'
import { fmtHours, fmtSp, isNegative } from '../../api/wire'
import { AsOfLabel } from '../../components/common/AsOfLabel'
import { QueryError, anyPending, firstError } from '../../components/common/QueryError'
import {
  useBusFactor,
  useDqSummary,
  useRoleCoverageOrg,
  useRoleDeficit,
  useRoleDeficitEffective,
  useTeamCapacitySp,
  useTeams,
} from '../../hooks/useViews'
import type { RoleDeficitEffectiveRow, RoleDeficitRow } from '../../types/views'

export function RolesScreen() {
  const [problemsOnly, setProblemsOnly] = useState(true)
  const deficitQ = useRoleDeficit()
  const effectiveQ = useRoleDeficitEffective()
  const coverageQ = useRoleCoverageOrg()
  const busQ = useBusFactor()
  const capacityQ = useTeamCapacitySp()
  const teamsQ = useTeams()
  const dqQ = useDqSummary()
  const queries = [deficitQ, effectiveQ, coverageQ, busQ, capacityQ, teamsQ, dqQ]
  const error = firstError(queries)

  if (anyPending(queries)) {
    return (
      <Stack gap="md" maw={1400}>
        <Title order={2}>Роли и ёмкость</Title>
        <Skeleton height={360} />
        <Skeleton height={260} />
      </Stack>
    )
  }

  if (error) {
    return (
      <Stack gap="md" maw={1400}>
        <Title order={2}>Роли и ёмкость</Title>
        <QueryError error={error} title="Не удалось загрузить роли и ёмкость" />
      </Stack>
    )
  }

  const deficits = deficitQ.data?.items ?? []
  const effective = effectiveQ.data?.items ?? []
  const shownDeficits = problemsOnly ? deficits.filter((row) => !isNegative(row.gap_hh)) : deficits
  const shownEffective = problemsOnly ? effective.filter((row) => !isNegative(row.gap_hh)) : effective
  const hiringNeeds = (coverageQ.data?.items ?? []).filter((row) => row.verdict.includes('НАЙМ'))
  const teamFocus = new Map((teamsQ.data?.items ?? []).map((row) => [row.team_id, row.focus_factor]))

  return (
    <Stack gap="lg" maw={1400}>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <div>
          <Title order={2}>Роли и ёмкость</Title>
          <Text c="dimmed" size="sm" mt={2}>
            Дефицит людей, риск незаменимости и доступная командная ёмкость — отдельные срезы.
          </Text>
        </div>
        <AsOfLabel iso={deficitQ.data?.as_of ?? null} />
      </Group>

      <Section
        title="Дефицит по командам и ролям"
        note="Две витрины показаны рядом. Сейчас цифры совпадают, потому что замещения ролей запрещены; при разрешении замещений они разойдутся."
        action={<Switch checked={problemsOnly} onChange={(event) => setProblemsOnly(event.currentTarget.checked)} label="Только проблемы" />}
      >
        <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
          <DeficitTable title="Штатное покрытие" rows={shownDeficits} />
          <EffectiveDeficitTable title="С учётом замещений" rows={shownEffective} />
        </SimpleGrid>
      </Section>

      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="lg">
        <Section title="Где нужен наём" note="Роли, спрос по которым некому закрывать в организации.">
          <ScrollArea type="auto">
            <Table striped highlightOnHover miw={580}>
              <Table.Thead><Table.Tr><Table.Th>Роль</Table.Th><Table.Th ta="right">Спрос</Table.Th><Table.Th ta="right">Людей</Table.Th><Table.Th>Вердикт</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>
                {hiringNeeds.map((row) => (
                  <Table.Tr key={row.role_name}>
                    <Table.Td>{row.role_name}</Table.Td>
                    <Table.Td ta="right" className="mono">{fmtHours(row.demand_hh)}</Table.Td>
                    <Table.Td ta="right" className="tabular">{row.people_incl_substitution}</Table.Td>
                    <Table.Td><Verdict text={row.verdict} problem={!isNegative(row.gap_hh)} /></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        </Section>

        <Section title="Незаменимость по ролям" note="Bus Factor роли: отсутствие роли в штате и единственный носитель — разные риски.">
          <ScrollArea type="auto">
            <Table striped highlightOnHover miw={540}>
              <Table.Thead><Table.Tr><Table.Th>Роль</Table.Th><Table.Th ta="right">BF</Table.Th><Table.Th ta="right">Спрос</Table.Th><Table.Th>Риск</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>
                {(busQ.data?.items ?? []).map((row) => (
                  <Table.Tr key={row.role_id}>
                    <Table.Td>{row.role_name}</Table.Td>
                    <Table.Td ta="right" className="mono">{row.bus_factor}</Table.Td>
                    <Table.Td ta="right" className="mono">{fmtHours(row.demand_hh)}</Table.Td>
                    <Table.Td><RiskBadge risk={row.risk} /></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        </Section>
      </SimpleGrid>

      <Section title="Ёмкость команд" note="Доступные SP рассчитаны из средней скорости и focus factor. История каждой команды — два наблюдения.">
        <ScrollArea type="auto">
          <Table striped highlightOnHover miw={720}>
            <Table.Thead><Table.Tr><Table.Th>Команда</Table.Th><Table.Th ta="right">История</Table.Th><Table.Th ta="right">Velocity</Table.Th><Table.Th ta="right">Focus</Table.Th><Table.Th ta="right">SP / спринт</Table.Th><Table.Th ta="right">SP / PI</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {(capacityQ.data?.items ?? []).map((row) => (
                <Table.Tr key={row.team_id}>
                  <Table.Td fw={500}>{row.team_id}</Table.Td>
                  <Table.Td ta="right" className="tabular">{row.history_points}</Table.Td>
                  <Table.Td ta="right" className="mono">{fmtSp(row.avg_velocity)}</Table.Td>
                  <Table.Td ta="right" className="mono">{fmtSp(teamFocus.get(row.team_id) ?? row.focus_factor)}</Table.Td>
                  <Table.Td ta="right" className="mono">{fmtSp(row.available_sp_per_sprint)}</Table.Td>
                  <Table.Td ta="right" className="mono">{fmtSp(row.available_sp_per_pi)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </Section>

      <Section title="Диагностика исходных данных" note="Порядок и готовые пояснения приходят из диагностической витрины.">
        <Stack gap={0}>
          {(dqQ.data?.items ?? []).map((row) => (
            <Group key={row.rule_code} py="sm" gap="md" wrap="nowrap" style={{ borderBottom: '1px solid var(--line)' }}>
              <Badge color={row.severity === 'error' ? 'red' : row.severity === 'warning' ? 'yellow' : 'gray'} variant="light">
                {row.severity}
              </Badge>
              <div style={{ flex: 1, minWidth: 0 }}>
                <Text size="sm" fw={500} className="mono">{row.rule_code}</Text>
                {row.example && <Text size="xs" c="dimmed">{row.example}</Text>}
              </div>
              <Text className="mono" fw={600}>{row.n}</Text>
            </Group>
          ))}
        </Stack>
      </Section>
    </Stack>
  )
}

function DeficitTable({ title, rows }: { title: string; rows: RoleDeficitRow[] }) {
  return <DeficitFrame title={title} rows={rows.map((row) => ({ ...row, supply: row.supply_hh }))} />
}

function EffectiveDeficitTable({ title, rows }: { title: string; rows: RoleDeficitEffectiveRow[] }) {
  return <DeficitFrame title={title} rows={rows.map((row) => ({ ...row, supply: row.supply_with_substitution_hh }))} />
}

function DeficitFrame({ title, rows }: { title: string; rows: Array<{ team_id: string; role_name: string; demand_hh: string; supply: string; gap_hh: string; verdict: string }> }) {
  return (
    <div>
      <Group justify="space-between" mb="xs"><Text fw={500}>{title}</Text><Text size="xs" c="dimmed">{rows.length} строк</Text></Group>
      <ScrollArea type="auto" h={390}>
        <Table striped highlightOnHover stickyHeader miw={620}>
          <Table.Thead><Table.Tr><Table.Th>Команда</Table.Th><Table.Th>Роль</Table.Th><Table.Th ta="right">Спрос</Table.Th><Table.Th ta="right">Фонд</Table.Th><Table.Th ta="right">Разрыв</Table.Th><Table.Th>Вердикт</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {rows.map((row) => (
              <Table.Tr key={`${row.team_id}-${row.role_name}`}>
                <Table.Td>{row.team_id}</Table.Td><Table.Td>{row.role_name}</Table.Td>
                <Table.Td ta="right" className="mono">{fmtHours(row.demand_hh)}</Table.Td>
                <Table.Td ta="right" className="mono">{fmtHours(row.supply)}</Table.Td>
                <Table.Td ta="right" className="mono">{fmtHours(row.gap_hh)}</Table.Td>
                <Table.Td><Verdict text={row.verdict} problem={!isNegative(row.gap_hh)} /></Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </ScrollArea>
    </div>
  )
}

function Verdict({ text, problem }: { text: string; problem: boolean }) {
  return <Badge color={problem ? 'red' : 'teal'} variant="light" size="sm">{text}</Badge>
}

function RiskBadge({ risk }: { risk: string }) {
  const color = risk === 'ок' ? 'teal' : risk.includes('НЕТ') ? 'red' : 'yellow'
  return <Badge color={color} variant="light" size="sm">{risk}</Badge>
}

function Section({ title, note, action, children }: { title: string; note?: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <Paper withBorder p="md">
      <Stack gap="md">
        <Group justify="space-between" align="flex-start" wrap="wrap">
          <div><Title order={3}>{title}</Title>{note && <Text size="xs" c="dimmed" mt={2}>{note}</Text>}</div>
          {action}
        </Group>
        {children}
      </Stack>
    </Paper>
  )
}
