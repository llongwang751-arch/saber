# 第二轮优化与实测记录

本轮继续处理 `AGI-saber-python/final`。原知识库语料哈希仍为 `4ebc22ec488d87f9e71ba7934d8f7da46e7fb94379d71d3762637666976d78f9`。没有重置 Docker、删除原卷或修改原知识库内容。

## RAG：修复候选浪费，补齐拒答诊断

原来多个子片段属于同一父段落，却占满 Top-K，进入回答阶段才去重。本轮把同文档、同版本、同父段落的精确去重提前，给其他段落留下候选位置；不会把不同文档/版本直接合并。

每次回答记录词面/重排证据得分、阈值和是否通过，拒答分为：没有候选、证据分数不足、模型主动拒答、JSON 不合法、缺少引用、引用来源或引文不合法。诊断中不额外保存完整原文。模型主动拒答不再显示成引用格式错误。

生产 LLM 客户端增加可选 usage 回调；只传递服务返回的 prompt/completion/total token 数，观察者故障不影响模型回答。不记录请求密钥或地址。

同一份真实知识库、同一组 50 题、Top-K=3、阈值仍为 0.08：

| 指标 | 上轮分词修复后 | 本轮父段落去重后 |
|---|---:|---:|
| 有答案题金标证据 Hit@3 | 31/36 | 33/36 |
| MRR | 0.8102 | 0.8426 |
| 有答案题返回带引用回答 | 30/36 | 32/36 |
| 引用了含金标锚点的证据 | 29/36 | 31/36 |
| 无答案题拒答 | 14/14 | 14/14 |
| 中位延迟 | 0.963 秒 | 1.021 秒 |
| P95 延迟 | 3.189 秒 | 5.530 秒 |

真实生成 42 次，服务报告输入 29,715 tokens、输出 12,810 tokens，总计 42,525。未估算价格，未统计 embedding 费用。生成存在波动，本轮没有声称差值统计显著。

仍有四道误拒答：

- q03/q04：Python 版本问题的词面分数分别约 0.0773/0.0708，低于阈值，属于阈值误拒答。
- q24/q27：相关自检/归因脚本没有进入足够准确的最终证据，模型主动拒答。并非引用格式校验失败。

这仍是**单文档本地检索回归集**，不是跨文档泛化测试；含金标锚点的引用比例也不是语义正确率。没有为提高这套题的分数而降低全局阈值。`scripts/run_existing_kb_eval.py` 现支持 local/keyword/semantic/hybrid 独立端口评测，远端初始化失败会报错，不会静默冒充混合检索成功。

摘要：`existing-kb-live-summary-followup.json`。逐题证据在 `runtime/kb-eval-20260919-diversity/`。

## PostgreSQL 与记忆一致性

修复连接池连接成功后 `_conn` 被归还置空，导致跳过 bootstrap 的问题；连接成功日志不再打印完整 DSN。

PG RAG 仓储补充 document_id/version_id/section 的存储、upsert 保留和检索回填，平台和基础设施两条建表路径均包含兼容迁移。偏好仓储检查实际受影响行数，数据库返回 -1/0 时不能向上层假报保存成功。

任务快照新增缺失的租户列 bootstrap；PG 同名 task_id 冲突时只能更新原租户的内容，禁止覆盖其 user_id。现有全局 task_id 主键保留，异租户同名写入被明确拒绝，尚未改成复合主键。

独立真实 PostgreSQL 已通过：

1. 连接池建表、RAG 写入和来源字段回读、重复 upsert 保留来源。
2. RAG、偏好、会话历史租户隔离及同用户不同会话隔离。
3. 记忆更正 CAS；旧版本不能覆盖新版本。
4. 在 outbox 写入后注入真实 SQL 除零异常，验证记忆行与投影事件一起回滚。
5. 重启**独立测试 PG 容器**后，偏好、会话和更正后的记忆仍存在。

证据：`live-persistence-eval.json`。快照防接管是在这次成功实测之后追加的修复，后续脚本已增加检查；因环境再次掉线，不能把它计入上述已通过的真实 PG 检查。

## Neo4j 来源关系

独立 Neo4j 真实结构检查通过：同一实体保留两个来源；删除一个文档后另一来源仍在；其他租户同名实体不串读；旧实体最后已知 pg_id 来源可回填。证据：`live-graph-eval.json`。

这里使用已知实体作为结构测试输入，没有把它当成“真实模型抽取准确率”或“图问答质量”评测。历史上已被覆盖、数据库中没有保留下来的来源依然需要从原文重建。

