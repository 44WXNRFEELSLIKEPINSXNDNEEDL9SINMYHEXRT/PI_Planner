import { QueryClient } from '@tanstack/react-query'
import { ApiError } from './client'

/**
 * `staleTime: 0` — сервер сам говорит `Cache-Control: no-store` (docs/UI_SPEC.md
 * §0): каждый показ экрана должен быть честным снимком `as_of`, а не старым
 * кэшем. React Query здесь только избавляет от повторных запросов ВНУТРИ
 * одного рендера и даёт единую точку инвалидации после загрузки файлов.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 0,
      gcTime: 5 * 60 * 1000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        // 4xx — ошибка запроса, повтор её не исправит; 503/сеть — можно попробовать ещё раз.
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false
        return failureCount < 2
      },
    },
  },
})

/** Один ключ на всё, что зависит от прогонов планировщика — для инвалидации после загрузки. */
export const RUN_SCOPED_KEY = 'run-scoped'
