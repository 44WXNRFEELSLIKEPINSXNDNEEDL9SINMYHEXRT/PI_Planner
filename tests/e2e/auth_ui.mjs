/**
 * Браузерная проверка входа и ролей (ADR-027): экран входа, неверный токен, viewer без
 * права загрузки, выход, admin со скачиванием шаблона. Запуск — tests/e2e/run_auth_ui.sh.
 * Переменные: BASE, VIEWER (токен viewer), ADMIN (токен администратора), OUT (скриншоты),
 * CHROME (путь к chromium, необязательно).
 */
import { createRequire } from 'node:module'
// require, а не import: так пакет находится и через NODE_PATH (ESM его игнорирует).
const { chromium } = createRequire(import.meta.url)('playwright-core')
const BASE = process.env.BASE, VIEWER = process.env.VIEWER, ADMIN = process.env.ADMIN, OUT = process.env.OUT || '.'
let failed = 0
const log = (ok, msg) => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`) }
const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined, args: ['--no-sandbox'] })
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, acceptDownloads: true })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', e => errors.push(String(e)))

await page.goto(BASE)
await page.getByLabel('Токен доступа').waitFor({ timeout: 10000 })
log(true, 'без токена показан экран входа')
await page.screenshot({ path: `${OUT}/01-login.png` })

await page.getByLabel('Токен доступа').fill('definitely-wrong-token-1')
await page.getByRole('button', { name: 'Войти' }).click()
await page.getByText('Токен не подошёл').waitFor({ timeout: 5000 })
log(true, 'неверный токен: понятное сообщение')

await page.getByLabel('Токен доступа').fill(VIEWER)
await page.getByRole('button', { name: 'Войти' }).click()
await page.getByText('vera', { exact: true }).waitFor({ timeout: 10000 })
// The role warning belongs to UploadScreen; the landing page is now SummaryScreen.
await page.goto(`${BASE}/#/upload`)
await page.getByText('Режим просмотра').waitFor({ timeout: 10000 })
log(true, 'viewer вошёл, видит «Режим просмотра»')
log(await page.getByText('Загрузка факта').count() === 0 && await page.getByText('Скачать шаблон').count() === 0, 'viewer не видит загрузку факта')
log(await page.getByText('vera', { exact: true }).count() > 0, 'в шапке имя пользователя')
await page.screenshot({ path: `${OUT}/02-viewer.png` })
await page.goto(`${BASE}/#/plan`)
await page.getByText('План квартала').first().waitFor({ timeout: 10000 })
await page.waitForTimeout(1500)
log(await page.getByText('API недоступен').count() === 0, 'viewer читает данные плана')

await page.getByRole('button', { name: 'Выйти' }).click()
await page.getByLabel('Токен доступа').waitFor({ timeout: 5000 })
log(true, 'выход возвращает на экран входа')
log(await page.evaluate(() => !sessionStorage.getItem('pi-planner-token') && !localStorage.getItem('pi-planner-token')), 'токен удалён из хранилища')

await page.getByLabel('Токен доступа').fill(ADMIN)
await page.getByRole('button', { name: 'Войти' }).click()
await page.goto(`${BASE}/#/upload`)
await page.getByText('Загрузка данных').first().waitFor({ timeout: 10000 })
await page.waitForTimeout(1500)
log(await page.getByText('Режим просмотра').count() === 0, 'admin: нет баннера «Режим просмотра»')
const dl = page.waitForEvent('download', { timeout: 8000 }).catch(() => null)
await page.getByText('Скачать шаблон').first().click()
const d = await dl
log(!!d && /actuals_sprint_\d+\.csv/.test(d.suggestedFilename()), `шаблон скачан с токеном: ${d?.suggestedFilename()}`)
await page.screenshot({ path: `${OUT}/03-admin-upload.png` })
log(errors.length === 0, `нет необработанных ошибок JS ${errors.join('|')}`)
await browser.close()
process.exit(failed ? 1 : 0)
