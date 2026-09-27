# AGI-Saber Research · 后端开发指南

深度研究框架的服务端：FastAPI + LangGraph 编排（chat / research 双 StateGraph）+ 自研持久化运行时 + React 18 前端。项目定位与特性总览见 [根 README](../README.md)，从零跑通一次研究见 [研究快速开始](docs/research-quickstart.md)。本页面向**开发与调试**。

## 目录速查

| 路径 | 职责 |
|---|---|
| `main.py` / `internal/application/bootstrap.py` | 启动入口与组合根（配置 → 基础设施 → UnifiedAgent → 路由装配） |
| `internal/chat_graph/` | LangGraph 聊天编排：policy → prepare/路由 → react / research(rag_agent) / rag / chat → finalize（`chat.engine` 可切 native 回退） |
| `internal/agent/` | 运行时核心：`planner` 计划、`planning_service` ReAct/子代理执行、`run_scheduler` 持久化调度、`recovery` 恢复重放 |
| `internal/research/` | 研究引擎：迭代检索循环、来源账本、引用报告、沙箱 coder（被 research_graph 节点复用） |
| `internal/research_graph/` | LangGraph 研究引擎：plan → interrupt 审批 → Send 并行研究 → coder → report（`research.engine` 可切 native） |
| `internal/handler/` | HTTP 边界：`chat_routes`（SSE 推流）、`run_routes`（断线重放）、文档/工具路由 |
| `internal/rag/` | 三路混合检索（Milvus/ES/Neo4j RRF）与降级档 |
| `internal/tools/` | 工具执行器、MCP 客户端（Streamable HTTP） |
| `internal/harness/` | 危险工具审批、安全护栏、动作日志 |
| `internal/infra/` | PG/ES/Milvus/Neo4j/Kafka 生命周期，逐依赖熔断降级 |
| `internal/evaluation/` | 内置评测平台（独立 SQLite 存储，离线可跑） |
| `web/` | React 18 + Vite + TypeScript + Zustand 前端（研究工作台、PlanReview、报告阅读） |
| `runtime/` | 本地运行产物（运行台账、报告、评测数据），gitignore，不入库 |

## 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 3.11（推荐） | 使用 `StrEnum`，3.10 不兼容 |
| Node.js | ≥ 20 | 仅前端构建/测试需要 |
| Docker | 可选 | 沙箱代码执行（`python:3.11-slim`）与全套基础设施（`docker-compose.yml`） |
| Tavily key | 可选 | 联网搜索；只研究本地文档时不需要 |

macOS / Linux 均已验证；Windows 推荐 WSL2 或 PowerShell（`scripts/run_research_local.py` 已验证）。

## 启动

三种方式（一键本地 / 标准部署 / 容器）见 [研究快速开始](docs/research-quickstart.md)。开发调试最常用的两条：

```bash
# 复用已有配置一键启动（独立 runtime/research-local/ 存储，自动生成 JWT 密钥）
python scripts/run_research_local.py

# 手动：配置 → 构建前端 → ASGI 工厂启动
cp config/conf.example.yaml config/conf.yaml
AGI_CONFIG=config/conf.yaml python -m uvicorn internal.application.bootstrap:create_app --factory --port 8090
```

服务地址 `http://127.0.0.1:8090`（端口可用 `AGI_SERVER_PORT` 覆盖）；Swagger 在 `/docs`；健康检查 `/healthz`、依赖就绪 `/readyz`。

## 开发与验证

```bash
python -m pytest -q                 # 全量测试（离线确定性，不消耗模型额度）
python -m ruff check .              # 正确性门禁（CI 同款规则）
npm --prefix web test               # 前端 node 测试
npm --prefix web run build          # 前端生产构建（构建产物 web/dist 由后端静态挂载在 /）
python examples/research/offline_demo.py   # 无模型验证研究全流程
```

改动约定：

- 提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/)：`feat(xxx): ...` / `fix(xxx): ...`
- 不修改 `alembic/versions/` 已有迁移，schema 变更只追加新迁移
- SSE 既有事件名与顺序是前后端契约，新增事件只允许追加
- 运行台账的租约/围栏/恢复语义有专门测试覆盖，改动前先跑 `tests/test_native_run_service.py` 与恢复相关用例
- 新增外部能力（搜索、抓取）必须提供 mock，测试离线可跑；API Key / 私钥绝不提交

## 配置

优先级：`AGI_CONFIG` 显式路径 > `config.local.yaml` > `conf.yaml`（精简档）> `config.yaml`（全套增强档）；环境变量（`AGI_*`）覆盖 YAML。密钥只放本地 `.env` / `config.local.yaml`，两者均已 gitignore。

| 场景 | 配置 |
|---|---|
| 仅 SQLite、零外部依赖（默认） | `conf.yaml`（复制 `conf.example.yaml`） |
| PG/Milvus/ES/Neo4j/Kafka 全套 | `config.yaml` + `docker-compose up -d` |
| 研究预算/轮次/工具声明 | `conf.yaml` 的 `research:` 与 `tools.manifest` |

常用环境变量：`AGI_JWT_SECRET`（生产必填，≥32 字节随机值）、`AGI_LLM_API_URL/KEY/MODEL`、`AGI_LLM_FAST_MODEL`（规划/校验用快模型）、`TAVILY_API_KEY`（联网搜索）。模型未配置时默认允许 Mock 回复（开发 profile）；已配置真实模型后调用失败默认报错，只有显式 `AGI_LLM_ALLOW_MOCK=1` 才回退 Mock。

## 常见问题

- **基础设施全部 `disconnected`**：预期行为。每个依赖独立熔断降级，本地 SQLite 主链路完整可用；要接通就 `docker-compose up -d`。
- **`marshmallow has no attribute __version_info__`**：依赖冲突，requirements.txt 已 pin `marshmallow<4`，重新 `pip install -r requirements.txt`。
- **端口 8090 被占用**：`AGI_SERVER_PORT=8091` 或释放端口。
- **新克隆后首页 404 / 无前端**：前端产物不入库，先 `npm --prefix web ci && npm --prefix web run build`。

## 数据位置

默认在 `final/runtime/`（gitignore）：`research-local/`（一键启动的应用数据与台账）、`evaluation.db`（评测平台）、`reports/`（评测报告）、`research-acceptance/`（真实模型验收证据）。迁移数据先停服务再整体拷贝，不要在运行中删除 SQLite 文件。

## 文档

- [研究快速开始](docs/research-quickstart.md) · [研究运行时架构](docs/research-architecture.md) · [改造验收记录](docs/research-acceptance-20260926.md)
- [原生 Agent 运行时重构](docs/原生Agent运行时重构.md) · [系统架构与核心流程图](docs/系统架构与核心流程图.md)
- [RAG 可靠性与故障恢复运行手册](docs/RAG可靠性与故障恢复运行手册.md) · [Agent 质量评测平台使用指南](docs/Agent质量评测平台使用指南.md)
