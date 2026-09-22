/**
 * Вертикальная линейка риска (docs/UI_DESIGN.md §4): цвет не единственный
 * носитель смысла — слово печатается рядом, а не подразумевается цветом.
 */
import { Group, Text } from '@mantine/core'
import type { ReactNode } from 'react'

export type RiskLevel = 'critical' | 'warning' | 'ok'

export const RISK_COLOR: Record<RiskLevel, string> = {
  critical: 'var(--stamp)',
  warning: 'var(--wax-text)',
  ok: 'var(--route-text)',
}

export function levelFromAlert(level: 'red' | 'yellow' | 'orange'): RiskLevel {
  if (level === 'red') return 'critical'
  if (level === 'yellow' || level === 'orange') return 'warning'
  return 'ok'
}

export function RiskRail({
  level,
  label,
  children,
}: {
  level: RiskLevel
  label: string
  children: ReactNode
}) {
  return (
    <Group gap="sm" wrap="nowrap" align="stretch">
      <div
        style={{
          width: 4,
          borderRadius: 2,
          background: RISK_COLOR[level],
          flexShrink: 0,
          alignSelf: 'stretch',
        }}
        aria-hidden
      />
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
      <Text
        size="xs"
        fw={500}
        style={{ color: RISK_COLOR[level], flexShrink: 0, whiteSpace: 'nowrap' }}
      >
        {label}
      </Text>
    </Group>
  )
}
