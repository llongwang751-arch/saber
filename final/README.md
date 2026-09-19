# AGI Assistant · 新同学启动指南

> 2026-09-13 性能核对：当前 Python `LocalRagChunkRepo.search_local` 是按租户过滤的词法扫描，并未使用 FTS5。本轮改为仅读取文本列、流式计算、有界 TopK，避免解码不用的向量；排序语义及租户隔离保持不变。Mini-Drop 使用独立测试库完成 Linux 同负载对照，详见 [本地检索性能核对](docs/本地检索性能核对-20260913.md)。这些结果不代表完整聊天、远程模型或 Milvus 验收。

一份"从 git clone 到看到首页"的最短路径。读完按步骤操作即可启动。

> 项目位置：本仓库的 `python` 分支，代码在 `final/` 目录下。
> 后端：FastAPI + Uvicorn（端口 **8090**）；前端：Vue 3 + Pinia + Vite，构建后由后端静态挂载在 `/`。
> Go/Python 复刻边界、源码映射和面试讲法见 `docs/Go-Python完全复刻对照与面试说明.md`。

---

## 0. 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 推荐 3.11 | 当前使用 `StrEnum`，不兼容 Python 3.10；其他版本需另行验证 |
| pip | ≥ 23 | |
| Docker / Docker Compose | 可选 | 启动 PG/ES/Milvus/Neo4j/Kafka 全套基础设施时需要 |
| Git | any | |

> macOS / Linux 均已验证；Windows 推荐 WSL2。

---

## 1. 拉代码 & 切到 python 分支

```bash
git clone git@github.com:AGI-Core/AGI-saber.git
cd AGI-saber
git checkout python
cd final
```

---

## 2. 两种启动方式（任选其一）

### 方式 A：纯本地，不起任何基础设施（最快，1 分钟跑通）

所有外部依赖（PG / ES / Milvus / Neo4j / Kafka）都做了 **优雅降级**，没装也能起。

```bash
# 1) 创建虚拟环境（强烈推荐，避免污染系统）
python3.11 -m venv .venv
source .venv/bin/activate

# 2) 安装依赖
pip install -r requirements.txt

# 3) 准备环境变量。至少要换掉 JWT 密钥；模型 Key 可稍后再配
Copy-Item .env.example .env       # Windows PowerShell
# cp .env.example .env            # macOS / Linux

# 4) 启动（main.py 会自动加载项目根目录的 .env，且不会覆盖进程环境变量）
python main.py
```

看到这一行就成功了：

```
INFO:     Uvicorn running on http://0.0.0.0:8090 (Press CTRL+C to quit)
```

打开浏览器：

- 前端：http://localhost:8090/
- 健康检查：http://localhost:8090/healthz
- 就绪检查：http://localhost:8090/readyz
- Swagger：http://localhost:8090/docs

> 纯本地模式也不是一次性 mock：用户、技能、门诊记录、文档及版本、聊天、长期记忆、任务快照、RAG 分块和评测结果都会写入 `runtime/` 下的 SQLite。当前仓库在 D 盘时，数据也在 D 盘。Milvus、Elasticsearch、Neo4j、Kafka 和 PostgreSQL 是分布式增强项，连接失败时本地核心链路仍可运行。

---

### 方式 B：完整基础设施（推荐用于真实测试）

```bash
# 1) 起 PG / ES / Milvus / Neo4j / Kafka（首次拉镜像可能需要 5 分钟）
docker-compose up -d

# 2) 等待容器全部 healthy（大约 30 ~ 60 秒）
docker-compose ps

# 3) 安装 Python 依赖（同方式 A）
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4) 启动应用
python main.py
```

成功后，`/healthz` 可用于存活检查。当前 `/readyz` 为 Go 兼容性探针，直接返回 `ok`，不检查关键依赖；模型、数据库与检索后端需结合鉴权后的状态接口和实际请求验证。

如果只想用 Docker 跑应用本身：

```bash
docker-compose up -d   # 包含 app 服务，会自动构建镜像
docker-compose logs -f app
```

---

## 3. 配置认证和模型

复制 [.env.example](./.env.example) 并只在本机填写。凭证通过环境变量覆盖 [config/config.yaml](./config/config.yaml)，不要把真实 Key 写入受 Git 跟踪的 YAML。

```dotenv
AGI_JWT_SECRET=至少32位的随机字符串
AGI_LLM_API_URL=https://provider.example/v1/chat/completions
AGI_LLM_API_KEY=你的对话模型Key
AGI_LLM_MODEL=模型名
AGI_EMBEDDING_API_URL=https://provider.example/v1/embeddings
AGI_EMBEDDING_API_KEY=你的Embedding-Key
AGI_EMBEDDING_MODEL=Embedding模型名
```

