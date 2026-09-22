import { Badge, Tooltip } from '@mantine/core'
import { useHealth } from '../../hooks/useHealth'

export function HealthBadge() {
  const health = useHealth()

  if (health.isPending) {
    return (
      <Badge color="gray" variant="light" size="sm">
        проверяю API…
      </Badge>
    )
  }
  if (health.isError) {
    return (
      <Tooltip label="База не отвечает — запустите run.sh или run.bat">
        <Badge color="red" variant="filled" size="sm">
          API недоступен
        </Badge>
      </Tooltip>
    )
  }
  return (
    <Tooltip label={`PostgreSQL ${health.data.server_version} · ${health.data.tables} таблиц · ${health.data.views} вьюх`}>
      <Badge color="teal" variant="light" size="sm">
        API в порядке
      </Badge>
    </Tooltip>
  )
}
