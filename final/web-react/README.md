# AGI-saber web（React 版，`agi-saber-web-react`）

Vue 3 + Pinia 版前端的 **React 18 + Vite + TypeScript + Zustand** 平行重写（迁移方案 L2 阶段产物，
见 `final/docs/langgraph-react迁移方案.md`）。与 `final/web/`（Vue 版）功能对等、共用同一套
FastAPI 后端与 SSE 事件契约；本目录自包含，可独立开发、构建与测试。

## 技术栈

- React 18 + Vite 5 + TypeScript（严格模式）
- Zustand 5（与 Vue 版 Pinia store 一一对应；无路由——单页 + 视图状态，与 Vue 版同模式）
- `node --test`（Node 24 原生 TS 类型剥离，离线可跑，无需浏览器）

## 常用命令

```bash
npm install        # 安装依赖
npm run dev        # 开发服务器 :5174，/api、/healthz、/readyz 代理到 http://127.0.0.1:8090
npm run build      # 产出 dist/（可由 FastAPI 静态托管，与 Vue 版同一挂载约定）
npm run preview    # 本地预览构建产物 :4174
npm test           # node --test 全部离线测试（SSE 解析器、研究工具、引用渲染、App 烟雾渲染）
npm run typecheck  # tsc --noEmit
```

## 目录结构

```
src/
  api/client.ts        # 统一 fetch 封装（Bearer 注入、401 回调、fetchJSON）——逐字移植
  lib/sse.ts           # SSE 流式解码器（id/retry/Last-Event-ID 语义）——逐字移植、框架无关
  utils/research.ts    # 计划审批载荷、引用 token 化、报告分块、事件标签——逐字移植
  utils/markdown.ts    # 自包含 Markdown → 安全 HTML——逐字移植
  stores/              # Zustand stores：auth / chat / sessions / docs / skills / evaluation
  components/          # 与 Vue 版一一对应的 19 个组件
  assets/              # styles.css、research.css（全局）+ panels.css（原 Vue scoped 样式聚合）
tests/                 # 9 项既有测试移植 + SSE/引用新测 + App 服务端烟雾渲染
```

## 测试说明

- `tests/sse.test.mjs`、`tests/research.test.mjs`：移植自 `final/web/tests/` 的 9 项既有断言。
- `tests/sse-parser.test.mjs`：SSE 解析器补充语义（默认事件名、粘性 id、NUL id 忽略、
  CR-only 行、final 冲洗、坏 JSON 复位、回调停播）。
- `tests/citation.test.mjs`：引用编号映射、锚点剥离、`javascript:` 链接纯文本化、
  报告分块与 Markdown 转义。
- `tests/smoke-render.test.mjs`：用 esbuild 打包 App 后以 `react-dom/server` 渲染整棵树，
  验证模块加载与渲染零运行时错误（离线，mock fetch/localStorage）。

## 与 Vue 版的对应关系

| Vue（`final/web/src`） | React（本目录） |
|---|---|
| `stores/auth|chat|sessions|docs|skills|evaluation.js`（Pinia） | `stores/*.ts`（Zustand，actions/state 一一对应；getter 变选择器） |
| `composables/useSSE.js` | `lib/sse.ts` |
| `utils/research.js` / `utils/markdown.js` | `utils/research.ts` / `utils/markdown.ts` |
| `App.vue` + 18 个组件 | `App.tsx` + 19 个组件（含 EvolutionSuggestionPanel） |
| `assets/styles.css`、`research.css`、SFC scoped styles | `assets/styles.css`、`research.css`、`panels.css` |

流式语义说明：Pinia 靠响应式代理就地修改消息对象；Zustand 版保持同样的就地修改
（`addMessage` 返回同一对象引用），每次事件后通过 `sessions.touch()` 克隆数组触发 React 订阅。

## 与 DeerFlow 的差异记录

按迁移方案 §0：编排迁移 LangGraph、前端迁 React，但部署模型保持 **FastAPI 静态托管 SPA**
（Vite 构建 + 同源 `/api`），不引入 Next.js；SSE 事件契约与 `Last-Event-ID` 重放语义逐字保留。
