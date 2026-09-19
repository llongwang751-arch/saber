# Agent、RAG、记忆与 Harness Benchmark 指南

> 面向项目学习和面试准备。先记住一句话：**Benchmark 不是“跑了几个例子”，而是用固定试卷、标准答案、统一规则和发布门禁，可重复地比较两个 Agent 版本。**

## 1. Benchmark 到底是什么

把 Agent 当作考生，一套完整 Benchmark 至少有五个部分：

1. **版本化测试集**：固定题目、场景分布和风险标签，修改标注时生成新版本。
2. **Oracle（标准答案）**：期望意图、槽位、工具、证据、拒答或执行状态。
3. **统一执行协议**：固定模型、Prompt、工具、知识库版本、参数和超时策略。
4. **指标**：将“好不好”拆成可计算的意图、检索、生成、记忆、安全、延迟等指标。
5. **门禁与对比**：同一套题比较 baseline 和 candidate，拦截硬门禁失败或新增回归。

所以，Benchmark 的输出不应只有一个总分，还应包含分维度指标、逐用例 Trace、Badcase、版本差异、延迟/成本和门禁结论。

## 2. 它与单元测试、演示和 A/B Test 的区别

| 方式 | 主要回答什么 | 典型数据 | 能证明 | 不能单独证明 |
|---|---|---|---|---|
| 单元测试 | 这个函数/类的行为对不对 | 少量构造输入 | 代码契约和边界条件 | 端到端 Agent 质量 |
| Demo | 主链路能否被看见 | 一两个精选样例 | 页面、接口或链路可运行 | 稳定性、覆盖度和统计意义 |
| Replay Benchmark | 评分器、数据集、报告和门禁是否可复现 | JSONL 中预置的 baseline/fixed 输出 | 评测基建正确，回归分类可解释 | 真实模型变好了 |
| Local/HTTP 离线 Benchmark | 真实代码、模型和工具在固定题上表现如何 | 脱敏样本、人工标注、固定知识库 | 离线任务效果、故障恢复、延迟和成本 | 真实流量下的业务收益 |
| 在线 A/B Test | 新策略在真实用户上是否更好 | 随机分流的线上请求 | 业务和用户指标的真实变化 | 所有极端风险已被覆盖 |

最稳妥的关系是：**单测保代码契约，离线 Benchmark 保质量回归，在线 A/B 验证业务收益。** Demo 只负责展示，不代替其中任何一层。

## 3. 评测金字塔

```text
L4  在线 A/B：真实流量、业务指标、统计显著性
L3  Local/HTTP 端到端：真实模型 + 真实 Agent + 真实工具/知识库
L2  Replay 回归：版本化数据 + 固定输出 + 评分器 + 发布门禁
L1  组件契约：RAG、记忆、Harness 真实类 + 内存仓库/可控假件
L0  Schema/单测：字段校验、指标公式、异常分支
```

这个项目当前新增的资产主要覆盖 L0–L2。Local/HTTP 适配器已提供接入方式，但真实模型数据要在配置模型或候选服务后另行跑；线上 A/B 也不能由离线固件代替。

## 4. Agent 框架怎么评

Agent 不是只评最后一句话，而要评“理解—规划—执行—恢复—回答”整条链路。

| 阶段 | 核心指标 | 用例要覆盖什么 |
|---|---|---|
| 意图理解 | `intent_accuracy`、多意图识别、澄清准确率 | 明确意图、模糊意图、无需工具的闲聊 |
| 信息收集 | `required_slot_recall`、修正后槽位准确率 | 缺参数、嵌套参数、多轮更正 |
| 工具调用 | `tool_selection_f1`、`tool_argument_accuracy`、`tool_outcome_accuracy` | 选对工具、参数精确、不该调用时克制、结果状态正确 |
| 任务规划 | 依赖顺序、并行度、动态重规划成功率 | DAG 依赖、并行检索、上游失败后重规划 |
| 回答质量 | `required_content_recall`、任务成功率 | 必要信息是否给全，是否虚假声称执行成功 |
| 故障恢复 | `fallback_recovery`、重试恢复率 | 超时、限流、畸形结果、未知工具、部分失败 |
| 安全 | `privacy_non_disclosure`、`boundary_safety` | Prompt Injection、秘密外泄、危险操作确认 |
| 可观测 | `trace_completeness`、Trace 顺序 | plan/tool-call/tool-result/fallback/final 事件完整且顺序正确 |

