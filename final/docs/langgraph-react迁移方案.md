# LangGraph + React 全栈迁移方案（对照 DeerFlow）

> 决策日期：2026-09-27。用户决策：全量对照 DeerFlow 技术栈——编排层迁移 LangGraph，前端迁移 React（后端 FastAPI 保持不变）。
> 本文档是迁移的执行契约：所有 agent 与后续会话按此执行，阶段必须串行验收。

## 0. 终态定义（End State）

| 层 | 现状 | 终态 |
|---|---|---|
| HTTP 层 | FastAPI（`internal/handler/`） | **不变**，SSE 事件契约逐字保留 |
| 编排层 | 自研 `turn_service` / `graph_runtime`（研究模式） | **LangGraph StateGraph**（`internal/research_graph/`），chat 三模式最终也入图 |
| 状态持久化 | 运行台账 SQLite + `run_scheduler` 调度 | LangGraph `SqliteSaver` 检查点 + **保留事件表与 SSE Last-Event-ID 重放** |
| 研究引擎 | `internal/research/`（自研循环） | 节点化：plan → interrupt 审批 → Send 并行研究 → coder → report |
| 前端 | Vue 3 + Pinia（`final/web/`） | **React 18 + Vite + TypeScript + Zustand**（构建产物仍由 FastAPI 静态挂载 `/`） |
| 工具/RAG/沙箱/预算/护栏 | `internal/tools` `rag` `harness` `resilience` | **不变**（LangGraph 节点直接调用现有客户端，不引入 LangChain chat model 抽象） |
| 评测平台 | `internal/evaluation/` | 不变 |

明确不引入：LangChain ChatModel/AgentExecutor 抽象（LLM 调用继续走 `internal/llm`，结构化输出走现有 JSON 契约）、Next.js（部署模型是 FastAPI 静态托管 SPA，用 Vite+React 保持一致；与 DeerFlow 的 Next.js 差异记录在 README）。

## 1. 阶段划分（每阶段独立验收、可回退）

### L1 LangGraph 研究引擎（本阶段）
- 新增 `final/internal/research_graph/`：`graph.py`（StateGraph：plan → **interrupt()** 审批 → Send 并行 research → coder → report）、`state.py`（TypedDict 状态与 reducer）、`adapter.py`（把图事件桥接为现有 SSE 词汇：`plan_created`/`token`/`node_start`/`research_round`/`source_found`/`done`，事件名与顺序不许变）
- 检查点：`SqliteSaver`，库文件 `runtime/langgraph-checkpoints.sqlite3`；审批编辑通过 `Command(resume=...)` 注入
- 接线：配置项 `research.engine: native | langgraph`（默认 `native`），`run_service` 按 engine 分发；两条后端共用同一 run 台账与事件表
- 预算/来源账本/引用校验复用 `internal/research/` 现有类（节点内调用，不重写）
- 测试：`tests/test_research_graph.py` —— 审批三态（approve/edit/reject）、并行步骤、与 native 引擎同一组确定性 mock 用例跑出等价报告、检查点崩溃恢复
- 验收：pytest + ruff 全绿；`research.engine: langgraph` 下离线示例跑通；默认行为与现在完全一致

### L2 React 前端（与 L1 并行开发，独立目录）
- 新增 `final/web-react/`：React 18 + Vite + TypeScript + Zustand；组件与现有 Vue 一一对应（App/ChatMessage/PlanReview/ResearchReport/RunWorkbench/ToolApprovals/TaskRecovery/AuthModal/EvaluationDashboard/DocViewer/SkillHub…），API 客户端与 SSE 解析逻辑逐字移植（`useSSE` 的 id/retry/Last-Event-ID 语义不变）
- 自带 `package.json`、`node --test` 测试（移植现有 9 项 + 关键交互新测）、`npm run build` 产出 dist
- 本阶段不改 `web/`、不改后端、不改 CI——两套前端并存

### L3 切换与退役（用户浏览器验收 React 版后执行）
- `web/dist` 指向 React 构建产物：`web/` 整体退役删除，`web-react/` 更名 `web/`；CI 与文档同步
- `research.engine` 默认值切 `langgraph`；chat 三模式入图（`internal/research_graph/chat_graph.py`）；`turn_service`/`graph_runtime` 原生编排路径退役删除
- README / quickstart / architecture 文档全面改写为新栈；教学路线重排
- 验收：全量测试绿 + 浏览器手工回归（对照 Vue 版 13 类边缘交互）

## 2. 不变量（任何阶段不许破坏）

1. HTTP API 与 SSE 事件契约：端点、事件名、事件顺序、Last-Event-ID 语义
2. 运行台账与事件表 schema（alembic 迁移不动）
3. 预算治理、工具级 HITL 审批、沙箱隔离语义
4. 测试与 ruff 全绿是每一阶段的提交门槛

## 3. 风险与对策

| 风险 | 对策 |
|---|---|
| LangGraph 检查点与自研台账双写不一致 | 台账仍是唯一事实源，检查点只存图状态；adapter 单向同步 |
| React 重写交互回归 | L2 不删 Vue，浏览器对照验收后才切 L3 |
| langgraph 依赖引入破坏现有 pinned 依赖 | 独立新增最小集（langgraph + langchain-core），装完跑全量回归 |
| 面试叙事混乱 | 叙事统一为："LangGraph 做编排（interrupt/checkpoint/Send），自研台账做接管与审计——分别解决不同层的问题" |

## 4. 教学路线影响

第三课起的课程改在新栈上讲：L3 持久化 run 层（仍成立，事件表未变）→ LangGraph 内部机制课（StateGraph/checkpointer/interrupt/Send，对照自研 graph_runtime）→ React 前端课。已上的第一、二课内容在 L3 切换前仍然有效。
