# AGI-saber-python：优化记录与开源项目对照

> 本文保留前期评审与上游版本依据，其中“尚未完成”是当时状态。2026-09-19 后续已补主 Agent 恢复、SQLite 记忆事务一致性及自动异机备份，最新逐项状态见 [恢复与备份验收](recovery-memory-backup-20260919.md)。

本次只处理 `AGI-saber-python/final`。工作区原本已有大量修改；没有重置、提交或改动其他版本。上游比较基于官方仓库公开架构与文档，借鉴机制，自行实现，没有把三个项目整套移植进来。

## 判断

适合做轻量借鉴。当前项目的价值在于把知识检索、个人记忆、工具执行放进同一个 Python Agent，且已有 SQL 主存储、事务 outbox、检索降级、测试与评测框架。主要短板是模块之间的真实执行契约：组件存在，不等于主流程正确使用；测试多，也不等于真实模型质量已经验证。

不建议再增加一套独立的“高级 RAG”“高级 Memory”或“第三套 Harness”。应该围绕现有入口补正确性，然后在真实样本上决定是否增加复杂能力。

## 与三个上游的区别

| 对照 | 上游定位 | 本项目更适合的边界 | 本次借鉴 |
|---|---|---|---|
| [WeKnora](https://github.com/Tencent/WeKnora) | 包含文档治理、检索问答、Agent、Wiki 和工作空间权限的知识产品 | 嵌入 Agent 的轻量知识服务 | 把检索分数与可回答性分开；事实引用携带证据 ID、原文与文档来源 |
| [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | 面向多 Agent 的团队记忆服务；Chat Memory 包含 Conversation、Atom、Scenario、Persona 层，并扩展其他记忆资产 | 用户级偏好与事实，加会话级 STM 和任务状态 | 原子事实键、来源可信度、更新优先于相似度去重 |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | 以插件组合模型、工具、循环、会话和运行环境的 Agent 框架；官方仍标记开发者预览 | 保留 Python 主调度器，统一执行策略与副作用派发 | 主 Agent 与独立 Harness 共用工具拦截边界；审批绑定、消费、执行日志分开 |

调研时固定的上游提交：

- WeKnora：`2a6a9c251734d12bf65bf17d8f3412689ebed4e1`，`main`。
- TencentDB Agent Memory：`41dee1f9f8cd2b7e87f3e5c073966dc58d296a16`，`feat/server_team`。
- DeepSeek Harness：`ddefc45fbc7f8e46dd73185e68295696d1297887`，`master`。

上表不代表本项目已经达到上游的功能、规模或质量，也不是三个上游的算法复刻。

## 按截图顺序落实的改动

### P0：会话与执行安全

**会话隔离。** `/api/conversations` 返回服务端生成的 ID。Vue 发消息时携带该 ID，数据库历史按 `user_id + conversation_id` 查询。每个会话使用独立 STM、任务缓冲、取消注册表和执行锁；用户长期记忆、基础设施和后台写入器共享。同一会话不能并发执行两轮，不同会话不会把各自聊天历史混入对方。默认兼容会话仍接受旧客户端，但不会恢复到具名会话。新增 `0018_conversation_history` 迁移。

**统一策略边界。** `internal/harness/execution.py` 实现 fail-closed 插件拦截。主 Agent 的图工具、旧直接工具入口、子 Agent 调用，以及独立 Harness 的 ReAct/DAG 都经过这层。被拦截后不会继续调用对应工具。DAG 按依赖层并行；主调度器遇到待审批或不确定写入时中断，不让 replanner 自动换个节点重试。

**审批。** 审批绑定用户、会话、调用 ID、工具和参数摘要，默认十分钟有效；SQL compare-and-set 完成 pending → approved/rejected → consumed。请求 ID 来自绑定内容，重复请求不能绕开已经消费的审批。前端展示工具与参数，提供批准并执行、拒绝按钮。批准执行的是保存的参数，不让模型临时替换动作。`confirm=true` 不能代替服务端审批；命令确认由已消费的人类授权赋予。

**副作用。** `internal/agent/tool_execution.py` 为主 Agent 的写操作写入持久化派发记录。相同调用成功后可读取已存结果；运行中、失败或超时的不确定动作禁止自动重复派发。MCP 默认保守地视为写操作；明确只读的工具才可以恢复自动重试。副作用工具禁止参加竞速组。

边界：这是同一调用的防重复派发，不是外部系统的 exactly-once。Python 无法强杀任意阻塞线程；超时后底层操作仍可能结束，因此必须核对不确定结果。用户重新提出一个新任务仍属于新调用。审批界面恢复单个确定动作，不会自动恢复整张多步骤任务图。

### P1：记忆、证据与评测

**记忆。** 主流程规则抽取接入槽位提取，排除引用示例与不安全内容；偏好先持久化再更新缓存和确认。用户事实加入 `factkey` 和来源标签。同一事实变更先执行带版本条件的事务更新，然后更新缓存；不同事实不能因 embedding 相似而合并，后续 consolidation 也保护这些原子事实。默认关闭从 assistant 回复自动沉淀事实，减少模型输出污染长期记忆。

这仍是有限槽位与 LLM 抽取的组合。开放语义的同义词、时间范围、多人关系、第三方引用仍需要带标注的评测。事务 outbox 有版本载荷，但不是永久保留全部事实历史的审计系统。规则即时偏好和异步长期记忆之间也尚未形成跨仓储的一个事务。

**RAG。** 所有检索结果在生成前经过模式对应的证据门槛：LLM/API rerank、cross-encoder、local overlap 与无重排回退分别处理，不再把很小的 RRF 分数套到重排阈值，也不允许无重排路径跳过证据检查。单条候选同样重排。生成模型默认输出事实及引用的结构化 JSON；校验 E 编号与原文连续片段，引用无效就拒答。结果携带文档 ID、版本 ID、章节及可回答性信息。

**这些门槛尚未完成生产数据校准。** `calibrated_modes` 默认空；本地词面重叠只是回退启发式。原文引用存在只能验证来源，不能证明每个结论被原文语义蕴含。下一步应分别测召回率、拒答准确率、引用正确率与语义支持率，不能只看答案有没有 `[E1]`。

**图谱。** 实体通过 `MENTIONED_IN` 连接文档块，避免同名实体只保留最后一个文档来源。删除文档先移除对应块，再清理孤立实体。旧图节点剩余的来源会做幂等回填；历史上已经被覆盖的其他来源只能从原文重新抽取。本机没有完成真实 Neo4j 集成验证。

**真实评测入口。** `scripts/run_live_optimization_eval.py` 禁用 mock，做只读依赖探测、真实 embedding 和带固定证据的真实生成检查。没有写生产语料，没有把回放结果算成模型成绩。结果在 `live-optimization-smoke.json`：真实 embedding 为 1024 维且匹配配置；真实生成给出“30 秒”并引用原文。PostgreSQL 连接失败，Elasticsearch HTTP 请求失败，当前环境缺 Neo4j、Milvus 驱动。因此完整真实检索质量、负载、故障恢复评测仍未完成。

### P2：资源控制与职责拆分

- 会话运行时默认最多 32 个，空闲 30 分钟过期；容量满时淘汰空闲项，执行中的会话不可淘汰，重建时恢复该会话历史。
- 用户 Agent 注册表设置 128 个容量上限。目前满载会拒绝新增，**尚未实现跨用户 Agent 的安全空闲淘汰**；这需要把 HTTP、评测与后台工作都纳入资源租约，避免关掉仍被使用的共享 writer。
- 记忆队列默认最多 128 个待处理任务，满载拒绝新任务；停止时排空已接受任务。SSE 队列限长 512，消费慢时施加背压。
- 主 Agent 每轮默认最多 24 次聊天模型调用、32 次工具派发；重试也消耗预算。通过上下文传播让图线程和检索/上下文并发工作共享计数。该预算不是全局 token/费用账单，也不覆盖所有后台索引与记忆任务。
- 新拆出 `conversations.py`、`tool_execution.py`、`harness/execution.py`、`harness/journal.py`、`memory/facts.py`、`rag/evidence.py`、`resilience/budget.py`。主 `agent.py`、HTTP handler 仍然偏大，完整拆分没有在本轮一次做完。

## 配置与迁移

应用数据库新增聊天会话字段与 `agent_action_journal`。应用既有 Alembic 生命周期会升级；独立运维可在确认数据库指向后执行 `python -m alembic upgrade head`。传统 PostgreSQL bootstrap 也添加会话列与索引。迁移前已有历史归属于空会话，不猜测归属到某个新聊天。

可选配置：

```yaml
rag:
  require_citations: true
  fallback_min_overlap: 0.08
  cross_encoder_threshold: 0.5
  calibrated_modes: []
harness:
  max_llm_calls_per_turn: 24
  max_tool_calls_per_turn: 32
```

没有证据的答案会比以前更容易拒答。这是可见的行为变化；不要为了让演示“总能回答”而直接关闭门槛。用领域样本校准后再调参数。

复跑只读真实检查：`python scripts/run_live_optimization_eval.py`。失败结果仍写报告，并以非零码退出。全量分批结果为 **696 passed、1 skipped、0 failed**；之后 HTTP/配置/应用回归 23 项、记忆回归 34 项、包含新增用例的安全边界测试 10 项也通过。分批记录见 `optimization-test-summary.json`，汇总见 `optimization-validation.json`；前端 `npm.cmd run build` 通过。单测与构建不等于浏览器交互验收或线上可靠性认证。

## 仍然需要优先处理

先恢复真实检索基础设施并重建旧图来源，然后用实际业务语料分别校准各检索模式。之后补多步任务持久化恢复、跨用户资源租约与淘汰、跨记忆仓储的一致写入以及引用语义验证。独立 Harness 对缺少完整图状态的旧 DAG 恢复明确拒绝。

2026-09-19 第二轮进展：独立 Harness 已新增完整图 checkpoint、安全续跑与持久化派发领取，主 Agent 图任务恢复仍待完成；PG 持久化与 Neo4j 来源结构已做独立真实验证，本地 RAG 已完成 50 题真实模型复测。环境故障与剩余限制详见 [第二轮记录](optimization-followup-20260919.md)，不要把第一轮的依赖探测结果当成当前所有能力的最终结论。
