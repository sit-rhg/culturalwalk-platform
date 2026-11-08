import { defineConfig, devices } from '@playwright/test'

/**
 * 端到端跑的是「真接口」模式：vite 把 /api 代理到本地 8000。
 * 5173 平时已经开着（`pnpm dev:real`），reuseExistingServer 直接复用；
 * 没开时才由 webServer 自己拉起来。
 */
const PORT = Number(process.env.E2E_PORT ?? 5173)
const baseURL = process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`
// 本机默认用系统 Chrome（免 `playwright install`）；CI 里没有 Chrome，
// 用 `E2E_CHANNEL=chromium` 走 Playwright 自带的 Chromium。
const CHANNEL = process.env.E2E_CHANNEL ?? 'chrome'
const browserUse = CHANNEL === 'chromium' ? {} : { channel: CHANNEL }

export default defineConfig({
  testDir: './e2e',
  // 主链路是一条串行故事：每一步都依赖上一步造出来的数据，不能并行
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 20_000 },
  reporter: [['list']],
  use: {
    baseURL,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    {
      name: CHANNEL === 'chromium' ? 'chromium' : 'chrome',
      // 本机没装 Playwright 自带浏览器，直接用系统 Chrome，免 playwright install；
      // CI 里用 E2E_CHANNEL=chromium 走自带的 Chromium。
      use: { ...devices['Desktop Chrome'], ...browserUse },
    },
  ],
  webServer: {
    // 直接起 dev-real.mjs，不依赖 pnpm 在 PATH 上；跨平台行为一致
    command: 'node ./scripts/dev-real.mjs',
    url: baseURL,
    reuseExistingServer: true,
    timeout: 120_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
})
