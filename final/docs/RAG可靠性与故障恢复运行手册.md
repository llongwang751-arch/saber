# RAG 可靠性与故障恢复运行手册

> 事实基线：2026-09-02 当前 Python 工作区。本文只描述已经进入代码和测试的行为；质量收益仍需真实数据验证。

## 1. 当前故障链路

```text
用户问题
  → Query Rewrite
  → Embedding Circuit Breaker
  → Milvus / Elasticsearch / Neo4j 独立 Circuit Breaker
  → RRF
  → 远程 LLM Rerank Circuit Breaker
      → 失败：LocalOverlapReranker
      → 再失败：保持 RRF 顺序
  → 只有有效远程 Rerank 分数才执行无答案阈值
  → 父块补全与答案生成
  → Trace 脱敏持久化
```

重要边界：本地 `LocalOverlapReranker` 是确定性词项/字符重叠算法，只用于远程服务故障时保持基本排序能力。项目也提供懒加载 Cross-Encoder 接口，但默认不下载权重；两种本地分数都没有资格直接复用远程 Rerank 的 0.30 阈值。

## 2. 熔断状态

- `closed`：正常放行；成功清零失败计数，失败累计。
- `open`：连续失败达到阈值后，在冷却期内不再调用故障依赖，直接走降级路径。
- `half_open`：冷却结束只允许少量探测；成功恢复 `closed`，失败重新 `open`。

Rerank、Embedding、Milvus、Elasticsearch、Neo4j 分别维护状态。这样 ES 故障不会阻止 Milvus 继续服务，Rerank 故障也不会让召回停机。

配置入口是 [`config/config.yaml`](../config/config.yaml)：

```yaml
embedding:
  failure_threshold: 3
  cooldown_seconds: 30
  half_open_max_calls: 1

rag:
  retrieval_circuit:
    failure_threshold: 3
    cooldown_seconds: 30
    half_open_max_calls: 1
  rerank:
    failure_threshold: 3
    cooldown_seconds: 30
    half_open_max_calls: 1
    fallback_mode: local_overlap
```

## 3. 索引写入与恢复

RAG Chunk 的父子内容、版本和向量先写主存储；Elasticsearch 与 Milvus 是可重建投影：

- ES/Milvus 写入使用有界重试；
- Milvus 使用 upsert，重复执行不会生成重复主键；
- 最终失败会发布 `rag.index_failed` 事件；
- 管理员可从主存储重建 ES/Milvus 投影。

登录后调用：

```http
POST /api/rag/reindex
Authorization: Bearer <token>
```

返回字段：

- `source_rows`：主存储 Chunk 数；
- `elasticsearch_indexed`：成功重建数；
- `milvus_indexed`：成功重建数；
- `failures`：失败目标和记录；
- `ok`：本次是否全部成功。

SQLite 投影 Outbox 的状态与 dead 重放：

```http
GET  /api/rag/projections/status
POST /api/rag/projections/retry
Authorization: Bearer <token>
```

SQLite 真相存储会在保存、更新或删除 Chunk 的同一事务写入 ES/Milvus 投影 Outbox；Worker 在依赖可用时消费，失败指数退避，超过次数进入 dead，可通过状态与重放 API 恢复。删除也会投递到两种索引，避免依赖离线时遗留“幽灵块”；人工重放严格限制为当前租户。原生 PostgreSQL 路径目前仍依赖重建接口，没有同库 Outbox，因此整个系统依然不是跨库强一致。

### Neo4j 租户键迁移

知识实体使用 `(user_id, name)` 复合唯一键，图记忆使用 `(user_id, mem_id)`
复合唯一键；实体、关系、记忆节点和记忆边均携带 `user_id`。直接检索、
多跳路径、中心度统计和删除也都要求节点与关系属于当前用户。

升级时先创建复合约束，成功后才删除旧的全局 `entity_name` 唯一约束。
程序不会猜测旧节点的归属，也不会把缺少 `user_id` 的旧节点迁到
`default_user`；这些旧节点会保留在库中但对业务查询不可见（fail closed）。
上线前应先备份 Neo4j，再按用户从主存储重新摄入文档、从长期记忆真相源重建
图投影；核对新投影后，旧无 `user_id` 节点只能在变更单审批下人工清理，应用
启动过程不会自动删除或改写它们。

## 4. Trace 查询与隐私

每次回答生成 `trace_id`。持久化内容包括模式、RAG 路径、任务步骤、工具调用摘要、拒答或降级结果。保存前会递归处理：

- API Key、密码、Authorization、Token 等敏感字段；
- Bearer 凭证和 `sk-...` 样式密钥；
- 手机号、邮箱和身份证样式文本；
- 超长字符串、过深对象和过大列表。

Trace 按登录用户隔离：

```http
GET /api/traces?limit=50
GET /api/traces/{trace_id}
DELETE /api/traces/{trace_id}
POST /api/traces/purge
Authorization: Bearer <token>
```

当前仍需生产补强：字段级加密、保留期限、访问审计、跨服务 span 和 Prompt/引用的完整映射。

## 5. 无答案阈值校准

默认 `0.30` 不是质量结论。使用人工标注的“可回答/不可回答”验证集运行：

```powershell
python scripts/calibrate_no_answer_threshold.py examples/no_answer_labels.example.jsonl `
  --output docs/RAG无答案阈值校准报告.md `
  --json-output docs/RAG无答案阈值校准结果.json
```

每次更换 Rerank 模型、Prompt、知识库、切分策略或召回配置都要重新校准。错误回答与错误拒答的成本必须由业务风险确定，不能永远照抄示例中的 5:1。

## 6. 验证命令

```powershell
python -m alembic upgrade head
python -m pytest tests -q --basetemp .codex-pytest-complete-full
```

验收时应保存同一提交的原始测试输出、失败/跳过项和实际 Alembic head；静态文档不预填会随用例与迁移增长而过期的数字。

重点测试：

- [`test_dependency_circuits.py`](../tests/test_dependency_circuits.py)
- [`test_rerank_circuit_breaker.py`](../tests/test_rerank_circuit_breaker.py)
- [`test_local_reranker.py`](../tests/test_local_reranker.py)
- [`test_trace_persistence.py`](../tests/test_trace_persistence.py)
- [`test_rag_reindex.py`](../tests/test_rag_reindex.py)
- [`test_threshold_calibration.py`](../tests/test_threshold_calibration.py)

## 7. 面试回答

> 我把故障分成单次抖动、持续故障和索引不一致。单次抖动用有界重试；持续故障由每个外部依赖独立熔断；远程 Rerank 失败可切可选 Cross-Encoder、本地确定性重排，再回退 RRF。索引使用 upsert，SQLite 路径通过事务 Outbox 自动补偿，所有路径都能从主存储重建。每次请求生成脱敏、租户隔离且有保留期限的 Trace。当前代码行为是否满足既有合同，以同一提交的全量回归输出为准；阈值和排序质量仍要用真实标注集与故障压测证明。
