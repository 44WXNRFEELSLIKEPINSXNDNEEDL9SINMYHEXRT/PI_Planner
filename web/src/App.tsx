import { useEffect, useState } from 'react'
import { ApiError, api } from './api'
import type { HealthResponse } from './api'

type Probe =
  | { state: 'loading' }
  | { state: 'ok'; data: HealthResponse }
  | { state: 'error'; message: string }

export default function App() {
  const [probe, setProbe] = useState<Probe>({ state: 'loading' })

  useEffect(() => {
    let alive = true
    api
      .health()
      .then((data) => {
        if (alive) setProbe({ state: 'ok', data })
      })
      .catch((err: unknown) => {
        const message = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err)
        if (alive) setProbe({ state: 'error', message })
      })
    return () => {
      alive = false
    }
  }, [])

  return (
    <main className="shell">
      <header className="masthead">
        <h1>PI-Planner</h1>
        <p className="sub">Квартальное планирование и звёздная карта команд · ПочтаТех</p>
      </header>

      <section className="card">
        <h2>Состояние слоя данных</h2>
        {probe.state === 'loading' && <p className="muted">Проверяю /api/health…</p>}
        {probe.state === 'error' && (
          <p className="bad">
            API недоступен: {probe.message}
            <br />
            <span className="muted">
              Ожидаемо до вехи M4 — сервер появится в <code>app/server.py</code>.
            </span>
          </p>
        )}
        {probe.state === 'ok' && (
          <dl className="kv">
            <dt>СУБД</dt>
            <dd>PostgreSQL {probe.data.server_version}</dd>
            <dt>База</dt>
            <dd>{probe.data.dbname}</dd>
            <dt>Таблиц</dt>
            <dd>{probe.data.tables}</dd>
            <dt>Вьюх</dt>
            <dd>{probe.data.views}</dd>
            <dt>DSN</dt>
            <dd>{probe.data.dsn}</dd>
          </dl>
        )}
      </section>

      <section className="card">
        <h2>Что дальше</h2>
        <ol className="muted">
          <li>Планировщик квартала (M2): упаковка спринтов по приоритету и топологии.</li>
          <li>Пересчёт раз в спринт и KPI (M3): предсказуемость, say/do, bus factor.</li>
          <li>Шесть экранов (M4), включая звёздную карту на SVG без сторонних чартов.</li>
        </ol>
      </section>
    </main>
  )
}