## Harness：持久化恢复与禁止重复派发

独立 Harness 的 DAG checkpoint 现在保存完整图、节点状态、节点结果和用户上下文。新 runtime 可以恢复未派发节点，完成节点不会重跑。已完成任务再次恢复会直接返回保存的结果。

ReAct 在进入外部工具前记录 dispatching；DAG 在派发前记录 running。发现这种未确认结果的状态时，自动恢复会中断，不猜测外部动作有没有成功。不能承诺 exactly-once；这是保守的 at-most-once 派发约束。

动作领取使用持久化原子操作：SQLite 唯一键，JSONL 存储使用独占创建的 claim 文件，内存存储只提供进程内互斥。claim 不会自动过期，避免崩溃后错误重放。旧的、缺少完整图的 DAG checkpoint 继续拒绝自动恢复。

验证包括：重新打开 SQLite 续跑剩余节点；四个存储实例同时争抢只允许一个成功；真实子进程先写文件再 `os._exit(23)`，ReAct 与 DAG 恢复都不会重复写入。相关 Harness/边界测试 22 项通过。

**边界：这项新增完整 DAG 恢复属于独立 Harness。主 Agent 的 GraphRuntime/HTTP 多步任务恢复尚未完成，不能把二者混为一谈。**

## 本机基础设施与测试条件

本机缓存 `postgres:15` 镜像的 entrypoint 是 0 字节，直接执行报 exec format error。改用验证可启动的 postgres:16-alpine，并使用新测试卷 pg16，未迁移或覆盖原数据卷。

Neo4j 缓存镜像的配置含空字节；ES keystore 读取报 EOF。测试 Compose 使用独立干净 Neo4j 配置，ES 仅在测试容器里重建临时 keystore。所有测试端口绑定 127.0.0.1，给容器设置了内存上限。

Docker/WSL 多次发生桥接/引擎退出；本轮也观察到 Python MemoryError、OpenBLAS 分配失败以及一次测试 SQLite disk I/O error。尚无证据把这些归为同一个根因。磁盘剩余空间检查 C 约 19 GB、D 约 14 GB，不能据此声称文件系统健康。

因此 PG 和图结构曾通过验证，不等于整套环境已稳定恢复。ES/Milvus 真实质量、三路融合、负载和服务故障恢复评测仍未通过完整验收。没有修改全局 WSL 配置或终止其他应用来掩盖问题。Docker 官方也建议单独管理 WSL 资源限制，参见 [WSL best practices](https://docs.docker.com/desktop/features/wsl/best-practices/)；本轮未替用户修改全局资源配置。

测试设施修复了三个问题：补齐与现有 Starlette 兼容的 httpx 测试依赖；取消所有 pytest 进程共用的 basetemp；文档 API 手写 ASGI 客户端等响应结束后才发送 disconnect，避免测试自己截断响应体。全量检查脚本使用独立临时目录和数据库，限制 BLAS 线程，并分小批运行以降低资源压力。

最终全量分批结果：**714 passed，1 skipped**。其中一个 MCP 超时边界用例发现浮点误差会使 80ms 预算略超上限；已在 ToolCallContext 将剩余时间限制在原始预算内，对应整批 25 项复测通过，原失败记录保留在汇总 JSON 中。全量之后新增两项持久化/usage 回调用例，连同相关用例共 10 项通过；不要将这 10 项全部累加到全量数量。证据：`optimization-followup-test-summary.json`。`pip check`、Compose 配置验证与修改文件语法检查也通过。

## 复现入口

```powershell
.venv-eval/Scripts/python.exe scripts/run_optimization_checks.py
.venv-eval/Scripts/python.exe scripts/run_existing_kb_eval.py --mode local --output runtime/kb-new-run
docker compose -f docker-compose.eval.yml up -d
.venv-eval/Scripts/python.exe scripts/run_live_persistence_eval.py --restart
.venv-eval/Scripts/python.exe scripts/run_live_graph_eval.py
.venv-eval/Scripts/python.exe scripts/run_existing_kb_eval.py --mode hybrid --output runtime/kb-hybrid-new-run
```

后续仍需：稳定 ES/Milvus 环境并完成各模式校准；主 Agent 图任务恢复；跨用户 Agent 的安全租约与淘汰；偏好、事实记忆等不同仓储间的统一事务边界；独立留出集的引用语义验证。
