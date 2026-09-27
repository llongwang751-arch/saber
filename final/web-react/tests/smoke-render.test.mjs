// 烟雾渲染测试：用 esbuild 把 App（含全部组件与 Zustand store）打成 Node 可运行包，
// 以 react-dom/server renderToString 做服务端渲染，验证模块加载与渲染无运行时错误。
// 效果不依赖浏览器/DOM；fetch 与 localStorage 在打包入口内先行 mock。
import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')

const MOCKS = `
const __store = new Map()
globalThis.localStorage = {
  getItem: k => (__store.has(k) ? __store.get(k) : null),
  setItem: (k, v) => __store.set(k, String(v)),
  removeItem: k => __store.delete(k),
  clear: () => __store.clear(),
}
globalThis.fetch = async () => ({
  ok: true, status: 200,
  text: async () => '{}',
  json: async () => ({}),
})
globalThis.matchMedia = globalThis.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
`

const ENTRY = (appPath) => `${MOCKS}
const React = await import('react')
const { renderToString } = await import('react-dom/server')
const { default: App } = await import(${JSON.stringify(appPath)})
const html = renderToString(React.createElement(App))
if (!html.includes('AGI-saber')) throw new Error('brand text missing from render output')
if (!html.includes('登录')) throw new Error('auth modal missing from render output')
if (!html.includes('输入消息，Enter 发送')) throw new Error('chat input missing from render output')
process.stdout.write('SMOKE_OK length=' + html.length)
`

test('App smoke-renders to string without runtime errors', async () => {
  const { build } = await import('esbuild')
  // 临时目录放在项目内：react 从 ./node_modules 解析，App 用相对路径进入打包
  const dir = await mkdtemp(join(root, '.smoke-tmp-'))
  try {
    const entryPath = join(dir, 'entry.mjs')
    await writeFile(entryPath, ENTRY('../src/App.tsx'))
    const outfile = join(dir, 'bundle.mjs')
    await build({
      entryPoints: [entryPath],
      outfile,
      bundle: true,
      platform: 'node',
      format: 'esm',
      // react/react-dom 一并打入，bundle 自包含（临时目录无法向上解析 node_modules）；
      // banner 为被 externalize 的 node 内建模块（如 stream）提供 require 实现
      banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
      absWorkingDir: root,
      jsx: 'automatic',
      logLevel: 'silent',
    })
    const res = spawnSync(process.execPath, [outfile], { encoding: 'utf8', cwd: root, timeout: 60000 })
    assert.equal(res.status, 0, `smoke render failed:\nSTDOUT: ${res.stdout}\nSTDERR: ${res.stderr}`)
    assert.ok(res.stdout.includes('SMOKE_OK'), res.stdout)
  } finally {
    await rm(dir, { recursive: true, force: true }).catch(() => {})
  }
})
