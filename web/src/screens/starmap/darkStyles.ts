import type { CSSProperties } from 'react'

/** Общие стили тёмной области: Mantine живёт в светлой схеме, тёмное красим сами. */
export const panel: CSSProperties = {
  background: 'var(--field)',
  border: '1px solid rgba(127, 166, 217, 0.22)',
  borderRadius: 2,
  padding: 16,
}

export const muted: CSSProperties = { color: '#a3aecb' }

export const chip = (active: boolean): CSSProperties => ({
  all: 'unset',
  cursor: 'pointer',
  padding: '3px 10px',
  borderRadius: 2,
  fontSize: 13,
  border: `1px solid ${active ? 'var(--star)' : 'rgba(127, 166, 217, 0.35)'}`,
  background: active ? 'rgba(232, 236, 245, 0.12)' : 'transparent',
  color: 'var(--star)',
})
