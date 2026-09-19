# AGI-saber Go → Python 完全复刻对照与面试说明

> **冻结基线（2026-09-14）**：本文以 Go `845e8f7` 为唯一对照基线。该基线包含 `rag_agent / rag / react` 三路路由和 `research → writer → review → doc` 固定报告 DAG；Python 已按该行为恢复，不再把它误标为历史设计。

## 1. “完全复刻”在本项目中的定义

Python 版以 Go 版为行为基准，要求相同输入得到相同的模式选择、任务依赖、工具边界、事件顺序、持久化语义和 HTTP 契约。它不是逐行翻译：Go 的 goroutine、channel、`context.Context`、Chi 和 `database/sql`，在 Python 中分别使用守护线程/信号量、事件队列、`CancelToken`、FastAPI 和 SQLAlchemy/psycopg2 实现。

因此面试时应说“行为级、协议级和失败语义级复刻”，不要说“两边是同一份代码”。Python 版还保留了质量评测、Badcase 闭环、智慧养殖等扩展；这些属于超集，不改变 Go 基准链路。

## 2. 已对齐能力矩阵

| 能力 | Go 基准 | Python 对应实现 | 等价点 |
|---|---|---|---|
| 请求路由 | `application/chat/core_agent.go` | `agent/agent.py`、`agent/router.py` | `use_rag`、RAG 是否已加载、报告意图共同决定 `react/rag/rag_agent` |
| 普通 ReAct | `mode_react.go` | `agent/agent.py` | 普通问答不隐式启用子智能体 |
| 报告流水线 | `subagents.go` | `agent/subagents.py` | 固定为研究→写作→评审→文档，依赖关系一致 |
| 图规划与执行 | `plan_graph.go`、`runtime_graph.go` | `agent/planner.py`、`agent/graph_runtime.py` | 并发就绪节点、超时、失败、重规划、动态加节点一致 |
| 图数据结构 | `domain/graph/graph.go` | `graph/task_graph.py` | 节点状态、终态依赖、邻接表和入度一致 |
| SSE/Trace | `runtime_process.go` | `agent/graph_runtime.py`、`handler/handler.py` | 每节点输出 Thought→Action→Observation，并有 node_start/node_done |
| 工具边界 | `tool_registry.go` | `tools/tools.py`、`agent/graph_runtime.py` | 默认只注册 `search_web`；MCP 真实 HTTP POST，并保留结构化成功/失败、错误码、可重试标记和尝试次数 |
| RAG | Go Chat/RAG 组合 | `rag/rag.py`、`rag/hybrid.py` | 最近 6 条历史、多查询改写、快速模型重排、主模型合成；一级 RRF 中 Milvus/ES 均为 `1.0`，图谱使用 `kg_weight` |
| 上下文装配 | `ctx_builder.go`、`ctx_prompt.go` | `promptctx/*`、`agent/agent.py` | 用户、任务、偏好、记忆、工具状态和约束统一装配 |
| 快慢模型 | Go fast/main model | `llm/llm.py` | 规划、改写、评审走 fast；最终回答走 main，未配置则回退 |
| 取消与恢复 | `infra_cancel.go`、`mem_restore.go` | `agent/cancel.py`、`agent/restore.py` | 多请求取消、快照恢复、当前任务保留一致 |
| 产物落盘 | `artifact.go` | `agent/artifact.py` | 每用户/任务隔离工作区，沙箱复制失败时本地降级 |
| 长期记忆事务 | `memorytx/repository.go` | `application/local_repos.py`、`repo/longterm.py` | 权威行与目标 Outbox 同事务提交，更新递增版本 |
| 投影幂等 | `projection/versioned` | `memory/consistency.py` | 忽略旧版本、重复事件幂等、同版本不同哈希报冲突 |
| 删除与重试 | `memoryprojection/worker.go` | `application/local_repos.py`、`repo/memory_projection.py` | 删除墓碑、指数退避、达到上限后 dead；生产 PostgreSQL 使用 `SKIP LOCKED` 租约与过期回收 |
| 鉴权 | Go auth middleware | `application/auth.py`、`application/api.py` | bcrypt、HS256 JWT、用户隔离、启动拒绝短密钥 |
| 诊断保护 | `handler/debug.go` | `handler/handler.py` | 仅显式启用并提供 `X-Admin-Token` 时开放；错误令牌返回 404 |
| 配置 | `config/config.go` | `config/config.py`、`config/config.yaml` | fast model、graph replan、artifact、auth、pprof、SkillHub 字段一致 |

## 3. 路由规则必须背熟

| 条件 | 模式 | 行为 |
|---|---|---|
| `use_rag=true`，知识库已加载，且用户要研究/总结/报告/文档/方案/分析 | `rag_agent` | 强制执行四段子智能体报告链路 |
| `use_rag=true`，知识库已加载，普通问题 | `rag` | 基于本地证据直接回答 |
| 其余情况 | `react` | 普通工具/推理链路，不启用子智能体 |

这也解释了为什么早期页面显示“执行成功但 Trace 是空对象”：旧 Python 演示数据只把用例标成通过，没有真正保存完整节点事件。现在运行时会保存并展示真实的 Thought、Action、Observation 和文档结果。

