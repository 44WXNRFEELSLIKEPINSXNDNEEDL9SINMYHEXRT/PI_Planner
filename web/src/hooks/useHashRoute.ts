import { useEffect, useState } from 'react'

export type ScreenId =
  | 'summary'
  | 'assistant'
  | 'upload'
  | 'plan'
  | 'risks'
  | 'starmap'
  | 'kpi'
  | 'roles'
  | 'profiles'
  | 'data'

const SCREENS: ScreenId[] = ['summary', 'assistant', 'upload', 'plan', 'risks', 'starmap', 'kpi', 'roles', 'profiles', 'data']
const DEFAULT_SCREEN: ScreenId = 'summary'

function parse(hash: string): ScreenId {
  const id = hash.replace(/^#\/?/, '').split('?')[0] as ScreenId
  return SCREENS.includes(id) ? id : DEFAULT_SCREEN
}

/** Простая маршрутизация по хешу (`#/profiles?team=X`): без внешнего роутера. */
export function useHashRoute(): [ScreenId, (id: ScreenId, query?: Record<string, string>) => void] {
  const [screen, setScreen] = useState<ScreenId>(() => parse(window.location.hash))

  useEffect(() => {
    const onChange = () => setScreen(parse(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  const go = (id: ScreenId, query?: Record<string, string>) => {
    const qs = query ? `?${new URLSearchParams(query).toString()}` : ''
    window.location.hash = `/${id}${qs}`
  }

  return [screen, go]
}

/** Параметры из хеша: `#/profiles?team=X` → `team`. Обновляется при любой смене хеша. */
export function useHashQuery(): URLSearchParams {
  const read = () => new URLSearchParams(window.location.hash.split('?')[1] ?? '')
  const [params, setParams] = useState(read)
  useEffect(() => {
    const onChange = () => setParams(read())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return params
}
