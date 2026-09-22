/**
 * Общая обвязка для запроса витрины: скелетон, ошибка, пусто, данные.
 * Различает 503 (сервер недоступен — это не баг фронта) от прочих ошибок
 * (docs/UI_SPEC.md §0: коды 400/404/503 означают разное).
 */
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { Alert, Skeleton, Stack, Text } from '@mantine/core'
import { ApiError, ServiceUnavailableError } from '../../api/client'

interface Props<T> {
  query: UseQueryResult<T>
  skeletonHeight?: number
  skeletonRows?: number
  isEmpty?: (data: T) => boolean
  emptyTitle?: string
  emptyBody?: ReactNode
  children: (data: T) => ReactNode
}

export function QueryState<T>({
  query,
  skeletonHeight = 22,
  skeletonRows = 4,
  isEmpty,
  emptyTitle = 'Здесь пока пусто',
  emptyBody,
  children,
}: Props<T>) {
  if (query.isPending) {
    return (
      <Stack gap={8}>
        {Array.from({ length: skeletonRows }).map((_, i) => (
          <Skeleton key={i} height={skeletonHeight} radius={2} />
        ))}
      </Stack>
    )
  }

  if (query.isError) {
    const err = query.error
    if (err instanceof ServiceUnavailableError) {
      return (
        <Alert color="red" variant="light" title="API недоступен">
          База данных не отвечает. Запустите{' '}
          <code>run.sh</code> (Linux/macOS) или <code>run.bat</code> (Windows) из корня
          репозитория и обновите страницу.
        </Alert>
      )
    }
    const message = err instanceof ApiError ? err.message : String(err)
    return (
      <Alert color="red" variant="light" title="Не удалось загрузить данные">
        {message}
      </Alert>
    )
  }

  const data = query.data as T
  if (isEmpty?.(data)) {
    return (
      <Stack gap={4} py="md">
        <Text fw={500}>{emptyTitle}</Text>
        {emptyBody && (
          <Text c="dimmed" size="sm">
            {emptyBody}
          </Text>
        )}
      </Stack>
    )
  }

  return <>{children(data)}</>
}
