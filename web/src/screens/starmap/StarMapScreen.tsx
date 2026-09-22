/**
 * Звёздная карта (docs/UI_DESIGN.md §5.4, docs/UI_SPEC.md §2.6, §8.5).
 * Тёмная палитра — только здесь, класс `.starmap-scope`.
 * Три разные вещи, не смешивать:
 *  - карта: состав команд и распределение людей между ними, риск отсутствия;
 *  - Bus Factor по компетенциям (`v_bus_factor_skill`) — главная метрика ТЗ;
 *  - роли без людей в штате — вопрос найма, отдельный блок.
 */
import { useMemo, useState } from 'react'
import { Group, Skeleton, Stack, Text, Title } from '@mantine/core'
import { useRun } from '../../hooks/useRun'
import {
  useBusFactorSkill,
  useEngineerAbsenceRisk,
  useOrbitMap,
  useRoleCoverageOrg,
  useSatelliteCapacity,
} from '../../hooks/useViews'
import { QueryError, anyPending, firstError } from '../../components/common/QueryError'
import { AsOfLabel } from '../../components/common/AsOfLabel'
import type { BusFactorSkillRow, EngineerAbsenceRiskRow } from '../../types/views'
import { StarMapSvg } from './StarMapSvg'
import { EngineerDrawer } from './EngineerDrawer'
import { SkillBusFactor } from './SkillBusFactor'
import { HiringGap } from './HiringGap'
import { LEVEL_COLOR, LEVEL_WORD, starLevel, type StarLevel } from './risk'
import { muted, panel } from './darkStyles'

