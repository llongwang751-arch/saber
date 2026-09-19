# RAG Benchmark v1

这是一套不依赖外部模型或搜索服务的 RAG 离线评测样例。Golden set 位于：

```text
tests/fixtures/rag_quality_eval_v1.jsonl
```

它包含 9 类必须分开观察的能力：直接召回、分级相关性、无答案、历史问题改写、Rerank、重复块、Claim 级证据归因、跨租户隔离和单路故障降级。

## 运行

在 `AGI-saber-python/final` 目录执行：

```powershell
python -m pytest tests/test_rag_quality_eval_dataset.py -q
# 或生成包含 Agent、RAG、记忆与 Harness 的统一报告
python scripts/run_benchmark_suite.py
```

## 每条样本的关键字段

- `corpus`：本样本固定的候选 Chunk；`tenant_id` 是权限边界，不是普通 metadata。
- `oracle.answerable`：知识库是否具备答案。无答案样本用拒答准确率评分，不把空相关集硬算成 Recall=1。
- `oracle.relevance_grades`：0～3 级相关性标签，为 Recall@K、MRR 和 nDCG 提供 oracle。
- `oracle.claims[].evidence_ids`：每条答案事实允许引用哪些 Chunk。
- `runs.baseline/candidate.ranking`：两个策略版本的有序召回结果。
- `runs.*.abstained`：策略是否拒答。

## 指标解释

- Recall@K 看相关内容有没有被捞上来。
- MRR@K 看第一条可用证据出现得是否足够靠前。
- nDCG@K 看强相关证据是否排在弱相关和噪声之前。
- Claim coverage 看必须回答的事实是否完整。
- Evidence precision 看每个事实的引用是否真的能支持它。
- Abstention accuracy 单独评估无答案拒答，不能混进普通检索指标。
- Tenant leakage rate 必须为 0，且应作为发布硬门禁。

当前 fixture 是小规模、完全合成的契约集，适合 PR 回归和面试演示，不应把它的绝对分数当作线上效果。生产评测还需扩展真实匿名化 Query、时间切片数据、难负例、冲突文档、文档版本与人工双标一致性，并固定 Embedding、索引和 Rerank 版本后比较。

当前合成结果只用于验证评分器和门禁逻辑：baseline 的 Recall@3 / MRR@3 / nDCG@3 分别为 `0.875 / 0.6875 / 0.7210`，candidate 为 `1.000 / 1.000 / 1.000`。这里的提升是 fixture 预设出来的可验证回归信号，不是对真实线上效果的宣称。
