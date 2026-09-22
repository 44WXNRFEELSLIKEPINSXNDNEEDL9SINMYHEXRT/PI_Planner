/**
 * Единая точка входа к витринам. КРИТИЧНО: у react-query кэш по `queryKey`,
 * а не по `queryFn` — если два места запросят один и тот же `queryKey` с
 * РАЗНОЙ формой ответа (например, один ждёт конверт, другой — `.items`),
 * второй молча получит данные первого и упадёт на чужой форме. Единственная
 * защита — один хук на все витрины, всегда возвращающий конверт целиком,
 * и ключ, целиком выведенный из имени витрины и параметров запроса.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { fetchView, type ViewEnvelope, type ViewParams } from '../api/client'

export function useView<T>(name: string, params: ViewParams = {}): UseQueryResult<ViewEnvelope<T>> {
  return useQuery({
    queryKey: ['view', name, params.runId ?? null, params.limit ?? null, params.offset ?? null, params.order ?? null],
    queryFn: () => fetchView<T>(name, params),
  })
}