PowerShell 启动示例：

```powershell
Get-Content .env | Where-Object { $_ -match '^[^#][^=]*=' } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
}
python main.py
```

`AGI_AUTH_REQUIRED` 默认为 `1`。本地模式首次打开页面可注册账号；后续用同一账号登录。文档、记忆、医疗业务数据和技能按账号隔离，评测证据按租户共享并由角色控制，便于创建者与另一名审批人在同一证据上完成双人审核。切换到 `production_authenticated` 后公开注册会自动关闭，必须使用后台预置命令创建业务账号。未设置安全 JWT 密钥时 `/api/status` 会暴露开发密钥告警，不能用于生产环境。

获取 Key：
- 火山方舟控制台：https://console.volcengine.com/ark
- 创建一个有 `Chat Completions` + `Embedding` 权限的 API Key

---

## 4. 自检清单

```bash
# 健康检查
curl http://localhost:8090/healthz

# 前端首页（应返回 HTML）
curl -s http://localhost:8090/ | head -c 100

# 注册并保存返回的 access_token
curl -X POST http://localhost:8090/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"demo_user","password":"change-me-123"}'

# 简单聊天（把 TOKEN 替换为上一步 access_token）
curl -N -X POST http://localhost:8090/api/chat \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好"}'
```

---

## 5. 常见问题

### Q1：`AttributeError: module 'marshmallow' has no attribute '__version_info__'`

依赖冲突。`pymilvus` 间接依赖 `environs` → `marshmallow`，新版 `marshmallow 4.x` 不兼容。

**修复**：本仓库 requirements.txt 已 pin `marshmallow<4` 和 `environs<10`。重新安装即可：

```bash
pip install -r requirements.txt --upgrade
```

### Q2：`RuntimeError: Form data requires "python-multipart" to be installed.`

```bash
pip install python-multipart
```

（已写入 requirements.txt，正常 `pip install -r` 就有）

### Q3：`ImportError: cannot import name 'SearchResult' from 'internal.rag.rag'`

旧代码遗留，最新 `python` 分支已修。`git pull origin python` 即可。

### Q4：端口 8090 被占用

```bash
# macOS / Linux
lsof -i :8090
kill -9 <PID>
```

或修改 [main.py](./main.py) 末尾的 `port=8090`。

### Q5：基础设施全部 `disconnected`

属于 **预期行为**，应用做了优雅降级，可以直接用。要接通就跑 `docker-compose up -d`。

### Q6：Milvus 启动慢 / 卡死

Milvus 单机版依赖 etcd + minio，**首次启动需 60s+**。先看日志：

```bash
docker-compose logs -f milvus
```

如果实在不需要 Milvus，可以在 `docker-compose.yml` 注释掉 milvus / etcd / minio 三个服务。

---

## 6. 项目结构速览

```
final/
├── main.py                 # 入口，启 FastAPI app
├── config/
│   ├── config.yaml         # 非敏感默认配置
│   └── config.py           # 配置加载
├── web/                    # Vue 3 + Pinia + Vite 前端
│   ├── src/
│   └── dist/               # 构建产物（被 / 静态挂载）
├── internal/
│   ├── handler/            # HTTP 路由
│   ├── application/        # JWT、多用户、技能、养殖业务、本地持久化
│   ├── evaluation/         # 评测集、适配器、Trace、指标、Badcase 与报告
│   ├── agent/              # ReAct Agent / Router / Planner
│   ├── llm/                # LLM 客户端
│   ├── rag/                # 三路 RRF 检索
│   ├── memory/             # 三层记忆 + 图记忆
│   ├── graph/              # Neo4j 知识图谱
│   ├── promptctx/          # Prompt 多源装配
│   ├── platform/           # PG/ES/Milvus/Kafka/Neo4j 客户端
│   ├── repo/               # 数据访问层
│   ├── sandbox/            # Docker / 本地沙箱
│   ├── tools/              # exec_command / tavily 等工具
│   └── infra/              # Infrastructure 初始化
├── Dockerfile
├── docker-compose.yml
├── runtime/                # 本地 SQLite、分租户评测库、报告等运行数据
├── alembic.ini             # 全部应用数据库迁移入口
├── examples/evaluation/    # 合成、脱敏的评测样例
├── scripts/                # 一键离线评测演示
└── requirements.txt
```

---

## 7. Agent 质量评测闭环

