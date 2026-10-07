import { useState } from 'react'
import { L } from '../../components/imperium/L'
import { Alert, Group, Paper, Skeleton, Stack, Text, Title } from '@mantine/core'
import { usePlanData } from './usePlanData'
import { useRun } from '../../hooks/useRun'
import { GanttGrid } from './GanttGrid'
import { PlanSummary } from './PlanSummary'
import { TaskDetailDrawer } from './TaskDetailDrawer'
import { AsOfLabel } from '../../components/common/AsOfLabel'
import { ApiError, ServiceUnavailableError } from '../../api/client'
import type { TaskRow } from '../../types/views'

export function PlanScreen() {
  const data = usePlanData()
  const { runId, runs } = useRun()
  const [selected, setSelected] = useState<TaskRow | null>(null)

  if (data.isPending) {
    return (
      <Stack gap="md">
        <Title order={2}><L>План квартала</L></Title>
        <Skeleton height={320} />
      </Stack>
    )
  }

  if (data.isError) {
    const err = data.firstError
    if (err instanceof ServiceUnavailableError) {
      return (
        <Alert color="red" title="API недоступен">
          База не отвечает — запустите <code>run.sh</code> или <code>run.bat</code>.
        </Alert>
      )
    }
    return (
      <Alert color="red" title="Не удалось построить план">
        {err instanceof ApiError ? err.message : String(err)}
      </Alert>
    )
  }

  const totalLive = data.groups.reduce((n, g) => n + g.tasks.filter((t) => t.status !== 'Done').length, 0)
  const inQuarter = [...data.scheduleByTask.values()].filter((s) => s.decision === 'in_quarter').length
  const achievedGoals = data.initiativeGoals.filter((row) => row.quarter_goal_status === 'confirmed_achieved').length
  const selectedRun = runs.find((row) => row.run_id === runId)
  const algorithmName = selectedRun?.algorithm === 'lns-cpsat@1'
    ? 'LNS + CP-SAT'
    : selectedRun?.algorithm === 'greedy-priority-topo@2'
      ? 'Жадный'
      : selectedRun?.algorithm
  const stability = (selectedRun?.params as Record<string, unknown> | undefined)?.stability as {
    continued_role_pairs: number; kept_role_pairs: number; switched_role_pairs: number;
    people_changed: number; switches: Array<{ task_id: string; role_id: number; before: string[]; after: string[]; cause: string }>
  } | undefined

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <div>
          <Title order={2}><L>План квартала</L></Title>
          {algorithmName && <Text c="dimmed" size="sm">Алгоритм: {algorithmName}</Text>}
          <Text c="dimmed" size="sm" mt={2}>
            {data.scheduleByTask.size > 0
              ? `В квартал взято ${inQuarter} задач из ${data.scheduleByTask.size} — остальные перенесены на следующий PI. Это решение планировщика, а не сокращение бэклога.`
              : `Живых задач: ${totalLive}`}
          </Text>
          <Text c="dimmed" size="sm">
            Цель подтверждена у {achievedGoals} из {data.initiativeGoals.length} инициатив.
            Включение задачи в план ещё не означает бизнес приёмку.
          </Text>
        </div>
        <AsOfLabel iso={data.asOf} />
      </Group>

      <PlanSummary schedule={[...data.scheduleByTask.values()]} />
      {stability && stability.continued_role_pairs > 0 && <Alert color={stability.switched_role_pairs ? 'yellow' : 'teal'} title="Стабильность исполнителей">
        Сохранено {stability.kept_role_pairs} из {stability.continued_role_pairs} продолжающихся связок «задача × роль».
        Смен: {stability.switched_role_pairs}, затронуто людей: {stability.people_changed}.
        {stability.switches.map((item) => <Text key={`${item.task_id}-${item.role_id}`} size="xs" mt={4}>
          {item.task_id}, роль {item.role_id}: {item.before.join(', ') || '—'} → {item.after.join(', ') || '—'} ({item.cause === 'unavailable_or_unqualified' ? 'нет доступного квалифицированного прежнего исполнителя' : item.cause === 'task_deferred' ? 'задача перенесена' : 'ограничение фонда или конкуренция за ресурс'}).
        </Text>)}
      </Alert>}

      <Legend hasBaseline={data.hasBaseline} />

      <Paper withBorder p={0} style={{ overflow: 'auto' }}>
        <GanttGrid
          sprints={data.sprints}
          groups={data.groups}
          scheduleByTask={data.scheduleByTask}
          baselineByTask={data.baselineByTask}
          hasBaseline={data.hasBaseline}
          spByTask={data.spByTask}
          assignmentsByTask={data.assignmentsByTask}
          onSelect={setSelected}
        />
      </Paper>

      <TaskDetailDrawer
        task={selected}
        state={selected ? data.stateByTask.get(selected.task_id) : undefined}
        roleDemand={selected ? data.roleDemandByTask.get(selected.task_id) ?? [] : []}
        progress={selected ? data.progressByTask.get(selected.task_id) ?? [] : []}
        dependencyBound={selected ? data.boundsByTask.get(selected.task_id) : undefined}
        goalOutcome={selected ? data.goalsByTask.get(selected.task_id) : undefined}
        schedule={selected ? data.scheduleByTask.get(selected.task_id) : undefined}
        assignments={selected ? data.assignmentsByTask.get(selected.task_id) ?? [] : []}
        onClose={() => setSelected(null)}
      />
    </Stack>
  )
}

function Legend({ hasBaseline }: { hasBaseline: boolean }) {
  return (
    <Group
      gap="lg"
      wrap="wrap"
      px="md"
      py={8}
      style={{ border: '1px dashed var(--mantine-color-default-border)', borderRadius: 8 }}
    >
      <Text size="sm" fw={700} style={{ fontFamily: 'var(--font-heading)' }}>Как читать таблицу</Text>
      <LegendItem swatch={{ background: 'var(--ink)' }} label="Назначена работа" />
      <LegendItem swatch={{ background: 'repeating-linear-gradient(90deg, var(--line) 0, var(--line) 3px, transparent 3px, transparent 7px)' }} label="Ожидание в окне задачи" />
      <LegendItem
        swatch={{
          background:
            'repeating-linear-gradient(45deg, var(--ink) 0, var(--ink) 3px, transparent 3px, transparent 6px)',
        }}
        label="Есть заём у другой команды"
      />
      <LegendItem swatch={{ borderTop: '1px dashed var(--muted)', background: 'transparent' }} label="Перенесена / отменена" />
      {hasBaseline && (
        <LegendItem swatch={{ background: 'var(--muted)', height: 2, alignSelf: 'flex-end' }} label="Базовая линия Недели 0" />
      )}
      <Text size="sm" c="dimmed">✓ завершена по факту</Text>
    </Group>
  )
}

function LegendItem({ swatch, label }: { swatch: React.CSSProperties; label: string }) {
  return (
    <Group gap={6} wrap="nowrap">
      <div style={{ width: 20, height: 12, borderRadius: 1, ...swatch }} />
      <Text size="sm" c="dimmed">
        {label}
      </Text>
    </Group>
  )
}
