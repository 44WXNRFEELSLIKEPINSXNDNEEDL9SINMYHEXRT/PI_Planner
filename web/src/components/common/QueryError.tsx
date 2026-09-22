/**
 * Общее состояние ошибки для экранов, которые читают несколько витрин сразу
 * (`QueryState` рассчитан на один запрос). 503 отделяется от прочих ошибок:
 * «сервер не поднят» — это не баг данных, и текст другой (docs/UI_SPEC.md §0).
 */
import type { UseQueryResult } from '@tanstack/react-query'
import { Alert } from '@mantine/core'
import { ApiError, ServiceUnavailableError } from '../../api/client'

type AnyQuery = UseQueryResult<unknown, Error>

/** Первая непустая ошибка из набора запросов — её и показываем. */
export function firstError(queries: AnyQuery[]): Error | null {
  for (const q of queries) {
    if (q.error) return q.error
  }
  return null
}

export function anyPending(queries: AnyQuery[]): boolean {
  return queries.some((q) => q.isPending)
}

export function QueryError({ error, title }: { error: Error; title: string }) {
  if (error instanceof ServiceUnavailableError) {
    return (
      <Alert color="red" variant="light" title="API недоступен">
        База данных не отвечает. Запустите <code>run.sh</code> (Linux/macOS) или{' '}
        <code>run.bat</code> (Windows) из корня репозитория и обновите страницу.
      </Alert>
    )
  }
  return (
    <Alert color="red" variant="light" title={title}>
      {error instanceof ApiError ? error.message : String(error)}
    </Alert>
  )
}
