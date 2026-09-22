import { AppShell, Group, ScrollArea, Tabs, Text } from '@mantine/core'
import type { ReactNode } from 'react'
import { HealthBadge } from './HealthBadge'
import { PiBadge } from './PiBadge'
import { RunSelect } from './RunSelect'
import type { ScreenId } from '../../hooks/useHashRoute'

const NAV: { id: ScreenId; label: string }[] = [
  { id: 'upload', label: 'Загрузка' },
  { id: 'plan', label: 'План квартала' },
  { id: 'risks', label: 'Риски' },
  { id: 'starmap', label: 'Звёздная карта' },
  { id: 'kpi', label: 'KPI' },
  { id: 'roles', label: 'Роли и ёмкость' },
  { id: 'profiles', label: 'Профили' },
]

export function Shell({
  screen,
  onNavigate,
  children,
}: {
  screen: ScreenId
  onNavigate: (id: ScreenId) => void
  children: ReactNode
}) {
  return (
    <AppShell header={{ height: 106 }} padding="md">
      <AppShell.Header>
        <Group justify="space-between" px="md" pt={10} wrap="nowrap">
          <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
            <Text fw={600} size="lg" style={{ whiteSpace: 'nowrap' }}>
              PI-Planner
            </Text>
            <Text size="sm" c="dimmed">
              ПочтаТех
            </Text>
          </Group>
          <Group gap="md" wrap="nowrap">
            <PiBadge />
            <RunSelect />
            <HealthBadge />
          </Group>
        </Group>
        <ScrollArea scrollbarSize={4} type="auto" px="md">
          <Tabs
            value={screen}
            onChange={(v) => v && onNavigate(v as ScreenId)}
            variant="outline"
          >
            <Tabs.List style={{ flexWrap: 'nowrap' }}>
              {NAV.map((item) => (
                <Tabs.Tab key={item.id} value={item.id} style={{ whiteSpace: 'nowrap' }}>
                  {item.label}
                </Tabs.Tab>
              ))}
            </Tabs.List>
          </Tabs>
        </ScrollArea>
      </AppShell.Header>
      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  )
}
