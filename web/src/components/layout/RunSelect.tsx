/**
 * Выбор прогона планировщика. По умолчанию — «текущий» (последний удачный),
 * подписан явно: без этого непонятно, почему план от 3-го спринта отличается
 * от Недели 0 (docs/UI_SPEC.md §0).
 */
import { Select } from '@mantine/core'
import { useRun } from '../../hooks/useRun'

export function RunSelect() {
  const { runs, runId, defaultRunId, isDefault, setRunId, isLoading } = useRun()

  const options = runs.map((r) => ({
    value: String(r.run_id),
    label:
      `Прогон ${r.run_id} · спринт ${r.as_of_sprint}` +
      (r.run_id === defaultRunId ? ' · текущий' : '') +
      (r.status !== 'ok' ? ` · ${r.status}` : ''),
  }))

  return (
    <Select
      size="sm"
      w={240}
      placeholder="Прогон"
      data={options}
      value={runId !== null ? String(runId) : null}
      onChange={(v) => setRunId(v ? Number(v) : null)}
      disabled={isLoading || runs.length === 0}
      description={isDefault ? 'текущий (последний удачный)' : 'выбран вручную'}
      allowDeselect={false}
    />
  )
}
