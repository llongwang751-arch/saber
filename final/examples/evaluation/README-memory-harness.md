# 记忆系统与 Agent Harness 合成评测集

`memory_harness_eval.jsonl` 是一组不依赖在线模型和外部基础设施的确定性发布门禁。
每行都符合项目现有的 `EvalCase` Schema，记忆与调度器专用标签放在
`metadata.oracle`，因此既能被现有数据集导入器读取，也能由独立 pytest 执行。

## 覆盖范围

| 组件 | 指标 | 目标 |
|---|---|---:|
| 多轮记忆 | `coreference_entity_recall` | 1.00 |
| 用户画像 | `preference_retention_accuracy` | 1.00 |
| 偏好更正 | `preference_correction_accuracy` | 1.00 |
| 记忆安全 | `memory_contamination_rate` | 0.00 |
| 多租户 | `cross_tenant_leak_rate` | 0.00（S0 门禁） |
| 事实更正 | `stale_fact_exposure_rate` | 0.00 |
| 遗忘机制 | `expired_memory_removal_f1` | 1.00 |
| 记忆合并 | `memory_dedup_f1` | 1.00 |
| 图记忆 | `graph_expansion_recall` | 1.00 |
| 取消 | `cancel_effectiveness` | 1.00 |
| 超时 | `timeout_enforcement_rate` | 1.00 |
| 重试 | `retry_recovery_rate` | 1.00 |
| 依赖失败 | `dependency_failure_containment` | 1.00 |
| 重规划 | `replan_success_rate` | 1.00 |

## 运行方式

```bash
python -m pytest tests/test_memory_harness_eval_dataset.py -q
```

测试文件会先执行数据 Schema、用例 ID、能力覆盖和阈值完整性校验，再把每条数据分发到实际的
`Preference`、`LongTerm`、`MemoryManager`、`LLMRewriter`、`TaskGraph` 和
`GraphRuntime`。失败信息会带上 case ID、指标、得分、阈值与诊断详情。

## 分层评测方法

这组数据属于 L0/L1 确定性测试：固定输入、固定 Oracle、无模型随机性，适合每次提交必跑。
它不能替代更高层评测：

1. L2 离线模型评测：冻结模型、Prompt、Embedding 和索引快照，重复运行多轮对话及检索集，报告均值、置信区间和分桶结果。
2. L3 基础设施集成：连接 PostgreSQL、Milvus、Neo4j，注入断网、慢响应、重复消息和乱序 Outbox，验证最终一致性与恢复时间。
3. L4 线上实验：按用户稳定分桶做 A/B，监控任务成功率、人工采纳率、P95 延迟、Token 成本、记忆污染投诉率，并配置自动回滚。

建议发布门禁规则：任何跨租户泄漏、敏感信息写入或必需依赖越权继续执行都直接阻断发布；
其余指标采用相对基线门禁，候选版本不得显著回退。
