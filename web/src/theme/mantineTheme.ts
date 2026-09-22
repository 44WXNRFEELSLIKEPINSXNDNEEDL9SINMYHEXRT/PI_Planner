import { createTheme, rem } from '@mantine/core'

/**
 * Тема «Маршрутного листа» (docs/UI_DESIGN.md §7).
 * Радиус 2 — маршрутный лист, а не карточки; акцент — почтовая синь,
 * а не дефолтный синий Mantine.
 */
export const theme = createTheme({
  primaryColor: 'post',
  primaryShade: { light: 8, dark: 6 },
  defaultRadius: 2,
  fontFamily: 'var(--font-text)',
  fontFamilyMonospace: 'var(--font-mono)',
  headings: {
    fontFamily: 'var(--font-text)',
    sizes: {
      h1: { fontSize: rem(38), lineHeight: '1.15', fontWeight: '600' },
      h2: { fontSize: rem(22), lineHeight: '1.3', fontWeight: '500' },
      h3: { fontSize: rem(18), lineHeight: '1.4', fontWeight: '500' },
    },
  },
  colors: {
    post: [
      '#EEF1F6',
      '#D8DFEA',
      '#B4C1D6',
      '#8DA0C0',
      '#6E85AD',
      '#5A73A1',
      '#4E6899',
      '#3F5885',
      '#14213D',
      '#0D1729',
    ],
  },
  components: {
    Paper: {
      defaultProps: { withBorder: true },
      styles: {
        root: { boxShadow: 'none' },
      },
    },
    Table: {
      styles: {
        table: { fontVariantNumeric: 'tabular-nums' },
      },
    },
  },
})
