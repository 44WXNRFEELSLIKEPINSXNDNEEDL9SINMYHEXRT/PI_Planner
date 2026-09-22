/**
 * Круглый штемпель KPI (docs/UI_DESIGN.md §4): тонкое кольцо — прогноз,
 * залитая дуга — факт. Форма показывает разницу прогноза и факта, которую
 * требует ТЗ, вместо одной подписи мелким шрифтом.
 *
 * Деления на треке — границы нормы (`target_min` / `target_max`): если норма
 * не задана с одной стороны (как у bus_factor, `target_max = null`),
 * рисуется только одно деление.
 */
import { Stack, Text } from '@mantine/core'

const SIZE = 132
const CENTER = SIZE / 2
const R_OUTER = 58
const R_INNER = 44

function polar(r: number, angleDeg: number): [number, number] {
  const rad = ((angleDeg - 90) * Math.PI) / 180
  return [CENTER + r * Math.cos(rad), CENTER + r * Math.sin(rad)]
}

function ringOffset(radius: number, fraction: number): { circumference: number; offset: number } {
  const circumference = 2 * Math.PI * radius
  const clamped = Math.max(0, Math.min(1, fraction))
  return { circumference, offset: circumference * (1 - clamped) }
}

export type StampTone = 'ok' | 'warning' | 'critical'

function toneColor(tone: StampTone): string {
  if (tone === 'critical') return 'var(--stamp)'
  if (tone === 'warning') return 'var(--wax-text)'
  return 'var(--route-text)'
}

export function toneOf(value: number, min: number | null, max: number | null): StampTone {
  if (min !== null && value < min) return 'critical'
  if (max !== null && value > max) return 'warning'
  return 'ok'
}

interface Props {
  title: string
  unit?: string
  forecast: number | null
  actual: number | null
  targetMin: number | null
  targetMax: number | null
  /** Верхняя граница шкалы — 100 для процентов «до 100», больше для say_do (может расти за 100%). */
  domainMax?: number
  caption?: string
}

export function KpiStamp({
  title,
  unit = '%',
  forecast,
  actual,
  targetMin,
  targetMax,
  domainMax = 100,
  caption,
}: Props) {
  const shown = actual ?? forecast ?? 0
  const tone = toneOf(shown, targetMin, targetMax)
  const forecastRing = forecast !== null ? ringOffset(R_OUTER, forecast / domainMax) : null
  const actualRing = actual !== null ? ringOffset(R_INNER, actual / domainMax) : null

  const minAngle = targetMin !== null ? (targetMin / domainMax) * 360 : null
  const maxAngle = targetMax !== null ? (targetMax / domainMax) * 360 : null

  return (
    <Stack align="center" gap={6}>
      <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label={`${title}: ${shown}${unit}`}>
        <circle cx={CENTER} cy={CENTER} r={R_OUTER} fill="none" stroke="var(--line)" strokeWidth={2} />
        <circle cx={CENTER} cy={CENTER} r={R_INNER} fill="none" stroke="var(--line)" strokeWidth={1} />

        {[minAngle, maxAngle].map((angle, i) =>
          angle === null ? null : (
            <line
              key={i}
              x1={polar(R_OUTER - 6, angle)[0]}
              y1={polar(R_OUTER - 6, angle)[1]}
              x2={polar(R_OUTER + 6, angle)[0]}
              y2={polar(R_OUTER + 6, angle)[1]}
              stroke="var(--muted)"
              strokeWidth={2}
            />
          ),
        )}

        {forecastRing && (
          <circle
            cx={CENTER}
            cy={CENTER}
            r={R_OUTER}
            fill="none"
            stroke={toneColor(tone)}
            strokeWidth={3}
            strokeLinecap="round"
            strokeDasharray={forecastRing.circumference}
            strokeDashoffset={forecastRing.offset}
            transform={`rotate(-90 ${CENTER} ${CENTER})`}
          />
        )}

        {actualRing && (
          <circle
            cx={CENTER}
            cy={CENTER}
            r={R_INNER}
            fill="none"
            stroke={toneColor(tone)}
            strokeWidth={9}
            strokeLinecap="round"
            strokeDasharray={actualRing.circumference}
            strokeDashoffset={actualRing.offset}
            transform={`rotate(-90 ${CENTER} ${CENTER})`}
          />
        )}

        <text
          x={CENTER}
          y={CENTER - 2}
          textAnchor="middle"
          fontSize={22}
          fontWeight={600}
          fontFamily="var(--font-mono)"
          fill="var(--ink)"
        >
          {shown.toFixed(shown % 1 === 0 ? 0 : 1)}
          {unit}
        </text>
        <text x={CENTER} y={CENTER + 16} textAnchor="middle" fontSize={10} fill="var(--muted)">
          {actual !== null ? 'факт' : 'прогноз'}
        </text>
      </svg>
      <Text fw={500} size="sm" ta="center">
        {title}
      </Text>
      {caption && (
        <Text size="xs" c="dimmed" ta="center" maw={160}>
          {caption}
        </Text>
      )}
    </Stack>
  )
}
