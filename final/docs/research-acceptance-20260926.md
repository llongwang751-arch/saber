# AGI-Saber Research 改造与验收记录

日期：2026-09-26。改造目录：`AGI-saber-python/final`。保留进入任务前已有的未提交重构，未提交、发布、部署或修改既有数据库迁移文件。

## 已交付

- 在既有原生运行账本上加入研究模式、合法 DAG 计划、持久化审批等待、版本 CAS 编辑/批准/拒绝、取消、事件重放和关联续跑。
- 新 `internal/research` 包实现搜索/阅读/缺口分析循环，支持真实 Tavily 与本地文档原文；来源登记、去重、原文引用检查、带编号报告及 Markdown/Python 产物。
- 研究执行预算涵盖调用次数、模型输出 token、来源、上下文和执行耗时，检查点保留预算使用量。代码只交给隔离 Docker，未知执行结果不自动重放。
- Vue 研究工作台覆盖计划编辑与审核、过程观察、引用跳转、报告下载、刷新恢复、移动布局；继续保留普通聊天、知识库、记忆和评测。
- 简化配置示例、声明式工具/MCP 注册、独立研究 Compose、离线示例、HTTP 演示脚本和真实模型验收脚本。医疗、农场、在线实验默认关闭，旧实现保留。

## 验证结果

| 检查 | 结果 |
|---|---|
| 改造前全量测试 | 790 passed，2 skipped |
| 改造后全量回归 | 839 passed，2 skipped；271.76 秒 |
| 后续预算/租约/配置等最终关键回归 | 77 passed |
| 最后提示范围调整后的研究/引用/计划测试 | 47 passed；与上一行有重叠，不相加 |
| Ruff | `python -m ruff check .` 通过 |
| 前端 | 9 项 Node 测试通过；Vite 生产构建通过 |
| 确定性离线示例 | 2 轮研究、2 个来源、连续引用编号、报告产物 |
| Edge 交互验收 | 13 类交互检查通过，包含版本冲突、取消、拒绝、恢复、375px 布局；此轮使用模拟 API |
| 真实 LLM + 本地知识库 | 真实模型生成计划 → HTTP 批准 → 检索上传资料 → 完整引用报告；最后一次无研究限制 |
| Edge + 真实后端 | 无 API 模拟；真实报告/来源显示、引用跳转、重载一致、下载逐字匹配持久化报告、375px 无横向溢出；无 JS 错误或失败请求 |

最后一次真实模型验收位于本机 `runtime/research-acceptance/68ecf5dd4395/`，运行 ID 为 `1b17cf25-4b25-4571-9f7b-78e53f4737ba`。执行阶段 5 次模型调用、3 次检索调用、1 轮研究、1 个去重文档来源；服务报告输出 token 7079（可能包括推理 token）。这些数字只描述该小型验收资料，不是通用研究质量或性能基准。

浏览器证据位于本机 `tmp/research-ui/real-backend-results.json`，对应上述最新完整成功的真实模型报告。下载报告的 SHA256 为 `5547adcc02d768c7e8e10a57eec4521dd5cb60587bc39d503267fd594b3b61ec`。检查期间没有新增运行、模型调用或写请求。测试启动的临时后端与 Vite 已关闭。

## 已知运行边界

- Tavily 已在后续凭证联调中配置，搜索及正文提取实测可用；指定网页仍可能提取失败，此时保留摘录并明确报告证据缺口。Docker 在后续启动修复中已完成真实验收，见下节。结构化搜索、失败降级、禁用工具、代码隔离与不确定执行恢复均有离线测试。
- 模型输出会变化。预算不足或证据缺口会返回 `partial`，报告明确显示限制；没有取得任何来源时返回失败。一次低输出预算验收触发了部分报告，已有证据仍保留，随后提高验收预算完成了完整报告。
- 引用校验验证来源编号、出处与逐字摘录，不保证每条推断都被来源充分支持。高风险结论仍需领域审核。
- 研究模式使用受限研究/代码/写作角色；它与原生聊天、工具审批、技能和 MCP 能力共存，不宣称与 DeerFlow 所有功能完全一致。

