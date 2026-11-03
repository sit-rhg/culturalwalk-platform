#!/usr/bin/env node
/**
 * 真接口开发服务器：pnpm dev:real
 *
 * 为什么不用 "VITE_USE_MOCK=false vite ..." 直接写在 package.json 里：
 * npm script 在 Windows 上走 cmd，前缀式环境变量赋值不生效，也没装 cross-env。
 * 这里用 spawn 把变量塞进子进程环境，两端行为一致，且不引入新依赖。
 */
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'

const require = createRequire(import.meta.url)
// 只解析包位置再拼 bin：vite 5.4.21 的 exports 没有暴露 ./bin/vite.js，
// require.resolve('vite/bin/vite.js') 会报 ERR_PACKAGE_PATH_NOT_EXPORTED；
// 而 ../node_modules/vite/bin/vite.js 依赖 pnpm 的平铺布局，换个 node-linker 就找不到。
const viteBin = join(dirname(require.resolve('vite/package.json')), 'bin', 'vite.js')

const child = spawn(process.execPath, [viteBin, '--port', '5173'], {
  stdio: 'inherit',
  env: { ...process.env, VITE_USE_MOCK: 'false' },
})

// 把中断信号转给 vite：否则 Ctrl+C 只杀掉这层壳，5173 会被孤儿进程继续占着
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal))
}

child.on('exit', (code, signal) => {
  // 被信号杀掉不是成功：按 signal 退出时如果把空 code 当 0，脚本会谎报通过
  process.exit(code ?? (signal ? 1 : 0))
})
