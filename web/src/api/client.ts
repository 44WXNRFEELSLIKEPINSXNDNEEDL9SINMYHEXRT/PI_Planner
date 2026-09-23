/**
 * Тонкий клиент над `/api/*`. Один маршрут читает все витрины
 * (GET /api/views/{view}), три маршрута пишут (docs/SCHEMA.md §4).
 */
const BASE = '/api'

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/** Сервер недоступен вовсе (503, сеть, DNS) — отличается от ошибки данных. */
export class ServiceUnavailableError extends ApiError {}

async function parseErrorBody(res: Response): Promise<unknown> {
  const text = await res.text().catch(() => '')
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function messageFrom(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'message' in body) {
    const m = (body as { message?: unknown }).message
    if (typeof m === 'string') return m
  }
  return fallback
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, init)
  } catch (err) {
    throw new ServiceUnavailableError(0, 'сеть недоступна или сервер не отвечает', {
      error: 'network_error',
      message: String(err),
    })
  }
  if (!res.ok) {
    const body = await parseErrorBody(res)
    const message = messageFrom(body, res.statusText)
    if (res.status === 503) throw new ServiceUnavailableError(res.status, message, body)
    throw new ApiError(res.status, message, body)
  }
  return (await res.json()) as T
}

/** Конверт ответа `GET /api/views/{view}` (docs/UI_SPEC.md §0). */
export interface ViewEnvelope<T> {
  view: string
  kind: 'view' | 'table'
  screen: string
  note: string
  run_id: number | null
  run_column: string | null
  run_default: boolean
  as_of: string
  order: string[]
  limit: number
  offset: number
  count: number
  returned: number
  truncated: boolean
  has_more: boolean
  columns: string[]
  items: T[]
}

export interface ViewParams {
  runId?: number
  limit?: number
  offset?: number
  order?: string
}

function buildQuery(params: ViewParams): string {
  const q = new URLSearchParams()
  if (params.runId !== undefined) q.set('run_id', String(params.runId))
  if (params.limit !== undefined) q.set('limit', String(params.limit))
  if (params.offset !== undefined) q.set('offset', String(params.offset))
  if (params.order) q.set('order', params.order)
  const s = q.toString()
  return s ? `?${s}` : ''
}

/** `GET /api/views/{name}` — строки витрины как есть плюс конверт. */
export function fetchView<T>(name: string, params: ViewParams = {}): Promise<ViewEnvelope<T>> {
  return request<ViewEnvelope<T>>(`/views/${name}${buildQuery(params)}`)
}

export interface HealthResponse {
  dsn: string
  server_version: string
  dbname: string
  tables: number
  views: number
}

export function fetchHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health')
}

/** Файл отправляется телом POST, без multipart (сервер — только stdlib). */
async function postFile<T>(path: string, file: File, extraQuery: Record<string, string> = {}): Promise<T> {
  const q = new URLSearchParams({ filename: file.name, ...extraQuery })
  let res: Response
  try {
    res = await fetch(`${BASE}${path}?${q.toString()}`, { method: 'POST', body: file })
  } catch (err) {
    throw new ServiceUnavailableError(0, 'сеть недоступна или сервер не отвечает', {
      error: 'network_error',
      message: String(err),
    })
  }
  const body = (await res.text().then((t) => (t ? JSON.parse(t) : null))) as T
  if (!res.ok) {
    const message = messageFrom(body, res.statusText)
    if (res.status === 503) throw new ServiceUnavailableError(res.status, message, body)
    throw new ApiError(res.status, message, body)
  }
  return body
}

export { request, postFile }
