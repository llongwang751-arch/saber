# RAG 测试集与评测审计

## 结论

项目已有 RAG 组件级测试和通用 Agent 评测器，但此前没有一套同时包含查询、语料、相关性分级、有序召回结果、无答案标签和 Claim→Evidence 标注的可执行 RAG golden set。现在通用评测器已在 `rag_evidence_f1` 之外增加 Recall@K、MRR@K、nDCG@K 和无答案判断，专用 Golden Set 再补充语料、改写与 Claim→Evidence 标注。

本次新增：

- `tests/fixtures/rag_quality_eval_v1.jsonl`：9 个完全合成、可离线复现的 RAG 用例。
- `tests/test_rag_quality_eval_dataset.py`：数据契约、Recall@K、MRR、nDCG、无答案、证据归因、Query Rewrite、Rerank、重复块、租户隔离、检索降级和 RagLab 测试。

## 数据集标注协议

每个用例包含：

- `corpus[].chunk_id/document_id/tenant_id/text`：候选语料及租户归属。
- `oracle.answerable`：知识库能否回答，不能回答的样本不参与 Recall/MRR/nDCG，而进入拒答准确率统计。
- `oracle.relevance_grades`：Chunk 的 0～3 级相关性；Recall/MRR 将大于 0 视为相关，nDCG 使用完整分级。
- `oracle.acceptable_rewrites`：多轮指代问题的允许改写。
- `oracle.claims[].evidence_ids`：Claim 级证据归因，避免“引用了某篇文档”却不能支持具体结论。
- `runs.baseline/candidate.ranking`：有序检索结果，用于离线策略对比。
- `runs.*.abstained`：是否拒答。

## 指标口径

| 层级 | 指标 | 解释 |
|---|---|---|
| 召回 | Recall@K | 标注相关 Chunk 中有多少进入 Top K |
| 首命中 | MRR@K | 第一个相关 Chunk 出现得有多靠前 |
| 排序 | nDCG@K | 高相关 Chunk 是否排在低相关 Chunk 前面 |
| 无答案 | Abstention accuracy | 无答案时拒答、有答案时作答是否正确 |
| 生成 | Claim coverage | 必须回答的事实是否都覆盖 |
| 归因 | Evidence precision | 每个 Claim 引用的 Chunk 是否真的属于其支持证据 |
| 安全 | Tenant leakage rate | 返回结果中是否出现其他租户 Chunk，目标必须为 0 |
| 稳定性 | Degraded-path success | 向量路故障后关键词/本地路能否继续命中 |

建议离线发布门禁不要只看平均值。至少同时要求：Recall@5、MRR@5、nDCG@5 不低于基线；无答案准确率不回退；租户泄漏为 0；P95 延迟和失败降级成功率达标。

## 当前实现审计

已经能直接测到的能力：

- `HybridStore` 的多查询检索、RRF、远程/本地 Rerank、结构化 Trace 和路径降级。
- `Engine` 的父块回填、近重复证据去重、Rerank 阈值拒答和无答案 Trace。
- `LLMRewriter` 的历史感知、自包含改写与失败回退。
- `LocalRagChunkRepo` 的 SQLite 持久化和 `user_id` 隔离。
- `RagLab` 的切分、Embedding、向量召回、增强 Prompt、生成五阶段，以及哈希向量降级。

通用评测 Schema 当前已经支持 `Expected.evidence_relevance`、`retrieval_k`、三项排序阈值、`answerable`，并按 `AgentOutput.evidence_ids` 的顺序计算 Recall@K/MRR@K/nDCG@K。尚未统一的字段是：

- 可接受 Query Rewrite 列表和 Claim→Evidence 映射仍在 RAG 专用 Schema 中。
- `AgentOutput.evidence_ids` 能表达顺序，但每项尚未携带原始 score、retrieval source、document version，仍不足以定位全部索引版本漂移。
- RagLab 的 Chunk ID 是单次请求内的整数，未携带稳定的 `document_id/version_id/chunk_id`，目前适合教学诊断，不适合直接沉淀成长期线上评测记录。

## 发现的生产风险

本轮测试最初发现生产仓储没有在 PG、Milvus、Elasticsearch 和 Neo4j 图检索全链路透传 `user_id`。现已补上 PG 复合唯一键与查询条件、ES keyword 字段与 term filter、Milvus scalar field 与过滤表达式；Neo4j 的实体、关系和图记忆也使用租户复合键，并在写入、直接/多跳检索、中心度与删除路径限定租户。`HybridStore` 的摄入、搜索、回填、删除和重建路径会透传租户；对应预期失败已转成普通回归门禁。

旧 Milvus 集合没有 `user_id` 字段时必须从 PG 真相源重建，代码会告警并拒绝退回无过滤查询。旧 Neo4j 节点没有 `user_id` 时同样不会被业务查询读取，需要按租户从主存储重建图投影。租户泄漏率仍是 S0 门禁，不能因为平均检索分数较高而放行。

## 分层评测方法

1. PR 层：纯函数与组件契约测试，不依赖网络，覆盖 Rewrite、Fusion、Rerank、Dedup、No-answer、Trace。
2. 每日离线层：固定知识库版本与 Embedding/Rerank 版本，跑 golden set，输出分场景指标及 bootstrap 置信区间。
3. 集成层：使用真实 PG、Milvus、ES、Neo4j，注入单路超时/错误，检查索引一致性、熔断、降级和租户隔离。
4. 线上层：记录匿名化 query、检索 rank/score/source、引用关系、时延、Token 和反馈；按策略版本进行 A/B 分桶。
5. Badcase 闭环：失败样本按“改写、召回、排序、上下文组装、生成、归因、拒答、权限”归因，加入版本化回归集后再发布。

执行命令：

```powershell
python -m pytest tests/test_rag_quality_eval_dataset.py -q
```