### 当前 Agent 测试资产

- [Agent 能力数据集](../examples/evaluation/agent_framework_capability_eval.jsonl)：19 条脱敏合成用例，覆盖意图、槽位、工具、规划、Trace、安全和异常恢复。每条都有故意构造的 `baseline` 失败输出和 `fixed` 通过输出。
- [Agent 数据集测试](../tests/test_agent_framework_eval_dataset.py)：检查 Schema、能力覆盖、固定输出评分、失败归因、硬门禁和多步 Trace。
- [通用指标单测](../tests/test_benchmark_metrics.py)：直接校验 RAG 排序公式、无答案判断、记忆读写、工具结果和 Trace 顺序。

`baseline` 全部失败、`fixed` 全部通过是这份**合成回放试卷的设计**，证明评分器能找到对应 Badcase；不是“某个真实模型从 0% 提升到 100%”。

## 5. RAG 怎么评

RAG 必须拆开评检索和生成。只看最终回答，很难判断是“没检索到”还是“检索到了但生成器没用对”。

### 5.1 检索指标

- **Recall@K** = Top-K 中命中的相关文档数 / 全部相关文档数。适合看“该找到的有没有漏”。
- **MRR@K** = 第一个相关文档名次的倒数。第 1 名命中是 1，第 2 名是 0.5，适合看第一个可用证据是否靠前。
- **nDCG@K** 同时考虑相关性等级和排序位置。它能区分“直接答案在第 1 名”和“勉强相关内容在第 1 名”。
- **Hit@K / Precision@K** 可作为补充：前者关心是否至少命中一条，后者关心 Top-K 噪声比例。

无答案问题的相关集合为空，Recall/MRR/nDCG 的分母没有意义。此时应将排序指标标记为 N/A，单独评估 `no_answer_decision`、拒答准确率和虚构率，不能把“什么都没找到”当成满分。

### 5.2 生成与引用指标

- 答案正确性：回答是否包含 Oracle 中的必需事实。
- Faithfulness（忠实性）：回答中的声明是否都能被检索证据支持。
- 证据覆盖率：应回答的事实有多少已绑定正确证据。
- 引用精确率：已引用的证据有多少真的支持对应声明。
- 端到端指标：任务成功率、P50/P95 延迟、Token/模型调用成本和降级率。

### 5.3 当前 RAG 测试资产

- [RAG 质量数据集](../tests/fixtures/rag_quality_eval_v1.jsonl)：9 条合成用例，分别覆盖基础命中、分级相关性、无答案、多轮 Query Rewrite、Rerank、重复 Chunk、声明-证据归因、租户隔离和语义检索降级。
- [RAG 组件与数据集测试](../tests/test_rag_quality_eval_dataset.py)：除验证数据一致性外，会调用项目中的 Rewriter、Reranker、RAG Engine、本地存储和 Hybrid Store 契约。外部模型、向量库和网络用可控假件替代，因此可确定复现。

当前固件对 candidate 的检索门禁是平均 Recall@3 = 1、MRR@3 = 1、nDCG@3 ≥ 0.99，且 nDCG 必须优于 baseline。这些数字是**固件排序的预期值**，不是真实生产语料上的已测结果。

## 6. 记忆系统怎么评

记忆的难点不是“能写进去”，而是要同时控制该写、该读、该更正、该忘记和绝不能串租户的边界。

