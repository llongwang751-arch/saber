# 现有知识库真实评测（2026-09-19）

本轮只处理 `AGI-saber-python/final`。现有知识库通过 SQLite `mode=ro` 抽样，写入全新的独立数据库；未向原知识库写数据。资料中出现的脚本命令只是问答证据，没有执行。知识库中保存的是 tx-harness README，不是本轮对其他项目源码的审查。

## 范围与方法

- 原库：`runtime/evaluation.db`；1 份文档、31 个片段。
- 逻辑语料 SHA-256：`4ebc22ec488d87f9e71ba7934d8f7da46e7fb94379d71d3762637666976d78f9`。运行结束重新读取校验。
- 50 道人工编写问题：18 个事实主题各 2 种问法，共 36 道有答案题；另有 14 道知识库未提供答案的问题。
- 使用生产 `LocalRagChunkRepo → HybridStore → Engine`，调用配置中的真实模型，禁止 mock。模型服务收到的是检索出的现有资料。
- `top_k=3`，词面证据阈值 `0.08`；未以这套题调阈值。这是一套回归基线，不是独立留出测试集。
- 隔离库重建本地 FTS 索引，禁用远程 reranker。因此衡量本地检索路径，不代表线上所有装配方式。
- 指标中的“命中”指命中含人工金标锚点的证据；“回答”指通过引用检查并返回回答。二者都不是语义正确率。
- 金标锚点不是所有正确表述的穷举。例如 q02 能引用另一个段落解释 Harness，但没有命中选定锚点，不能直接判为错误答案。
- 来源文本对项目效果的宣称未作外部事实核验。引用检查只证明引文在资料里，不能证明结论真实或推理成立。

## 实测结果

正式修复前记录在 `runtime/kb-eval-20260919-final/`，分词修复后记录在 `runtime/kb-eval-20260919-tokenfix/`。可公开的修复后指标见 `existing-kb-live-summary.json`。

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 金标证据 Hit@3（36 题） | 31/36（86.1%） | 31/36（86.1%） |
| MRR | 0.8102 | 0.8102 |
| 有答案题返回带引用回答 | 27/36（75.0%） | 30/36（83.3%） |
| 有答案题引用含金标锚点证据 | 26/36（72.2%） | 29/36（80.6%） |
| 无答案题拒答 | 14/14 | 14/14 |
| 请求异常 | 0 | 0 |
| 中位延迟 | 1.119 秒 | 0.963 秒 |
| P95 延迟 | 2.786 秒 | 3.189 秒 |
| 真实生成调用 | 37 | 40 |

空租户读取隔离检查通过，原语料哈希保持一致。首次探索运行曾得到 29/36 回答，说明生成环节有波动；以上差值不等于已证明泛化提升，尤其两道证据等级题在不同轮次表现不一致。首轮还额外执行一次检索用于计算排名；正式记录改为直接使用生产检索 trace，避免重复查询干扰延迟。

确认修复的例子：q01“tx-harness 是做什么的？”从检索命中后误拒答，变为引用标题与背景作答。

仍存在 6 道误拒答：q03/q04（Python 版本）、q19（每题需要的材料）、q21（结果大表路径）、q24（阶段 4 自检脚本）、q27（归因分析脚本）。Python 的 token 已能匹配，但较长中文问句仍会稀释覆盖分数；这需要独立校准，不能只为这两题下调全局阈值。后两题的生产 trace 将“模型主动拒答”和“引用格式验证失败”合并记录，现有记录不足以精确区分。

## 基础设施结果

按用户指定的 `Docker Desktop（修复启动）.lnk` 启动，其脚本为 `D:/DockerRuntime/Start-Docker-Desktop.ps1`，通过设置独立 `LOCALAPPDATA` 绕过原来的 socket 路径错误。

Docker 引擎曾返回版本 `29.7.2`，原有 `neo4j-db` 容器曾显示运行。随后拉取独立评测镜像时出现 `unexpected EOF` 和 Docker API 500。Docker 后端日志在 `2026-09-19T10:53:05Z` 记录 `vpnkit-bridge` 退出码 `0xc00000fd`，随后 WSL 引擎停止。尚未确认根因，不能把“启动过”记为恢复成功。

`docker-compose.eval.yml` 使用独立项目名、卷与端口：PG 15432、ES 19200、Milvus 19540、Neo4j 17687。没有复用原库；原 9200 端口由 cpolar 占用。

真实只读探针结果见 `live-isolated-smoke.json`：PG、ES、Milvus、Neo4j 连接均失败；真实 embedding 返回 1024 维，真实生成返回带可验证引文的答案。**混合检索、图来源重建、远端读写与恢复验证仍未完成。**

## 本轮修复

1. 独立 Python 3.12 评测环境安装现有依赖。为 pymilvus 2.4 的 `pkg_resources` 依赖固定 `setuptools<81`，`pip check` 通过。
2. 修复 `internal/agent/subagents.py` 缺少 `Optional` 导入的问题；该问题在 Python 3.12 下阻止模块导入。
3. 修复本地证据评分先删除所有空格的问题：`Python 3.10` 不再变成 `python3.10`；标题标识符尾部的冒号不再破坏匹配。保留文件路径 token。未降低拒答阈值。

## 可复现命令

在 `AGI-saber-python/final` 目录执行，输出目录必须尚不存在，避免覆盖旧评测：

```powershell
.venv-eval/Scripts/python.exe scripts/run_existing_kb_eval.py --output runtime/kb-eval-new-run
.venv-eval/Scripts/python.exe -m pytest tests/test_existing_kb_eval.py tests/test_optimization_boundaries.py tests/test_rag_alignment.py tests/test_threshold_calibration.py tests/test_subagents.py tests/test_local_reranker.py -q
docker compose -f docker-compose.eval.yml up -d
.venv-eval/Scripts/python.exe scripts/run_live_optimization_eval.py --profile isolated --output docs/live-isolated-smoke.json
```

输出包含原始语料快照、带锚点的问题集、逐题答案与检索 trace、指标摘要、独立 SQLite 库。完整资料留在本地 `runtime/`，不放进公共报告。

相关 53 项测试通过；Compose 配置校验通过。测试通过不表示 Docker 服务已经可用。真实生成存在波动，单轮差值不作统计显著性或因果提升的证明。

## 后续验收条件

先使修复版 Docker/WSL 连续稳定运行，再完成隔离 PG/ES/Milvus/Neo4j 的写入、查询、租户交叉读取和重启恢复测试。随后分别评测本地、BM25、向量、混合与图检索。不能用本地模式数据校准其余模式。

现有单文档无法覆盖跨文档事实冲突、版本纠正和实际业务资料。补充多文档后按事实主题分组切分校准集与留出集，防止同一事实的两种问法分到两边。成本统计还需保留模型服务 usage；当前仅记录真实调用次数，不编造 token 数和费用。
