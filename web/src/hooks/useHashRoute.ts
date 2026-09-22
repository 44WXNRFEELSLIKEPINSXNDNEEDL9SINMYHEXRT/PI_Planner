import { useEffect, useState } from 'react'

export type ScreenId =
  | 'upload'
  | 'plan'
  | 'risks'
  | 'starmap'
  | 'kpi'
  | 'roles'
  | 'profiles'

const SCREENS: ScreenId[] = ['upload', 'plan', 'risks', 'starmap', 'kpi', 'roles', 'profiles']
const DEFAULT_SCREEN: ScreenId = 'upload'

function parse(hash: string): ScreenId {
  const id = hash.replace(/^#\/?/, '') as ScreenId
  return SCREENS.includes(id) ? id : DEFAULT_SCREEN
}

/** Простая маршрутизация по хешу: семь экранов, без внешнего роутера. */
export function useHashRoute(): [ScreenId, (id: ScreenId) => void] {
  const [screen, setScreen] = useState<ScreenId>(() => parse(window.location.hash))

  useEffect(() => {
    const onChange = () => setScreen(parse(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  const go = (id: ScreenId) => {
    window.location.hash = `/${id}`
  }

  return [screen, go]
}