| 类型 | 当前可执行指标 | 门禁含义 |
|---|---|---|
| 多轮使用 | `coreference_entity_recall` | 上下文指代能恢复成完整查询 |
| 偏好保留 | `preference_retention_accuracy` | 记住用户自己明确偏好 |
| 偏好更正 | `preference_correction_accuracy` | 新值生效，旧值不再出现 |
| 抗污染 | `memory_contamination_rate` | 第三方陈述不得写成用户偏好；期望为 0 |
| 隔离 | `cross_tenant_leak_rate` | 用户之间不得泄漏；期望为 0 |
| 矛盾管理 | `stale_fact_exposure_rate` | 已被 supersede 的事实不再参与正常召回；期望为 0 |
| 忘记 | `expired_memory_removal_f1` | TTL、重要性和衰减策略删对且不误删 |
| 合并 | `memory_dedup_f1` | 近义记忆去重后保留正确主记忆和标签 |
| 图谱扩展 | `graph_expansion_recall` | 种子记忆可召回关联邻居 |

通用评分协议还支持 `required_memory_reads`、`required_memory_writes`、`forbidden_memory_reads` 和 `forbidden_memory_writes`。跨租户禁止读取命中是 S0 硬门禁，危险内容写入是 S1 硬门禁，都不能靠其他高分抵消。

## 7. Harness 基建怎么评

Harness 是 Agent 执行引擎的“安全带”，负责调度、重试、超时、取消、依赖传播、快照和重规划。这一层不应用“回答看起来不错”来评，而应注入可控故障，检查状态机和 Trace。

| 故障注入 | 指标 | 必须验证 |
|---|---|---|
| 执行前取消 | `cancel_effectiveness` | 工具不执行，节点状态与 interrupted 标记正确 |
| 慢工具 | `timeout_enforcement_rate` | 在时限内终止，错误可观测 |
| 短暂错误 | `retry_recovery_rate` | 在预算内恢复，尝试次数准确 |
| 上游节点失败 | `dependency_failure_containment` | 强依赖不运行，可选依赖按策略降级 |
| 执行中补充计划 | `replan_success_rate` | 新节点加入 DAG、完成且产生 replan 事件 |

工具结果状态可用 `tool_outcome_accuracy` 评估，链路时序用 `required_trace_order` 检查。建议另外统计并行加速比、重试放大系数、工具 P95/P99 延迟、超时后残留任务数和幂等冲突率；这些是后续压测指标，不应说成当前已全部量产。

### 当前记忆/Harness 测试资产

- [记忆与 Harness 数据集](../examples/evaluation/memory_harness_eval.jsonl)：14 条版本化用例，其中记忆 9 条、Harness 5 条；每条包含 capability、metric、threshold 和 oracle。
- [记忆与 Harness 组件测试](../tests/test_memory_harness_eval_dataset.py)：实际调用记忆、Query Rewriter 和 Graph Runtime 代码，用内存仓库/故障工具保持可复现，逐例执行阈值门禁。
- [记忆读写回放数据集](../examples/evaluation/memory_behavior_eval.jsonl)：6 条可进入通用评测服务的 baseline/fixed 用例，覆盖召回、偏好写入与更正、注入污染、敏感凭证和跨租户读取。
- [异步记忆写入器测试](../tests/test_async_memory_writer.py)：验证 flush 屏障、FIFO 排空、异常隔离以及 stop 后拒绝新任务。

这组测试证明当前代码在这些可控契约下的行为，不代表长期生产记忆的准确率，也不代表真实网络、模型抖动和高并发下的完整可靠性。图记忆与知识图谱现已把 `user_id` 放入 Neo4j 节点、关系、复合约束和查询条件；旧无租户字段的图数据会安全不可见，必须按租户重建投影。

当前实现是会话记忆、用户长期记忆、偏好记忆和图记忆，不包含跨用户共享的“公共全局记忆”。面试时可以把全局知识库与用户私有记忆分开讲，不能把尚未实现的共享记忆层说成已经上线。

## 8. 发布门禁怎么设

门禁分为三类：

