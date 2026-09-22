import { useState } from 'react'
import { Alert, Group, Paper, Skeleton, Stack, Text, Title } from '@mantine/core'
import { usePlanData } from './usePlanData'
import { GanttGrid } from './GanttGrid'
import { TaskDetailDrawer } from './TaskDetailDrawer'
import { AsOfLabel } from '../../components/common/AsOfLabel'
import { ApiError, ServiceUnavailableError } from '../../api/client'
import type { TaskRow } from '../../types/views'

export function PlanScreen() {
  const data = usePlanData()
  const [selected, setSelected] = useState<TaskRow | null>(null)

  if (data.isPending) {
    return (
      <Stack gap="md" maw={1200}>
        <Title order={2}>План квартала</Title>
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

  return (
    <Stack gap="md" maw={1200}>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <div>
          <Title order={2}>План квартала</Title>
          <Text c="dimmed" size="sm" mt={2}>
            {data.scheduleByTask.size > 0
              ? `В квартал взято ${inQuarter} задач из ${data.scheduleByTask.size} — остальные перенесены на следующий PI. Это решение планировщика, а не сокращение бэклога.`
              : `Живых задач: ${totalLive}`}
          </Text>
        </div>
        <AsOfLabel iso={data.asOf} />
      </Group>

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

      <Legend hasBaseline={data.hasBaseline} />

      <TaskDetailDrawer
        task={selected}
        board={selected ? data.boardByTask.get(selected.task_id) : undefined}
        schedule={selected ? data.scheduleByTask.get(selected.task_id) : undefined}
        assignments={selected ? data.assignmentsByTask.get(selected.task_id) ?? [] : []}
        onClose={() => setSelected(null)}
      />
    </Stack>
  )
}

function Legend({ hasBaseline }: { hasBaseline: boolean }) {
  return (
    <Group gap="lg" wrap="wrap">
      <LegendItem swatch={{ background: 'var(--ink)' }} label="в квартале" />
      <LegendItem
        swatch={{
          background:
            'repeating-linear-gradient(45deg, var(--ink) 0, var(--ink) 3px, transparent 3px, transparent 6px)',
        }}
        label="есть заём у другой команды"
      />
      <LegendItem swatch={{ borderTop: '1px dashed var(--muted)', background: 'transparent' }} label="перенесена / отменена" />
      {hasBaseline && (
        <LegendItem swatch={{ background: 'var(--muted)', height: 2, alignSelf: 'flex-end' }} label="базовая линия Недели 0" />
      )}
    </Group>
  )
}

function LegendItem({ swatch, label }: { swatch: React.CSSProperties; label: string }) {
  return (
    <Group gap={6} wrap="nowrap">
      <div style={{ width: 20, height: 12, borderRadius: 1, ...swatch }} />
      <Text size="xs" c="dimmed">
        {label}
      </Text>
    </Group>
  )
}
