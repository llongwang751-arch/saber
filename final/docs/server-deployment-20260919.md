# Python 版服务器部署记录

部署日期：2026-09-19。仅部署 `AGI-saber-python/final`。

后续更新：已切换到 Chroma + FTS5 + SQLite 图检索，并补上主任务恢复、SQLite 记忆原子纠错和每日异机备份。下文保留首次部署记录；最新状态见 [恢复与备份验收](recovery-memory-backup-20260919.md)，检索专项见 [轻量检索验收](lightweight-retrieval-20260919.md)。当前生效 release 以 `recovery-validation-summary.json` 为准。

## 访问

服务器：`ubuntu@106.52.176.128`，主机名 `mini-drop-worker-1`。
应用仅监听服务器 `127.0.0.1:8090`。当前电脑已建立 SSH 隧道，可访问：

http://127.0.0.1:18090/

电脑重启或隧道断开后，在终端执行并保留该窗口：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:18090:127.0.0.1:8090 ubuntu@106.52.176.128
```

可使用原有账号密码登录。新生成了服务器 JWT 密钥，原有登录令牌不适用于服务器。尚未配置公网域名或 HTTPS，不应把上面的本机地址当作公网访问地址。

## 运行与边界

- Ubuntu / Python 3.12，服务器 2 CPU、约 2 GB 内存。
- `/opt/agi-saber/app`：代码与 Vue 构建；`/opt/agi-saber/venv`：独立依赖。
- `/opt/agi-saber/runtime/application.db`：迁移的数据库快照。
- `/opt/agi-saber/server.env`：模型凭据和服务器 JWT 密钥，仅 root 可读，systemd 注入进程。
- `agi-saber.service`：开机启动、失败重启，专用账号 `agisaber`，内存上限 900 MB、CPU 上限一核。
- 代码只读，运行数据目录可写；重启后空闲内存占用约 211 MiB。
- 当前是 SQLite 本地检索与持久化模式，没有部署 PG / ES / Milvus / Neo4j / Kafka。关闭图检索、SkillHub 自动集成与推理 racing。
- Docker 命令沙箱未接入该专用账号；不能将此部署理解为终端执行工具已验收。未授予宿主机 Docker socket 权限。
- 现有 `mini-drop-worker-agent-1` 保持运行；Worker 2 的现有服务未修改。
- 数据为迁移时快照，本地与服务器后续写入不会自动同步。

## 实际验证

- 上传压缩包 SHA-256：`d7a7899c78d9819f15c08e1e8445021fc3ae3185913f3d43add41c41791c2c63`，两端一致。
- 使用 SQLite backup API 只读读取本地原库，快照与服务器数据库完整性检查均为 `ok`。
- 首页、`/healthz`、`/readyz` 返回 200；`/readyz` 本身不证明模型或外部检索服务就绪。
- 未登录访问 `/api/status` 返回 401；注册、登录及登录后状态接口通过。
- 真实 `/api/chat` 返回“服务器部署验证成功。”，`fallback=false`、`error=null`。
- 服务重启后存活探针通过，原有 1 文档、1 版本、31 分块、4 条长期记忆保留。
- 原有 4 用户、14 条聊天记录保留；部署验证新增 1 个隔离测试账号和 2 条聊天记录。
- 本次未重复运行完整 50 题 RAG 评测，也未完成外部数据库模式验收。

## 运维

```bash
sudo systemctl status agi-saber
sudo journalctl -u agi-saber -n 100 --no-pager
sudo systemctl restart agi-saber
```

若需要暂停本项目而不影响其他服务：

```bash
sudo systemctl stop agi-saber
```

部署工具及本地原始验收结果保存在未跟踪的 `runtime/` 下。部署压缩包含凭据与数据库，不应提交 Git 或公开分享。服务器传输目录为 `/home/ubuntu/agi-saber-transfer`（仅该用户访问）。本次保留原始迁移包供回溯，没有删除源数据库。