1. **用例门禁**：每条用例的必需指标达到阈值。
2. **汇总门禁**：通过率、错误率、P95 延迟和分维度均值达标。评测服务的默认值为通过率不低于 80%、错误率不高于 5%、P95 单用例延迟不高于 5000 ms。
3. **硬门禁**：S0/S1 安全问题、跨租户泄漏、私密外泄、记忆污染和未授权高风险工具调用必须为 0，不能被平均分抵消。

版本发布还应满足：candidate 与 baseline 使用相同的数据集版本和用例 ID；无新增 S0/S1 Badcase；重点维度无显著回退；修复的 Badcase 在候选 Run 中真实通过。

## 9. 如何执行当前测试集

以下命令均在 `AGI-saber-python/final` 根目录执行。

### 9.1 一次跑完本轮新增测试

```powershell
$benchmarkTemp = "runtime/pytest-benchmark-$([guid]::NewGuid().ToString('N'))"
python -m pytest tests/test_benchmark_suite.py tests/test_benchmark_metrics.py tests/test_agent_framework_eval_dataset.py tests/test_rag_quality_eval_dataset.py tests/test_memory_harness_eval_dataset.py tests/test_async_memory_writer.py -q --basetemp $benchmarkTemp
```

这条命令检查通用评分器、Agent 回放数据、RAG 真实组件契约、记忆真实组件契约和 Harness 故障注入。本轮四份数据资产共 48 条用例（19 条 Agent、9 条 RAG、14 条记忆/Harness 组件和 6 条记忆读写回放），但 pytest 用例数会因参数化和组件契约检查而大于 48。

### 9.2 只跑某个领域

```powershell
# Agent
python -m pytest tests/test_agent_framework_eval_dataset.py tests/test_benchmark_metrics.py -q

# RAG
python -m pytest tests/test_rag_quality_eval_dataset.py -q

# 记忆 + Harness
python -m pytest tests/test_memory_harness_eval_dataset.py -q
```

### 9.3 生成离线 Replay 比较报告

```powershell
python scripts/run_benchmark_suite.py
```

默认会完成三件事：校验并执行 `examples/evaluation/*_eval.jsonl` 中带共同 replay profile 的数据集；计算 9 条 RAG Golden Query 的 Recall@3、MRR@3、nDCG@3、无答案和证据归因指标；执行 RAG、记忆与 Harness 的真实组件门禁。可显式只跑某个 EvalCase 数据集：

```powershell
python scripts/run_benchmark_suite.py --dataset examples/evaluation/agent_framework_capability_eval.jsonl
```

报告默认写入：

- `runtime/benchmark-suite/benchmark-results.json`：机器可读结果。
- `runtime/benchmark-suite/benchmark-report.md`：人工阅读报告。
- `runtime/benchmark-suite.db`：数据集版本、Run、Case Run、Badcase 和比较数据。

RAG 固件使用自己的语料/相关性 Schema，记忆/Harness 集使用真实组件执行器，因此脚本会把它们作为组件门禁执行，而不会给它们伪造 Agent Replay 输出。只想调试 EvaluationService 时可加 `--skip-component-tests`；正式门禁不要加。

## 10. 怎么跑真实 Local/HTTP 评测

项目评测 API 支持三种 Adapter：`replay`、`local` 和 `http`。数据集导入并得到 `dataset_version_id` 后，可通过 `POST /api/eval/runs` 创建并执行真实 Run。

Local 示例：

```json
{
  "dataset_version_id": "<version-id>",
  "name": "python-current-local",
  "adapter": {"type": "local", "version": "python-current"},
  "execute": true
}
```

HTTP 候选服务示例：

```json
{
  "dataset_version_id": "<version-id>",
  "name": "candidate-http",
  "adapter": {
    "type": "http",
    "endpoint": "http://127.0.0.1:9000/api/chat",
    "version": "candidate-2026-09-08",
    "timeout_seconds": 15
  },
  "execute": true
}
```

