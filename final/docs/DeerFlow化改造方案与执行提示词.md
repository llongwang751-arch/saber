# DeerFlow 化改造方案与执行提示词

> 编写日期：2026-09-26。本文档分两部分：**第一部分**是改造方案（给人看，用于做决策）；**第二部分**是可以直接复制给另一个 AI agent 的执行提示词（给机器看）。

---

## 第一部分：改造方案

### 1. DeerFlow 的本质是什么

[DeerFlow](https://github.com/bytedance/deer-flow)（ByteDance，2025-05 开源，2.0 升级为"超级代理 harness"）的核心不是某个具体框架，而是四件事：

1. **研究计划 + 人在回路（Human-in-the-loop）**："人类设定议程，AI 负责执行"——AI 先产出结构化研究计划，人类可审批/编辑/拒绝，批准后才执行。
2. **多角色研究图**：协调者（意图路由）→ 规划者（生成计划）→ 研究员（多轮搜索/抓取/循环）+ 程序员（沙箱内写代码做数据分析）→ 报告员（汇总成带引用的报告）。
3. **迭代式深度研究**：不是一次性检索，而是"搜索 → 阅读 → 发现缺口 → 再搜索"的循环，带去重、来源登记、预算控制。
4. **开源工程形态**：聚焦的产品定位、conf.yaml 驱动的 LLM/工具配置、可插拔工具（含 MCP）、完整文档与示例、宽松许可证。

上游用 LangGraph 实现，但**这四件事没有一件必须用 LangGraph**。

### 2. 战略决策（改造前必须想清楚）

| 决策点 | 推荐 | 理由 |
|---|---|---|
| 运行时是否迁移 LangGraph | **否，保留原生运行时** | 现有 `run_scheduler`（租约/心跳/围栏）、`recovery`（动作日志重放）、SSE 断线重放已经是 DeerFlow 都没有的差异化资产；迁 LangGraph 等于推倒重来，且多引入一整套依赖 |
| 前端是否迁 Next.js（上游栈） | **否，保留 Vue 3** | 现有 Vue 资产（SSE 解析、审批面板、恢复面板）质量高，重写纯浪费 |
| 产品定位 | **收窄为"深度研究框架"**，通用对话为基础能力 | DeerFlow 是聚焦产品；本项目目前是医疗+农场+实验平台的大杂烩，开源必须有清晰一句话定位 |
| 医疗/农场/实验平台 | **feature flag 隔离，默认关闭，不物理删除** | 保留历史价值与演示能力，又不污染开源定位；是否彻底删除留给后续决策 |
| 评测平台 | **保留为核心卖点** | `internal/evaluation/` 是 DeerFlow 没有的差异化能力 |
| 许可证 | MIT | 上游同类项目均为宽松许可 |

**一句话定位建议**：一个生产级深度研究框架——原生异步运行时（持久化 run、断线恢复、动作日志重放）+ 混合检索（Milvus/ES/知识图谱 RRF 融合）+ 计划级人在回路 + 内置评测平台。

### 3. 现状 → 目标映射表

| 现有模块 | 现状 | 处置 |
|---|---|---|
| `internal/agent/run_scheduler.py`、`run_repository.py`（SQLite 台账、租约/心跳/围栏） | 已是持久化 run 层 | **保留**，仅扩状态机 |
| `internal/agent/recovery.py`（动作日志重放、TaskLease） | 已有断点恢复 | **保留** |
| `internal/agent/planner.py:118` `llm_plan_graph`（一次性 DAG，执行不可干预） | 计划生成即执行 | **改造**：产出研究计划后暂停等待人工审批 |
| `internal/agent/planning_service.py` + `graph_runtime.py`（拓扑分层并行执行） | 一次性执行 | **改造**：接入"暂停-审批-恢复"状态机 |
| `internal/agent/subagents.py`（research/writer/review/doc 固定 DAG `planner.py:432`） | 单目标单轮子代理 | **改造**：research 改迭代循环（搜索→缺口→再搜索），review 升级为引用校验，新增 coder 角色 |
| `internal/harness/approval.py`（工具级 HITL，危险工具拦截） | 只有工具级审批 | **保留**，新增计划级审批通道 |
| `internal/rag/hybrid.py`（Milvus+ES+Neo4j RRF 融合、熔断降级） | 完整 | **保留** |
| `internal/tools/mcp_client.py` + `tools/tools.py` | 已有 MCP 与工具契约 | **保留**，声明式注册化 |
| `internal/agent/artifact.py`（单文件 markdown） | 无引用管理 | **改造**：结构化报告（frontmatter + 引用编号 + 来源表） |
| `internal/resilience/budget.py` | 每回合预算 | **复用**到研究循环 |
| 医疗（`application/medical*.py`、MedicalTriageAgent 等）、农场、在线实验 | 业务域代码 | **feature flag 隔离**，默认关闭 |
| `config/config.py`（657 行 Go 对齐配置） | 过重 | **精简**为 conf.yaml + 环境变量 |
| Go-parity 契约测试（`test_go_parity_contract.py` 等） | 与 Go 版本对齐用 | **退役**（进 git 历史即可） |
| Vue 前端（`App.vue` 单页、ThinkPanel、ToolApprovals、TaskRecovery、RunWorkbench） | 无路由的单页 | **扩展**：新增研究工作台（计划审批、进度、报告阅读） |
| `internal/evaluation/` + `scripts/run_eval_300.py` 等 | 完整评测体系 | **保留**并对外宣传 |

### 4. 分阶段里程碑（每阶段独立可交付、可验收）

- **M0 定位收窄与开源脚手架**：新名称、MIT LICENSE、双语 README、业务域 feature flag 隔离、退役 Go-parity 测试。
- **M1 计划级 HITL**：计划 schema + `AWAITING_PLAN_REVIEW` 状态 + 审批/编辑/拒绝 API + 前端审批面板。
- **M2 迭代研究员 + 程序员角色**：多轮搜索循环、来源登记与去重、budget 截断、沙箱 coder。
- **M3 引用化报告**：大纲→分节写作→引用插入→参考文献；报告 artifact 结构化。
- **M4 前端研究工作台**：计划审批 UI、研究进度、报告阅读（引用悬浮）。
- **M5 开源化收尾**：conf.yaml 精简、声明式工具注册、examples/、docs/、docker-compose 精简、v0.1.0 发布。

每阶段的详细任务与验收标准写在第二部分提示词内，作为 agent 的执行契约。

### 5. 风险与保护措施

1. **回归风险**：任何时刻 `pytest` 全绿、`ruff check` 全绿是硬门槛；SSE 事件名契约、恢复/取消语义（租约 TTL 90s、围栏）不许破坏。
2. **范围失控**：agent 按阶段工作，每阶段结束停下汇报，不允许跨阶段顺手改。
3. **假开源**（有代码没文档没示例）：M5 的验收标准是"克隆 → 拷 conf → 跑通一个研究 case"，跑不通不许发布。
4. **新能力依赖外部服务**：搜索/抓取必须可 mock，测试离线可跑。

---

## 第二部分：给 AI Agent 的执行提示词

> 使用方式见文末"投喂建议"。`{{ }}` 为需要你先填好的决策项。

```markdown
# 任务：将本项目改造为 DeerFlow 形态的开源深度研究框架

## 你的使命
把当前仓库（一个多业务域 agent 平台）改造为一个聚焦的、可开源的深度研究（deep research）
框架。形态对标 ByteDance 的 DeerFlow：计划生成 → 人工审批 → 多角色迭代研究（研究员 +
程序员）→ 带引用的研究报告。但运行时保留本项目的原生实现，不迁移 LangGraph。

## 项目现状（先读这些文件再动手）
- 仓库根：本提示词所在仓库；所有后端代码在 `final/` 下，前端在 `final/web/`。
- 必读：
  1. `final/internal/agent/planner.py` —— 现有一次性计划 DAG（`llm_plan_graph`，118 行起；
     固定研究 DAG `research→writer→review→doc` 在 432 行附近）
  2. `final/internal/agent/planning_service.py` + `graph_runtime.py` —— TaskGraph 构建
     与拓扑分层并行执行
  3. `final/internal/agent/run_scheduler.py`、`run_repository.py`、`run_contracts.py`
     —— 持久化 run 层：SQLite 台账、线程池 worker、心跳/租约/围栏、取消令牌
  4. `final/internal/agent/recovery.py` —— 动作日志重放恢复（不确定副作用禁止自动重放）
  5. `final/internal/handler/chat_routes.py`（SSE 事件契约：start/route/plan_created/
     node_start/token/tool_call/rag_result/done）与 `final/internal/handler/run_routes.py`
     （Last-Event-ID 断线重放）
  6. `final/internal/harness/approval.py` —— 现有工具级 HITL 插件（审批持久化到动作日志）
  7. `final/internal/agent/subagents.py` —— 现有子代理（research/writer/review/doc +
     医疗专用）
  8. `final/internal/agent/artifact.py`、`internal/resilience/budget.py`、
     `internal/tools/mcp_client.py`、`internal/rag/hybrid.py`
  9. `final/web/src/App.vue`、`components/ToolApprovals.vue`、`components/TaskRecovery.vue`、
     `composables/useSSE.js`
  10. `final/config/config.py`、`.env.example`、`docker-compose.yml`

## 战略决策（已定，不要重新讨论、不要提出替代方案）
- 保留原生运行时（run_scheduler / graph_runtime / recovery），禁止引入 LangGraph /
  LangChain 依赖。
- 保留 Vue 3 前端，禁止迁移 Next.js。
- 产品定位收窄为深度研究框架；医疗、智慧农场、在线实验三大业务域通过 feature flag
  隔离，默认关闭，禁止物理删除业务代码。
- 新项目名：{{PROJECT_NAME，例如 SaberFlow}}；许可证 MIT。
- 评测平台（internal/evaluation/）是核心卖点，保留并保持可用。

## 硬约束（每一步都适用）
1. 任何时刻 `cd final && python -m pytest` 全绿；动手前先跑一次基线并记录。
2. 任何时刻 `ruff check .` 全绿（规则见 final/pyproject.toml）。
3. 不破坏：SSE 既有事件名与顺序；run 的租约/围栏/取消语义；既有测试的断言意图
   （允许为行为变化改测试，但必须在 commit message 里说明为什么行为变了）。
4. 新增外部能力（web 搜索、网页抓取）必须提供 mock 实现，测试离线可跑；
   真实实现走现有 Tavily/MCP 通道。
5. 小步提交：每个任务至少一个 commit，conventional commits 风格，中文描述，
   如 `feat(plan): 计划审批状态机与 API`。
6. 禁止修改 `alembic/` 已有迁移文件；schema 变更只允许追加新迁移。
7. 每阶段完成后停下，输出该阶段验收自检表，等指令再进入下一阶段。

## 分阶段任务

### M0 定位收窄与开源脚手架
任务：
- 在 `final/config/config.py` 增加业务域开关 `AGI_ENABLE_MEDICAL` /
  `AGI_ENABLE_FARM` / `AGI_ENABLE_EXPERIMENTS`（默认 false）：
  对应路由在 handler 组装处条件注册，医疗子代理不注册进 SubAgentRegistry，
  前端对应面板（MedicalDashboard 等）按 `/api/status` 返回的开关隐藏。
- 退役 Go-parity：将 `test_go_parity_contract.py` 等一致性测试移到
  `final/tests/legacy_parity/` 并默认 skip（`@pytest.mark.skip` + 说明），同时删除
  CI 中对它们的强依赖（如有）。
- 根目录（或 final/ 下按现有仓库习惯）新增：`LICENSE`（MIT，版权人 {{版权人}}）、
  `README.md`（英文，含架构图 Mermaid、快速开始、特性列表）、`README.zh-CN.md`（中文版）。
- 仓库改名残留清理：README 与文档中的产品名统一为 {{PROJECT_NAME}}（代码内部
  标识符暂不改名，避免大范围 diff）。
验收：
- 默认配置启动后，`/api/status` 报告三个业务域均为 disabled；研究/对话核心功能不受影响。
- 全量测试绿（skipped 的 legacy 测试除外）；README 中每个声称的特性都有对应代码路径。

### M1 计划级 HITL（最高优先级的核心差异）
任务：
- 定义研究计划 schema：`{objective, constraints[], steps:[{id, title, kind:
  research|code|write, guidance, tool_policy, depends_on[], acceptance}]}`，
  放在 `internal/agent/plan_contracts.py`（新文件），带 schema 校验。
- 改造 `llm_plan_graph` 增加研究计划产出路径（保留旧 DAG 路径兼容非研究意图）。
- 扩展 run 状态机：`plan_created` 之后进入 `AWAITING_PLAN_REVIEW`，run 暂停
  （复用 run_scheduler 的持久化，注意：暂停必须幂等、崩溃后重连仍是待审批状态）。
- 新 API（挂到现有 run 路由旁）：
  - `GET  /api/agent-runs/{id}/plan`
  - `POST /api/agent-runs/{id}/plan/review`，body `{action: approve|edit|reject,
    steps?}`；edit 用提交的 steps 覆盖并写回台账；reject 终止 run。
- 审批通过后从暂停点继续执行（复用 GraphRuntime 从指定步骤恢复的能力，参考
  recovery.py 的节点重放思路，但这里是"未开始节点正常调度"）。
- 前端：新增 `PlanReview.vue`（参考 ToolApprovals.vue 的轮询/操作模式）：
  展示计划步骤，支持内联编辑标题/指引/删除步骤、批准、拒绝。
- 新测试 `final/tests/test_plan_review_flow.py`：approve 直通、edit 后按新计划执行、
  reject 终止、暂停态断线重连（SSE Last-Event-ID）后仍可审批。
验收：
- 端到端：发起研究请求 → run 暂停待审批 → 编辑一个步骤 → 批准 → 执行继续 → run 完成。
- 工具级 HITL（harness/approval.py）行为不变。

### M2 迭代研究员 + 程序员角色
任务：
- 改造 `subagents.py` 的 ResearchAgent 为迭代循环：每轮 = 生成查询 → 检索/搜索 →
  阅读与要点抽取 → 缺口分析 → 决定继续或收敛；轮数上限与 token 上限接入
  `internal/resilience/budget.py`。
- 来源登记：新增 `SourceLedger`（进程内 + 随 run 持久化），每条要点登记
  `{source_id, url_or_doc_id, quote, round}`；URL 与语义指纹两级去重。
- 新增 CoderAgent：写 Python 代码并在现有 Docker 沙箱（`internal/agent/init_sandbox.py`
  + `exec_command` 工具）内执行，输出结构化结果；无沙箱环境时优雅降级为"仅生成代码
  不执行"并明示。Coder 只暴露受限工具集。
- 事件流：新增 `research_round`、`source_found`、`code_exec` 三类 SSE 事件
  （追加到事件契约末尾，不改既有事件）。
- 新测试：迭代收敛（mock 搜索）、budget 截断、去重、coder 沙箱失败降级。
验收：
- 对一个 mock 知识库的研究 run 产生 ≥2 轮迭代且 sources 无重复 URL；预算耗尽时
  优雅收敛并输出已有发现。

### M3 引用化报告
任务：
- Reporter 流程：根据 SourceLedger 与要点生成大纲 → 分节写作（逐节流式 token）→
  关键结论插入 `[n]` 引用 → 文末参考文献表（编号 ↔ source_id/url 映射）。
- 报告 artifact 升级（改 `internal/agent/artifact.py`）：YAML frontmatter
  （topic、plan 摘要、生成时间、run_id）+ 正文 + References；保存路径与现有
  workspace/doc library 一致。
- 引用校验：review 角色改为引用校验器——检查每个 `[n]` 有对应来源、来源均被
  至少引用一次，问题写回重写提示。
- 新测试 `test_report_citations.py`：编号连续、无悬空引用、来源全覆盖。
验收：
- 产出的报告每一处 `[n]` 都可点击追溯到 SourceLedger 条目；无引用悬空。

### M4 前端研究工作台
任务：
- 引入 vue-router（研究工作台 / 会话聊天 / 文档库 三视图；保持无 TS）。
- 研究工作台页面：左侧计划审批（M1 的 PlanReview）与研究步骤进度（消费
  research_round/source_found/code_exec 事件）；右侧报告阅读器（引用 [n] 悬浮
  显示原文片段，来自 SourceLedger）。
- 保留现有单页所有组件入口，不删除任何既有功能路径。
- 端到端联调脚本 `final/scripts/demo_research.py`：一条命令从发起 run 到
  打印报告。
验收：浏览器手工走通"输入主题 → 审批编辑计划 → 观察进度 → 阅读带引用报告"。

### M5 开源化收尾
任务：
- 配置精简：新增 `final/config/conf.example.yaml`（LLM provider/base_url/key、
  嵌入模型、搜索 key、RAG profile、业务域开关），旧 config.yaml 保持兼容并在
  文档标注为进阶配置。
- 工具声明式化：新增 `final/config/tools.example.yaml`（内置工具开关 + MCP
  server 列表），启动时按声明注册（走现有 register_mcp_server）。
- `examples/`：两个示例（本地 lightweight RAG profile 的离线示例 + 接 Tavily
  的在线示例）、示例 conf、curl 脚本。
- `docs/`：`architecture.md`（对照 Mermaid 图讲清 run 生命周期与状态机）、
  `quickstart.md`、`CONTRIBUTING.md`、`SECURITY.md`。
- docker-compose 拆 profile：`core`（仅必依赖）/ `full`（现有全部）。
- 发布：打 tag `v0.1.0`。
验收：干净克隆 → `cp conf.example.yaml conf.yaml` → 按 quickstart 启动 →
跑通 demo_research.py 全流程；CI（.github/workflows/tests.yml）绿。

## 完成定义（Definition of Done，全局）
- [ ] 所有阶段验收标准逐条满足
- [ ] `pytest` 全绿、`ruff check` 全绿、CI 绿
- [ ] README 双语与代码行为一致，无夸大描述
- [ ] 医疗等业务域在默认配置下不可见但可一键开启
- [ ] 每个 SSE 事件在 docs/architecture.md 中有文档

## 工作方式
- 先读后写：改任何文件前先读它；对状态机/调度器这类核心文件，先在回复里概述
  你理解的生命周期，确认与代码一致后再改。
- 测试驱动：新能力先写失败测试再实现。
- 诚实汇报：测试失败就贴输出，不要为了绿而改断言；发现既有代码 bug 单独
  commit 修复并说明。
- 每阶段末尾输出：完成的任务清单 / 验收自检表（逐条打勾）/ 遗留问题与建议。
```

---

## 投喂建议

1. **推荐：按阶段投喂**。每次消息 = 完整提示词 + 一句话「本次执行 M1，完成后停下」。
   上下文压力小，agent 不容易跑偏，你也保留了每阶段的否决权。
2. 长上下文 agent（如 200k+ 窗口）可以整份给，但提示词里已写明「每阶段停下汇报」，
   不要让它一口气跑完 M0–M5。
3. **先填决策项**：`{{PROJECT_NAME}}`、`{{版权人}}`。如果你想彻底删除医疗业务而不是
   feature flag 隔离，把硬约束第 3 条和 M0 对应任务改写为「物理删除 + 测试同步删除」。
4. 提示词中的文件路径与行号基于 2026-09-26 的代码库（`python` 分支）；若 agent 开工前
   代码有大幅变动，让它先重读「项目现状」清单里的文件并核对路径。
5. 每阶段验收时用这段话检查 agent 产出：「贴出该阶段验收标准，逐条给出证据
   （命令输出 / 文件路径 / 截图），不允许只说‘已完成’。」