四个报告节点的依赖固定为 `research → writer → review → doc`。这条冻结基线没有 Writer 二次修订或 Final Gate：Review 的意见作为上游结果和文档元数据保留，Doc 保存的仍是 Writer 草稿。面试时可以把“让 Review 真正拦截或触发修订”列为优化项，不能说成当前已实现。

RAG 权重也要按源码讲：Milvus 语义路与 Elasticsearch 关键词路在 RRF 中各乘 `1.0`，知识图谱路乘 `kg_weight`。配置项 `semantic_weight=0.7` 只为 Go/Python 配置结构兼容而保留，当前不参与 RRF 计分；不能讲成 `0.7/0.3` 或 `0.65/0.35`。

## 4. 记忆一致性的面试讲法

推荐回答：

> PostgreSQL 或本地 SQLite 是长期记忆的权威源，投影可重建。本地 SQLite 在同一事务写权威行和 Outbox，由本地 Worker 更新投影账本；生产 PostgreSQL 在同一事务写 `long_term_memory` 与按 Milvus/Neo4j target 拆分的 Outbox，提交成功后才更新进程缓存。每条记忆有单调递增 version 和跨 Go/Python 一致的 content hash。生产消费者用 `FOR UPDATE SKIP LOCKED` 租约领取，忽略旧版本；同版本同哈希幂等，同版本不同哈希报冲突；失败指数退避，达到上限进入 dead。删除使用新版本 tombstone；启动时立即、之后周期 reconcile，把缺失、旧版、哈希不一致和孤儿投影重新写入 repair outbox。

Python 测试中还保存了一个由 Go `encoding/json + SHA-256` 实际计算出的 golden hash，防止两种语言因为浮点和 HTML 转义规则不同而产生静默分叉。

## 5. 为什么两边实现看起来不同

- Go 用 goroutine 和 channel 调度；Python 用守护线程、信号量和线程安全队列。共同约束是“仅执行依赖已终结的就绪节点、限制最大并行度、可取消、可超时”。
- Go 用 `context.Context` 传播取消；Python 用 `CancelToken`，并在模型、工具、图执行和 SSE 层检查。
- Go 用 Chi；Python 用 FastAPI。对外请求字段、状态响应、SSE 事件和鉴权边界保持一致。
- Go 生产存储使用 PostgreSQL；Python 同时提供 PostgreSQL 和 SQLite 离线实现。两者采用相同的版本、哈希、墓碑和 Outbox 合同，但 SQLite 只更新本地投影账本，不能冒充真实 Milvus/Neo4j 生产消费。
- Go 的 pprof 在 Python 中对应线程、堆和运行时诊断信息；端点保护策略一致，采样内容受语言运行时影响。

MCP 失败语义不是“异常就全部重试”。Python 优先消费结构化 `ToolResult`：参数错误、HTTP 4xx 和主动取消不可重试；网络错误、超时和 HTTP 5xx 才可按 Harness 上限重试。每次尝试都会保留错误码、耗时与重试标记；取消后调度器停止等待。需要如实说明：底层同步 Python 调用已经在线程中运行时，无法保证把该线程硬杀掉，只能停止等待并丢弃迟到结果。

## 6. 现场验证命令

推荐在 Python 项目目录执行一键一致性脚本：

```powershell
python scripts/run_go_python_conformance.py
```

脚本默认检查相邻 Go 仓库是否处于 `845e8f7`，核对两边关键实现文件哈希，执行 Go 全包测试和 Python 对照契约，并写出：

- `runtime/go-python-conformance/conformance-results.json`
- `runtime/go-python-conformance/conformance-report.md`

Go 仓库不在默认位置时使用 `--go-root <目录>`；只复核已有 Go 测试证据时可加 `--skip-go-tests`，但报告中必须明确这是跳过项。输出目录可用 `--output-dir <目录>` 修改。

发布前还应在同一提交分别运行全量测试与前端构建：

```powershell
python -m pytest tests -q
Push-Location web
npm run build
Pop-Location
```

证据分三层：在目标提交实际运行一致性脚本和测试后，保存的原始输出属于“已运行的工程契约”；演示用 12 条数据的 `0/12 → 12/12` 属于确定性 Replay 基准，只证明评测器与回归门禁；真实 PostgreSQL、Milvus、Elasticsearch、Neo4j、Kafka、MCP 服务和 Docker 沙箱仍需在完整环境做集成、故障注入与容量验证。静态文档不固化会随用例增长而过期的通过数。

## 7. 面试中不要说错的三件事

1. 不要说“Python 是 Go 的逐行翻译”。应说外部行为、协议和故障语义一致，运行时机制采用语言原生实现。
2. 不要把 Python 独有的评测平台、智慧养殖和 SQLite 离线能力说成 Go 原版已有；它们是复刻完成后的扩展。
3. 不要只展示 100% 通过率。应点开 Trace，解释路由选择、节点依赖、工具参数、Observation、重试/降级和最终文档落盘。

## 8. 建议现场演示

先上传一份文档，再勾选知识库模式，输入：

```text
根据我上传的文档分析主要结论，生成 Markdown 报告并保存到本地文档库。
```

应观察到 `rag_agent`，随后依次出现研究、写作、评审和文档节点；文档节点的 Observation 中包含创建后的文档对象，而不是空的 `{}`。再输入一个普通问题，证明它走 `rag` 而不会滥用子智能体。最后展示记忆一致性测试和 Go/Python golden hash，说明复刻覆盖了正常路径与失败路径。
