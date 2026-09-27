# AGI-Saber Research 快速开始

这条链路对标 DeerFlow 的计划、人类审核、迭代研究和报告工作流，使用本项目的原生运行时和 Vue 3。它是 AGI-Saber 的研究模式，不是 ByteDance DeerFlow 的发行版。

## 本地启动

如果这台机器已有模型配置和构建后的前端，直接运行：

```powershell
python scripts/run_research_local.py
```

打开 `http://127.0.0.1:8090`，首次使用点击“注册”创建自己的账号。这个启动器复用原配置的模型和搜索凭证，将新账号、文档、任务保存在独立的 `runtime/research-local/`，自动生成持久 JWT 密钥，不启动外部数据库。它启用 `python:3.11-slim` Docker 沙箱；Docker 引擎不可用时仍可进行知识库研究，代码步骤会注明未执行。加 `--no-sandbox` 可显式关闭沙箱。

联网搜索需要独立的 Tavily API key：在 https://app.tavily.com 注册或登录后，从控制台复制，写入本地 `.env` 的 `TAVILY_API_KEY=...`，重启应用生效。它不是模型 API key；不联网、只研究已上传资料时无需配置。不要把真实密钥提交到 Git。

本机现已配置 DeepSeek 与 Tavily。DeepSeek 的 `AGI_LLM_API_URL` 为
`https://api.deepseek.com/chat/completions`，`AGI_LLM_MODEL` 和
`AGI_LLM_FAST_MODEL` 为本次账号实际返回并验证的 `deepseek-flash`；密钥仅保存在
Git 忽略的 `.env`。研究中的计划、查询、证据分析和提纲请求对官方 DeepSeek
启用 JSON Output，报告正文仍使用普通文本输出，避免把 Markdown 误当成 JSON。
其他兼容网关默认不假定支持 JSON Output；详见
[DeepSeek 官方说明](https://api-docs.deepseek.com/guides/json_mode/)。

在 `final/` 下使用 Python 3.11+。已有环境可跳过安装；前端需要 Node.js 20+。

```powershell
python -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config/conf.example.yaml config/conf.yaml
$env:AGI_CONFIG = (Resolve-Path config/conf.yaml).Path
$env:AGI_AUTH_REQUIRED = '0'
$env:AGI_LLM_API_URL = 'https://your-provider.example/v1/chat/completions'
$env:AGI_LLM_API_KEY = 'your-key'
$env:AGI_LLM_MODEL = 'your-model-id'
$env:TAVILY_API_KEY = 'your-tavily-key'
npm --prefix web ci
npm --prefix web run build
python -m uvicorn internal.application.bootstrap:create_app --factory --host 127.0.0.1 --port 8090
```

这里的 URL 是待替换占位符。Linux/macOS 使用 `source .venv/bin/activate`、`cp` 和 `export NAME=value` 设置同样配置。开发模式仅在本机使用；共享部署启用 `AGI_AUTH_REQUIRED=1`，设置至少 32 字节的私有随机 `AGI_JWT_SECRET` 并登录。

打开 `http://127.0.0.1:8090`，从“研究工作台”输入目标。计划出现后可编辑目标、步骤、依赖与验收条件，保存修改，再批准执行。结果显示研究轮次、来源摘录和引用报告，关闭面板不会取消后台执行。

`config/conf.yaml` 不启动 PostgreSQL、Milvus、ES、Kafka 或 Neo4j。`rag.profile: local` 使用现有 SQLite 文本检索；要研究私有文档，先上传、入库，再勾选知识库。此时可以不配置 Tavily，但规划和写作仍需要真实模型。`lightweight` 使用现有 Chroma/BM25 路径，需要 `requirements-lightweight.txt`；完整基础设施仍使用旧 `config/config.yaml` 和 `docker-compose.yml`。

配置优先级：显式配置路径 / `AGI_CONFIG`，之后是 `config.local.yaml`、`conf.yaml`、`config.yaml`。环境变量覆盖 YAML。不要把真实密钥放入示例文件。

## 命令行 API 示例

```powershell
# 创建任务、打印计划后停止，交给工作台审核
python scripts/demo_research.py '比较 SQLite 与 PostgreSQL 的并发控制，给出官方来源'

# 明确授权脚本批准生成的计划并等待结果
python scripts/demo_research.py '比较 SQLite 与 PostgreSQL 的并发控制' --approve-plan

# 已从工作台批准后继续等待同一任务
python scripts/demo_research.py --run-id YOUR_RUN_ID
```

启用登录时，先把登录接口返回的 token 设置到 `AGI_AUTH_TOKEN`。报告默认保存在 `runtime/research-report.md`。脚本使用普通 HTTP API，流程可直接接入其他客户端。

## 容器

设置前述模型、搜索、JWT 环境变量后：

```sh
docker compose -f docker-compose.research.yml up --build -d
```

这个 Compose 只启动应用并用 volume 保存 SQLite 数据，不挂载 Docker socket。需要执行 Python 时，单独配置可用的 Docker 沙箱，预先准备 `python:3.11-slim` 镜像。无法提供隔离环境时，程序员步骤返回生成的代码和未执行说明；不会自动在宿主机运行代码。

## 可调整边界

`research` 配置控制轮次、LLM 调用、工具调用、来源数量、上下文、输出 token 和总时限。`features.medical/farm/experiments` 默认关闭；对应环境变量为 `AGI_ENABLE_MEDICAL/FARM/EXPERIMENTS`。旧业务实现和回归测试保留，评测平台仍可使用。

`tools.manifest` 指向声明式工具配置，相对路径以配置文件所在目录为基准。`builtins` 支持 `search_web`、`rag_search`、`exec_command` 开关；`mcp_servers` 在启动时走现有 MCP 握手/发现。开关不会替你配置密钥或提供沙箱。研究步骤的工具策略进一步限制该步骤可使用的能力。

## 验证

本轮改造结果与验证边界见 [验收记录](research-acceptance-20260926.md)。不配置模型也可以运行 `python examples/research/offline_demo.py`，它使用明确标记的确定性样例验证两轮研究与引用报告，不代表真实模型表现。

```powershell
python -m pytest -q --basetemp=tmp/pytest-research-check
python -m ruff check .
npm --prefix web test
npm --prefix web run build
```

测试注入确定性模型和检索结果，不消耗真实服务额度。离线测试通过不代表实际模型、搜索账号或 Docker 环境已经可用；真实服务故障在运行结果中报告，不转换成伪造的研究证据。

配置好真实模型后，可运行 `python scripts/smoke_research.py --live`：使用独立 SQLite 测试库上传验收资料，走真实模型规划、批准、检索与报告的完整 HTTP 流程。此命令会消耗模型额度，结果与报告留在 `runtime/research-acceptance/`；不会使用现有用户的业务库。
