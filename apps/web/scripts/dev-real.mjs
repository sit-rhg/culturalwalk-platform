#!/usr/bin/env node
/**
 * 真接口开发服务器：pnpm dev:real
 *
 * 为什么不用 "VITE_USE_MOCK=false vite ..." 直接写在 package.json 里：
 * npm script 在 Windows 上走 cmd，前缀式环境变量赋值不生效，也没装 cross-env。
 * 这里用 spawn 把变量塞进子进程环境，两端行为一致，且不引入新依赖。
 */
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const viteBin = fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url))
const child = spawn(process.execPath, [viteBin, '--port', '5173'], {
  stdio: 'inherit',
  env: { ...process.env, VITE_USE_MOCK: 'false' },
})
child.on('exit', (code) => process.exit(code ?? 0))