离线 Replay 评测不需要 LLM Key，也不依赖 Milvus 或 Elasticsearch。本地开发默认用每租户一份 SQLite 保存不可变评测集、运行结果、Trace、Badcase 和人工标注；多副本生产环境必须配置共享的非 SQLite 租户数据库，否则后端会拒绝启动真实线上实验或宣称效果。

先执行数据库迁移和离线演示：

```bash
python -m alembic upgrade head
python scripts/run_agent_eval_demo.py
```

演示会用 12 条合成用例分别运行故障基线和修复版，输出 Markdown/CSV 报告到 `runtime/reports/`。这些数字是确定性回放结果，只用于展示评测闭环，不能当作真实模型效果或医学准确率。

要一次运行 Agent、RAG、记忆和 Harness 的分层 Benchmark，并生成统一 JSON/Markdown 报告：

```bash
python scripts/run_benchmark_suite.py
```

报告写入 `runtime/benchmark-suite/`。其中 Replay 分数验证评测器和回归门禁，RAG/记忆/Harness 组件门禁会执行真实核心类；两者都不能冒充线上模型或真实业务效果。

复核 Go `845e8f7` 与 Python 当前主链的一致性：

```bash
python scripts/run_go_python_conformance.py
```

脚本核对冻结 Go commit 与关键源码哈希，并运行 Go 全包测试和 Python 路由、报告 DAG、RAG、记忆、MCP、HTTP 对照合同；结果写入 `runtime/go-python-conformance/`。Go 仓库不在默认相邻目录时使用 `--go-root <目录>`。该报告是工程合同证据，不是线上效果结论；真实 PostgreSQL、Milvus、Elasticsearch、Neo4j、Kafka、MCP 服务与 Docker 沙箱仍需完整环境集成和故障注入。

启动服务并登录后，可直接点击顶栏“Agent 评测”，一键运行腾讯 JD 合成样例、查看进度、门禁、Trace、Badcase 和版本对比；也可以在 Swagger `http://localhost:8090/docs` 中使用右上角 Authorize 填写 Bearer Token 后调用：

```text
POST /api/eval/demo/bootstrap
GET  /api/eval/runs/{run_id}/progress
GET  /api/eval/runs/{run_id}/events
GET  /api/eval/runs/{run_id}/results
GET  /api/eval/case-runs/{case_run_id}/trace
GET  /api/eval/badcases
POST /api/eval/badcases/{badcase_id}/verify
POST /api/eval/runs/compare
GET  /api/eval/runs/{run_id}/report
GET  /api/traces
GET  /api/traces/{trace_id}
DELETE /api/traces/{trace_id}
POST /api/traces/purge
POST /api/rag/reindex
GET  /api/rag/projections/status
POST /api/rag/projections/retry
```

HTTP 路由以运行时 OpenAPI 为准；应用数据库当前迁移头为 `0017_memory_supersession_provenance`，共 17 段 Alembic 迁移。继续阅读：

- [对应 Agent 岗位 JD 的源码深读课：八章、80 问与演示训练](./docs/JD-Agent工程深读/00-学习入口与项目真实性地图.md)
- [全功能跑通与面试演示指南](./docs/全功能跑通与面试演示指南.md)
- [系统架构与核心流程图](./docs/系统架构与核心流程图.md)
- [Python/FastAPI 项目学习路线](./docs/Python-FastAPI项目学习路线.md)
- [Agent 质量评测平台使用指南](./docs/Agent质量评测平台使用指南.md)
- [Agent、RAG、记忆与 Harness Benchmark 指南](./docs/Agent-RAG-记忆-Harness-Benchmark指南.md)
- [RAG 测试集与评测审计](./docs/RAG测试集与评测审计.md)
- [腾讯医疗 AI Agent 质量评测面试准备](./docs/腾讯医疗AI-Agent质量评测面试准备.md)
- [Python 源码深度学习与面试手册](./docs/AGI-saber-Python源码深度学习与面试手册.md)
- [RAG 可靠性与故障恢复运行手册](./docs/RAG可靠性与故障恢复运行手册.md)
- [真实在线 RAG 实验控制面验收](./docs/P3-真实在线RAG实验控制面验收-20260914.md)
- [生产运行环境组件清单与可信指纹](./docs/P3-运行环境组件清单与可信指纹.md)
- [受控策略建议与人工演进验收](./docs/P4-受控策略建议与人工演进验收-20260914.md)

---

## 8. 其余可演示功能

