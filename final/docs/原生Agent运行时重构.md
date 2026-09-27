# AGI-saber 原生 Agent 运行时重构

本次是 **Saber 自身的分层与运行时重构**，不是把 DeerFlow 作为外部服务接进来。借鉴的是工程组织原则：职责分层、显式依赖、任务与会话分离、运行状态持久化、事件可追溯、取消可观察。Saber 原有的记忆、RAG、工具、图检查点、评测和业务功能保持兼容，并非将这些实现全部替换。

## 模块边界

| 层 | 模块 | 职责 |
|---|---|---|
| 启动装配 | `internal/application/bootstrap.py` | CLI/ASGI 共用装配入口、依赖注入、部分启动失败清理、幂等关闭 |
| 运行提供者 | `internal/application/run_runtime.py` | 应用级运行服务、配置校验、数据库定位和测试隔离 |
| Agent 门面 | `internal/agent/agent.py` | 初始化、兼容入口、委派；旧导入和方法签名继续可用 |
| 数据契约 | `internal/agent/contracts.py`、`serialization.py` | 请求/响应/步骤类型与转换 |
| 回合编排 | `internal/agent/turn_service.py` | 请求准备、路由、派发、取消传播 |
| 规划执行 | `internal/agent/planning_service.py` | 图规划、GraphRuntime、回答合成、可注入规划器/产物生成器 |
| 上下文 | `internal/agent/context_service.py` | 提示组装、历史、压缩、记忆前缀 |
| 记忆 | `internal/agent/memory_service.py` | 回合收尾、历史/轨迹/快照保存、工具观测 |
| 检索 | `internal/agent/retrieval_service.py` | RAG 查询、入库、请求级覆盖 |
| 文档/工具装配 | `internal/agent/document_service.py` | 文档操作、文档工具、MCP 注册 |
| 调度 | `internal/agent/run_scheduler.py` | 有界后台工作线程、前台观察、取消、工作者租约 |
| 持久化 | `internal/agent/run_repository.py` | SQLite 事务、迁移、幂等、会话互斥、事件和状态原子写入 |
| HTTP 装配 | `internal/handler/handler.py` | 中间件、路由组合、静态资源 |
| HTTP 功能组 | `chat_routes.py`、`document_routes.py`、`tool_routes.py`、`runtime_routes.py`、`run_routes.py` | 按业务分组的传输边界 |
| HTTP 契约 | `models.py`、`http_contracts.py`、`chat_experiments.py` | 参数模型、公开响应、实验曝光收尾 |
| 前端流协议 | `web/src/composables/useSSE.js` | 聊天与运行面板共用增量解析器 |

Agent 服务是接收当前会话 runtime 的无状态函数，不持有用户/会话实例，保留 ConversationPool 的浅复制隔离语义。规划器与记忆更新函数通过门面显式传入，兼容原有替身测试。运行仓储不导入 Agent 或 HTTP；调度器不直接写 SQL。`run_service.py` 和 `run_api.py` 仅保留旧导入路径的兼容导出。

## 运行链路

`POST /api/agent-runs` → 鉴权与归属校验 → SQLite `agent_runs` 排队记录 → 有界线程池 → `UnifiedAgent.process_stream` → SQLite `agent_run_events` → SSE/列表/详情。图检查点恢复通过 `POST /api/agent-runs/recover` 进入同一运行账本，复用原有 `resume_task` 的租约和副作用保护。

即时聊天 `POST /api/chat`、流式聊天 `POST /api/chat/stream`、后台任务和图恢复共用同一运行账本、容量限制与会话互斥。聊天响应头含 `X-Saber-Run-ID`，流式 `start` 事件还含 `run_id`。种类分别为 `chat_sync`、`chat_stream`、`chat`、`recovery`。前台聊天仍在原请求执行路径运行，后台任务才进入有界执行器。

前端 `web/src/components/RunWorkbench.vue` 只请求 Saber 同源 API，统一查看上述记录。账本默认写入 `runtime/agent_runs.sqlite3`；会话正文、长期记忆和图检查点保留原有仓储。设置 `AGI_RUN_DB_PATH` 可指定账本；显式 SQLite `AGI_EVAL_DATABASE_URL` 则派生同目录的独立运行库，避免测试或隔离实例污染默认账本。

| 状态 | 含义 |
|---|---|
| `pending` | 已持久化，等待工作线程 |
| `running` | Agent 已开始执行 |
| `cancelling` | 已发取消令牌，等待 Agent 到取消点 |
| `completed` / `failed` / `cancelled` | 终态，结果与 `done` 事件同事务提交 |
| `interrupted` | Agent 主动中断，或进程重启时遗留的未结束任务 |

同一用户、同一会话只允许一个活动运行；全局活动任务最多 64 个，超过返回 429。`Idempotency-Key` 可防止网络重试重复创建运行，同一键对应不同请求返回 409。事件有递增 ID，可用 `after` 或 SSE `Last-Event-ID` 补读；前端按 500 条分页回放历史，从最后一个已收到的 ID 继续实时流，并只保留最近的非 token 事件用于列表展示。用户只能查看和取消自己的运行。关闭页面不会停止后台任务。

每个 Saber 进程拥有本地线程池并向 SQLite 写入工作者心跳。多个进程可共享运行账本：新进程不会中断仍有心跳的旧进程任务；跨进程取消通过账本传递；心跳过期的运行才标记为 `interrupted`。这些任务不会自动重放，因为工具可能已经发生外部副作用；恢复前需检查 action journal/图检查点和目标环境。取消是协作式的，长时间不检查取消令牌的工具不能被强制终止。此实现仍是**单机共享 SQLite**，不等同于跨主机分布式任务队列。

