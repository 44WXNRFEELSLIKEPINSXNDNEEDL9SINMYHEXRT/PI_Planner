/**
 * Тонкая обёртка над API бэкенда.
 * В dev-режиме /api уходит через прокси Vite на 127.0.0.1:8000,
 * на демо тот же путь отдаёт сам сервер — код один и тот же.
 */
const BASE = '/api'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new ApiError(res.status, text || res.statusText)
  }
  return (await res.json()) as T
}

export type HealthResponse = {
  dsn: string
  server_version: string
  dbname: string
  tables: number
  views: number
}

export const api = {
  health: () => request<HealthResponse>('/health'),
}