- **对话主链**：与 Go `845e8f7` 一致分为 `rag_agent / rag / react` 三路。知识库已启用且有内容时，命中研究/总结/报告/文档/方案/分析关键词会执行 `research → writer → review → doc` 固定 DAG，并将 Markdown 保存到本地文档库、回填 RAG；Review 当前只保留意见和元数据，不会自动改写正文或阻止保存。普通知识问答走轻量 RAG，其他请求走统一工具执行链。
- **三路 RAG 融合**：一级 RRF 中 Milvus 与 Elasticsearch 均使用 `1.0` 权重，知识图谱使用 `kg_weight`；`semantic_weight=0.7` 仅为配置结构兼容保留，不参与当前计分。
- **结构化 MCP 调用**：保留 payload、原始 JSON、错误码、耗时和 `retryable`；参数错误/HTTP 4xx/取消不重试，网络错误/超时/HTTP 5xx 才按上限重试。
- **本地知识库**：上传 TXT、Markdown、可提取文字的 PDF，查看版本、重新入库或删除；离线模式也会持久化 RAG 分块，重启后仍能检索。
- **Skill 广场**：安装、启停、卸载 7 个内置技能；GitHub 搜索只读取仓库元数据，不下载和执行第三方代码。
- **智慧养殖**：导入 CSV、XLSX，进行年度、月周、日报、断奶、生长和饲料效率分析，支持异常提示及 Markdown 报告落入本地文档库。
- **记忆治理**：查看隔离、解除隔离、合并/替代后的长期记忆状态。本地 SQLite 在同一数据库事务写权威行与 Outbox，由本地 Worker 更新投影账本；生产 PostgreSQL 在同一事务写 `long_term_memory` 与按 target 拆分的 Outbox，再由带租约、重试、dead-letter 和周期对账的消费者投影到 Milvus/Neo4j。真实外部集群恢复能力仍需完整环境验证。
- **Agent 评测**：不可变数据集版本、Replay/Local/HTTP Adapter、16 项意图/工具/RAG/记忆/Harness/安全与 Trace 指标、S0/S1 硬门禁、SSE 进度、Badcase、版本回归和报告导出。
- **离线策略晋级**：候选策略版本、成对统计比较、双人审批、显式激活与回滚；审批证据在使用前重新校验，不能把 Replay 分数冒充线上收益。
- **真实在线实验**：稳定分桶、曝光与反馈账本、固定停止规则、归因成熟度、分流异常检查（SRM）、安全熔断和审计链；只统计后台预置且符合资格的业务账号。
- **受控策略演进**：根据 RAG Badcase 生成可解释的参数建议，经另一人审核后才能物化为新的离线候选；不会自动修改线上策略。
- **运行 Trace**：每次回答生成 `trace_id`，结构化过程脱敏后持久化，并按登录用户隔离查询。
- **RAG 故障恢复**：远程精排失败可切换本地确定性重排，Embedding/检索依赖独立熔断，ES/Milvus 投影可从主存储重新构建。

所有功能都受 JWT 用户边界保护。演示数据是合成数据，不代表医疗诊断准确率或真实生产吞吐。

---

## 9. 数据位置与清理

默认数据位于当前 `final/runtime/`：

```text
runtime/evaluation.db             账号、技能、文档、记忆、养殖等应用数据
runtime/evaluation-tenants/       本地开发使用的每租户评测数据库
runtime/reports/                  Markdown / CSV 报告
```

Docker 模式使用 `app_runtime` 数据卷。要迁移数据，先停服务，再整体备份 `runtime/` 或 Docker volume。不要在运行中直接删除 SQLite 文件。

生产实验账号需由后台显式预置；公开注册、开发账号、管理员和审批人均不会计入真实曝光：

```bash
python scripts/provision_experiment_user.py \
  --username farm_operator --tenant-id farm-a --create --experiment-eligible
```

命令会交互读取密码，不接受命令行密码，也不会输出密码或哈希。已有旧账号默认标记为 `legacy_unverified`，必须经过同一命令重新归属租户并明确授予实验资格。

生产在线实验还必须由构建/发布流水线生成只读的运行环境组件清单，并通过部署控制器单独注入清单摘要。旧变量 `AGI_EXPERIMENT_RUNTIME_ENVIRONMENT_FINGERPRINT` 仅保留兼容用途，手填任意 64 位十六进制字符串不能通过生产证据就绪检查。完整步骤见[生产运行环境组件清单与可信指纹](./docs/P3-运行环境组件清单与可信指纹.md)。

---

## 10. 开发约定

- 提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/)：`feat(xxx): ...` / `fix(xxx): ...`
- 新增依赖一定要写进 `requirements.txt` 并明确版本
- API Key / 私钥 **绝不提交**
- `python` 分支为 Python 主线，`main` 分支为 Go 版本

---

有任何卡点直接问，或者把启动日志贴给维护者。