## API

所有路由都需 Saber 登录令牌。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/agent-runs` | 创建运行；JSON：`message`、可选 `conversation_id`、`use_rag` |
| POST | `/api/agent-runs/recover` | 恢复图检查点；JSON：`task_id`、`conversation_id` |
| GET | `/api/agent-runs?limit=50` | 最近运行 |
| GET | `/api/agent-runs/summary` | 当前用户各状态数量、全局活跃量和健康工作者数 |
| GET | `/api/agent-runs/{run_id}` | 状态与最终结果 |
| GET | `/api/agent-runs/{run_id}/events?after=0` | 持久化事件补读 |
| GET | `/api/agent-runs/{run_id}/stream?after=0` | SSE 实时事件，支持 `Last-Event-ID` |
| POST | `/api/agent-runs/{run_id}/cancel` | 请求停止 |

`conversation_id` 留空即新建会话；传入已完成运行的 `conversation_id` 可继续同一 Saber 会话。两个 POST 均支持 `Idempotency-Key` 请求头。旧 `/api/tasks/{task_id}/resume` 保持兼容，前端恢复按钮已切换到新的持久化运行入口。普通即时聊天 `/api/chat/stream` 仍保留断连即取消的语义，不应与可脱离页面执行的后台运行混为一谈。

环境变量 `AGI_RUN_MAX_WORKERS`（默认 4，上限 16）与 `AGI_RUN_MAX_ACTIVE`（默认 64，上限 1024）控制单进程后台线程数和共享账本活动运行上限。旧库迁移在写事务中串行完成，并对并发开启 WAL 的锁冲突做有界重试；已有记录保留。`GET /api/agent-runs` 只返回列表元数据，完整结果由详情接口读取。

终态和唯一 `done` 事件同事务写入，写入失败全部回滚；终态后忽略新事件。工作者租约失效后停止接收新工作并取消本地令牌，避免过期工作者继续取得新任务。前端解析支持 CR/LF/CRLF、中文 UTF-8 分包、多行 data、事件游标及结束时尾片段；切换记录时丢弃旧请求返回值。

当前边界：单机 SQLite，而非跨主机调度；取消为协作式；运行事件暂未自动清理，应按部署容量规划备份和归档。旧业务实现仍有独立优化空间，这次验收不代表 DeerFlow 功能逐项等价，也不包含尚未配置的外部基础设施可用性保证。

## 验证

```powershell
cd final
python -m pytest -q --basetemp D:\tx\memory\.pytest-saber-acceptance
cd web
npm test
npm run build
```

专项测试在 `tests/test_native_run_service.py`、`tests/test_refactor_runtime.py`、`tests/test_sse_disconnect.py`：覆盖前后台统一账本、用户隔离、会话冲突、容量、跨工作者取消、并发迁移、终态事务回滚、过期租约、检查点恢复、40 任务排队、启动失败清理和断连取消。前端协议测试在 `web/tests/sse.test.js`。完整回归还覆盖原有 Agent/RAG/记忆/文档/评测/业务 API。

CLI 仍用 `python main.py`；ASGI 工厂可用 `python -m uvicorn internal.application.bootstrap:create_app --factory --port 8090`。真实模型验收与回归替身测试分开执行，浏览器脚本见 `scripts/smoke_runtime_ui.py`，需可用模型配置和 Playwright/浏览器。

升级前用 SQLite 在线备份接口保存运行库及应用库，或停止服务后连同 WAL/SHM 一起备份；勿在运行时仅复制主数据库文件。回滚需停止新服务后恢复已备份代码/前端与匹配的数据副本，不清空用户会话。当前改动未执行 Git 提交。

## 本机验收记录（2026-09-24）

| 检查 | 实测结果 |
|---|---|
| Python 全量回归（独立运行库） | 779 passed，2 skipped，143.64 秒 |
| 跳过项 | 可选 chromadb 依赖缺失；Windows 符号链接权限相关测试 |
| 全项目 `python -m ruff check .` | 通过；同时清理三个旧评测脚本的未使用导入/局部变量 |
| `python -m compileall -q internal scripts main.py` | 通过 |
| 前端 `npm test` / `npm run build` | 4 项测试通过；Vite 生产构建通过 |
| Edge 桌面与 390×844 窄屏 | 模型执行、关面板继续运行、刷新回放、前后台统一列表、移动导航通过；页面脚本错误 0 |
| 后台真实模型运行 | `5068157d-bd43-4601-9336-8dc0e1860aaa`，completed |
| 前台真实模型运行 | `2049aacf-a3d5-401c-a0e1-f9659d981c9b`，chat_stream / completed，恰好一个 done |
| 变更检查 | `git diff --check` 通过；未暂存、未提交 |

验收服务在 `http://127.0.0.1:8092`，旧 8090 进程保持原样。核心文件从 `agent.py` 1531 行、`handler.py` 1380 行分别降为 585 行、239 行，职责移入表中模块而非删减业务功能。浏览器脚本使用独立的 `runtime_smoke_` 测试账号，账号与验收历史保留供复核。

本机 PostgreSQL、Elasticsearch、Kafka、Milvus、Neo4j 与 Docker 未全部就绪；验收使用实际配置的模型及项目现有本地降级路径，未宣称外部组件集成已通过。测试仍有来自 FastAPI/pytest-asyncio 的弃用告警，升级依赖时需跟进。