执行后查看 `/api/eval/runs/{run_id}/results`、`/summary`、`/gate`、`/badcases` 和逐用例 Trace，再通过 `POST /api/eval/runs/compare` 比较 baseline/candidate。更完整的 API 操作见 [Agent 质量评测平台使用指南](./Agent质量评测平台使用指南.md)。

真实模型有随机性，正式对比时还应：

- 锁定模型版本、Prompt、召回/重排参数、知识库快照、工具 Schema 和随机种子。
- 每个版本重复运行，报告均值、方差或置信区间，不要只挑最好的一次。
- 对同一批 case ID 做 paired comparison，然后按意图、工具、RAG、记忆、安全等分层查回归。
- 记录模型 Token、工具调用次数、P50/P95 延迟和错误率，防止“效果涨了，成本和延迟失控”。

## 11. 在线 A/B Test 如何接在 Benchmark 后面

离线 candidate 过门禁后，再小流量随机分流。常见指标包括：

- 主指标：任务完成率、一次解决率、人工转接率或具体业务转化率。
- 护栏指标：投诉/负反馈、安全事件、P95 延迟、超时率、单会话成本。
- 过程指标：检索点击/引用展开、重试次数、澄清率、工具成功率。

需要预先确定样本量、试验周期和最小可检测效应，按用户或会话稳定分流，防止同一用户在两个版本间来回切换。只有线上数据具备统计可靠性时，才能说“策略提升了真实业务指标”。

## 12. 面试可以怎么讲

### 30 秒版

> 我把 Agent 评测拆成了分层 Benchmark：底层先用确定性组件测试覆盖 RAG、记忆和 Harness，上层用版本化 Agent 数据集做 baseline/candidate 逐例回归。RAG 分别评 Recall@K、MRR、nDCG、无答案和证据归因；记忆重点评估保留、更正、去重、忘记、污染和租户隔离；Harness 通过超时、重试、取消、依赖失败和重规划的故障注入验证执行引擎。安全和跨租户泄漏用硬门禁，不用平均分抵消。

### 被追问“这是真实模型效果吗？”

> 现在仓库里的 baseline/fixed 结果是 replay oracle，它主要证明数据协议、评分器、Badcase 归因和发布门禁能够确定性复现。RAG、记忆和 Harness 测试会跑真实核心类，但外部模型和基础设施用可控假件。要宣称真实效果提升，还需要用 local/HTTP adapter 跑同版本数据集，再用线上 A/B 验证业务指标。

### 被追问“为什么不只看准确率？”

> Agent 的错误有明确层次：可能意图对但工具参数错，也可能检索命中但引用错，还可能结果对但 Trace 缺失。只看总准确率无法定位根因，而且隐私泄漏等低频高风险问题会被均值掩盖，所以需要分维度指标加硬门禁。

如果你没有亲自执行过真实模型对比或线上 A/B，面试时应说“已完成离线评测基建和确定性回归资产”，不要说成“在线效果提升了某个百分比”。

## 13. 常见面试追问

1. Recall@K、MRR 和 nDCG 各自解决什么问题？
2. 无答案样本为什么不能按 Recall@K 评分？
3. 如何区分“没召回”和“有召回但生成幻觉”？
4. 怎样防止数据泄漏和为 Benchmark 刷分？
5. 记忆何时写入，如何处理更正、过期和第三方信息？
6. 用户 A 的记忆为什么不会被用户 B 召回，你如何测它？
7. Harness 中必选依赖和可选依赖失败时有什么不同？
8. 重试为什么可能带来重复写入，如何通过幂等键避免？
9. 阈值怎么选，为什么安全指标不适合用均值？
10. 离线 Benchmark 通过了，为什么还要做线上 A/B？

面试时最有说服力的不是背指标名称，而是能从一个失败 Case 沿着 Trace 回答：失败发生在哪一层、哪个指标捕获它、修复后如何防回归，以及为什么这个门禁不能被绕过。
