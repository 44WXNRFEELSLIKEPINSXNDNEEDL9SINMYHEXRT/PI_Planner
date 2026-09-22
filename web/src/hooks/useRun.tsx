/**
 * Прогон планировщика — общий для всех экранов: гант, риски, KPI, назначения
 * должны показывать ОДИН И ТОТ ЖЕ прогон одновременно, иначе цифры на разных
 * экранах не сойдутся (docs/UI_SPEC.md §0 — «run_id показывать там, где он не
 * null»). Правило «текущего» одно и то же везде: последний прогон со
 * `status = 'ok'` — сервер сообщает его через `run_default: true`.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { usePlanRuns } from './useViews'
import type { PlanRunRow } from '../types/views'

interface RunContextValue {
  runs: PlanRunRow[]
  /** Прогон, который сейчас показывают экраны: выбранный вручную или «текущий». */
  runId: number | null
  /** «Текущий» по правилу сервера: MAX(run_id) WHERE status = 'ok'. */
  defaultRunId: number | null
  isDefault: boolean
  setRunId: (id: number | null) => void
  isLoading: boolean
  isError: boolean
}

const RunContext = createContext<RunContextValue | null>(null)

export function RunProvider({ children }: { children: ReactNode }) {
  const [selected, setSelected] = useState<number | null>(null)
  const query = usePlanRuns()

  const runs = useMemo(
    () => (query.data?.items ?? []).slice().sort((a, b) => b.run_id - a.run_id),
    [query.data],
  )
  const defaultRunId = useMemo(() => {
    const ok = runs.filter((r) => r.status === 'ok')
    return ok.length ? Math.max(...ok.map((r) => r.run_id)) : null
  }, [runs])

  const runId = selected ?? defaultRunId

  const value: RunContextValue = {
    runs,
    runId,
    defaultRunId,
    isDefault: selected === null || selected === defaultRunId,
    setRunId: setSelected,
    isLoading: query.isLoading,
    isError: query.isError,
  }

  return <RunContext.Provider value={value}>{children}</RunContext.Provider>
}

export function useRun(): RunContextValue {
  const ctx = useContext(RunContext)
  if (!ctx) throw new Error('useRun() вызван вне <RunProvider>')
  return ctx
}
