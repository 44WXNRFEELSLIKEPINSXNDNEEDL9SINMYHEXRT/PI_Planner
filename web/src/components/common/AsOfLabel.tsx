import { Text } from '@mantine/core'

/** «as_of показывать в шапке каждого экрана» (docs/UI_SPEC.md §0). */
export function AsOfLabel({ iso }: { iso: string | null }) {
  if (!iso) return null
  const d = new Date(iso)
  const text = Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  return (
    <Text size="xs" c="dimmed" className="mono">
      данные на {text}
    </Text>
  )
}