## 使用入口

### 本机启动与 Docker 后续验收（同日）

`python scripts/run_research_local.py` 已启动修复版，入口 `http://127.0.0.1:8090`，首页、`/healthz`、`/readyz` 均正常。首次使用注册账号，数据持久化于独立的 `runtime/research-local/`；前文的完整模型报告属于先前验收记录。

Docker Desktop 因两处损坏的运行时 socket 启动失败，已备份隔离 `Docker/run` 与 `docker-secrets-engine` 临时目录后恢复，没有重置工厂配置或删除镜像/卷。Engine 29.7.2 已运行，`python:3.11-slim` 可用。沙箱改为命名容器创建/启动、取消轮询、强制清理及有界输出缓冲。31 项相关测试通过；真实容器完成计算、断网、只读根目录、256 MB/64 PID 限制、失败、超时、取消验证，测试后无遗留容器。证据位于 `runtime/docker-live-20260926.json`。宿主进程突然死亡等场景不能据此保证零遗留。

首次启动验证曾在报告写作时收到旧模型服务 HTTP 402，结果为 `partial` 且保留来源。随后已按用户授权切换 DeepSeek；该旧故障不再代表当前服务状态，最新结果见下节。

### DeepSeek 与 Tavily 真实联调（同日）

两仓私有 `.env` 已配置凭证，未写入源码或本验收记录。DeepSeek `/models`
及最小聊天请求均返回 HTTP 200；选用实际可用的 `deepseek-flash`。Tavily Search
返回 HTTP 200 与真实结果，Extract 成功读取 SQLite 官方 WAL 文档。

真实 Edge 验证注册、上传合成差旅资料、模型规划、批准、检索和报告：运行
`bbda4c6e-1505-48db-84d0-a1d4e61b3d40` 完整完成，1 个引用、无研究限制和 JS 错误。
执行阶段 4 次模型调用、3 次工具调用、输出 1067 tokens；审批前规划另计。
证据位于 `runtime/research-local/browser-acceptance.json` 和 `browser-report.md`。

首次联网任务针对 Tavily 文档页面；该页面的 Extract 返回抓取失败，研究结果如实为
`partial`，见 `web-acceptance.json`。另一次 SQLite 查询暴露模型返回无效 JSON 的问题，
见 `before-json-fix-sqlite-web-acceptance.json`。已对官方 DeepSeek 的结构化请求启用
`response_format={"type":"json_object"}`，并明确 JSON 提示；不静默补写模型输出。
新增 transport 回归结合原研究/引用测试共 34 项通过，Ruff 通过。

修复后再次通过真实 Edge 发起联网研究，运行
`47f9c57a-01ac-41e1-926e-135f2259d60d` 完整完成。取得 10 个去重来源（包含 SQLite
官方 WAL 全文，也包含其他站点），报告列出来源，研究限制为空、无 JS 错误，桌面及
375px 布局检查通过。执行阶段 4 次模型调用、6 次工具调用、输出 3098 tokens，
约 39.9 秒；这些数值是本次样例，不能包装成通用性能指标。
证据与报告为 `runtime/research-local/sqlite-web-acceptance.json`、
`sqlite-web-report.md`。用户服务已重启并保持运行，`/readyz` 返回 HTTP 200。

用于手动上传的合成资料见 [差旅政策演示](../examples/research/enterprise-policy-demo.md)。

- [快速开始](research-quickstart.md)
- [架构与状态机](research-architecture.md)
- [确定性离线示例](../examples/research/README.md)
- `python scripts/demo_research.py --help`
- `python scripts/smoke_research.py --live`（会调用已配置的真实模型并产生额度消耗）