export function StarMapScreen() {
  const { runId } = useRun()
  const orbitQ = useOrbitMap()
  const absenceQ = useEngineerAbsenceRisk(runId)
  const skillQ = useBusFactorSkill()
  const capacityQ = useSatelliteCapacity()
  const coverageQ = useRoleCoverageOrg()

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [skill, setSkill] = useState<BusFactorSkillRow | null>(null)

  const absenceById = useMemo(
    () => new Map<string, EngineerAbsenceRiskRow>((absenceQ.data?.items ?? []).map((r) => [r.engineer_id, r])),
    [absenceQ.data],
  )

  const queries = [orbitQ, absenceQ, skillQ, capacityQ, coverageQ]
  const error = firstError(queries)

  const frame = (children: React.ReactNode) => (
    <div className="starmap-scope" style={{ padding: 20, margin: -16, minHeight: 'calc(100vh - 106px)' }}>
      <Stack gap="lg" maw={1280} mx="auto">
        {children}
      </Stack>
    </div>
  )

  if (anyPending(queries)) {
    return frame(
      <>
        <Title order={2} c="var(--star)">Звёздная карта</Title>
        <Skeleton height={560} style={{ opacity: 0.15 }} />
      </>,
    )
  }
  if (error) {
    return frame(
      <>
        <Title order={2} c="var(--star)">Звёздная карта</Title>
        <QueryError error={error} title="Не удалось загрузить звёздную карту" />
      </>,
    )
  }

  const orbits = orbitQ.data?.items ?? []
  if (orbits.length === 0) {
    return frame(
      <>
        <Title order={2} c="var(--star)">Звёздная карта</Title>
        <div style={panel}>
          <Text size="sm">Инженеров в базе нет. Загрузите датасет на вкладке «Загрузка» — карта построится по составу команд.</Text>
        </div>
      </>,
    )
  }
  const skills = skillQ.data?.items ?? []
  const teams = new Set(orbits.flatMap((o) => o.teams))
  const shared = orbits.filter((o) => o.teams.length > 1).length
  const levels = new Map<StarLevel, number>()
  orbits.forEach((o) => {
    const l = starLevel(o, absenceById.get(o.engineer_id))
    levels.set(l, (levels.get(l) ?? 0) + 1)
  })
  const critical = skills.filter((s) => s.sole_in_role).length
  const single = skills.filter((s) => s.bus_factor === 1).length
  const atRisk = (absenceQ.data?.items ?? []).filter((a) => a.tasks_without_backup.length > 0)
  const selectedOrbit = orbits.find((o) => o.engineer_id === selectedId) ?? null
  const highlight = skill ? new Set(skill.engineers) : null

  return frame(
    <>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <div>
          <Title order={2} c="var(--star)">Звёздная карта</Title>
          <Text size="sm" mt={2} style={muted}>
            {orbits.length} инженеров в {teams.size} командах, {shared} из них делят ставку между двумя командами.{' '}
            {single} из {skills.length} компетенций держатся на одном человеке, {critical} — критично.
          </Text>
        </div>
        <div style={{ color: 'var(--star)' }}>
          <AsOfLabel iso={orbitQ.data?.as_of ?? null} />
        </div>
      </Group>

      {atRisk.length > 0 && (
        <div style={{ ...panel, borderLeft: '4px solid var(--flare)' }}>
          <Text size="sm" fw={600} style={{ color: 'var(--flare)' }}>
            Где отсутствие одного человека останавливает план
          </Text>
          {atRisk.map((a) => (
            <Text key={a.engineer_id} size="sm" mt={4}>
              <button type="button" onClick={() => setSelectedId(a.engineer_id)} className="mono" style={linkBtn}>
                {a.engineer_id}
              </button>{' '}
              ({a.role_name}) — задачи <span className="mono">{a.tasks_without_backup.join(', ')}</span> встанут: второго
              специалиста этой роли в компании нет.
            </Text>
          ))}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 16 }}>
        <div style={{ ...panel, padding: 8 }}>
          <StarMapSvg
            rows={orbits}
            absenceById={absenceById}
            highlight={highlight}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
          <Legend levels={levels} skillName={skill?.skill_name ?? null} onClearSkill={() => setSkill(null)} />
        </div>
      </div>

      <div style={panel}>
        <Title order={3} c="var(--star)">
          Bus Factor по компетенциям
        </Title>
        <Text size="xs" mt={2} mb={12} style={muted}>
          Для каждого заявленного навыка — сколько инженеров им владеют. «Критично» — единственный носитель, который ещё
          и единственный специалист своей роли: выпадет — работу не подхватит никто, замещения ролей запрещены.
        </Text>
        <SkillBusFactor rows={skills} selectedSkill={skill?.skill_id ?? null} onSelectSkill={setSkill} />
      </div>

      <div style={panel}>
        <Title order={3} c="var(--star)">
          Роли, которых нет в штате
        </Title>
        <Text size="xs" mt={2} mb={12} style={muted}>
          Это не незаменимость, а найм: людей с этими ролями в компании нет вовсе, поэтому на карте их нет. Часы — спрос
          бэклога на роль.
        </Text>
        <HiringGap rows={coverageQ.data?.items ?? []} />
      </div>

      <EngineerDrawer
        orbit={selectedOrbit}
        absence={selectedId ? absenceById.get(selectedId) : undefined}
        capacity={(capacityQ.data?.items ?? []).filter((c) => c.engineer_id === selectedId)}
        onClose={() => setSelectedId(null)}
      />
    </>,
  )
}

const linkBtn: React.CSSProperties = {
  all: 'unset',
  cursor: 'pointer',
  color: 'var(--star)',
  textDecoration: 'underline',
  textUnderlineOffset: 3,
}

function Legend({
  levels,
  skillName,
  onClearSkill,
}: {
  levels: Map<StarLevel, number>
  skillName: string | null
  onClearSkill: () => void
}) {
  const order: StarLevel[] = ['critical', 'single', 'ok']
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18, padding: '8px 10px', fontSize: 13, alignItems: 'center' }}>
      {order.map((l) => (
        <span key={l} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <svg width="14" height="14" aria-hidden>
            <circle cx="7" cy="7" r="5" fill={LEVEL_COLOR[l]} />
          </svg>
          {LEVEL_WORD[l]} <span className="mono" style={muted}>{levels.get(l) ?? 0}</span>
        </span>
      ))}
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
        <svg width="14" height="14" aria-hidden>
          <rect x="3" y="3" width="8" height="8" transform="rotate(45 7 7)" fill="var(--orbit)" />
        </svg>
        ставка на две команды
      </span>
      <span style={muted}>размер точки — грейд: Senior, Middle, Junior</span>
      {skillName && (
        <span style={{ marginLeft: 'auto' }}>
          Подсвечены носители «{skillName}»{' '}
          <button type="button" onClick={onClearSkill} style={linkBtn}>
            снять
          </button>
        </span>
      )}
    </div>
  )
}
