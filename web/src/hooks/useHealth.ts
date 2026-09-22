import { useQuery } from '@tanstack/react-query'
import { fetchHealth } from '../api/client'

/** Опрашиваем готовность раз в 15 с — то же, с чем сверяется devops (RUNBOOK §8). */
export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    staleTime: 10_000,
    refetchInterval: 15_000,
    retry: 1,
  })
}
