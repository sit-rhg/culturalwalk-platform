import { defineConfig, mergeConfig } from 'vitest/config'

import viteConfig from './vite.config'

/**
 * vitest 只在 vitest.config.ts 存在时用它、并忽略 vite.config.ts，
 * 所以这里合并 vite 配置，保住 @ 别名和 vue 插件。
 *
 * e2e/ 下跑的是 Playwright，而 vitest 默认会把所有 spec 文件都收进来，
 * 结果 `pnpm test` 在收集阶段就报 “Playwright Test did not expect test() to be called here”。
 */
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      exclude: ['**/node_modules/**', '**/dist/**', 'e2e/**'],
    },
  }),
)
