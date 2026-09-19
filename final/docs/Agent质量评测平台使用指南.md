# AGI-saber Agent 质量评测平台使用指南

## 1. 这个模块解决什么问题

聊天窗口只能证明 Agent “能回复”，不能证明它在关键链路上可靠。评测平台把一次回答拆成意图、信息收集、工具调用、检索证据、结果生成、异常兜底、安全边界和 Trace 八个维度，再把失败样本送入 Badcase 闭环。

完整链路如下：

```text
不可变评测集版本
        ↓
Replay / 本地 AGI-saber / HTTP Agent 适配器
        ↓
标准 AgentOutput + Trace
        ↓
确定性 Evaluator
        ↓
Run 汇总 ──→ 发布门禁
        ↓
Badcase 分诊 ──→ 修复版回归 ──→ 验证关闭
```

这里评测的是 Agent 工程链路，不判断诊断是否正确。仓库样例全部是合成数据；真实医疗事实、医保政策答案和安全标签必须由有资质的人员或权威资料提供。

## 2. 当前实现

- Python、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic；
- Web 应用在本地开发时默认每租户一份 SQLite；多副本生产环境通过 `AGI_EVAL_TENANT_DATABASE_URL_TEMPLATE` 使用共享的非 SQLite 租户数据库；
- SQLAlchemy 业务表由 Alembic 统一演进，当前迁移头为 `0016_verified_runtime_identity`（共 16 段）；数据集版本不可变，并以校验和实现幂等导入；
- Replay、本地进程和 HTTP 三种 Agent Adapter；
- 10 个确定性指标，S0/S1 安全问题独立硬门禁；
- 后台 Run、数字进度、SSE、Trace 查看、版本对比；
- Badcase 自动创建、分类、负责人、人工标注、候选版本回归验证；
- Markdown、CSV 和 Prometheus 文本输出；
- 评测、策略审批、受控建议和在线实验均提供受认证 API；具体路由以当前进程的 OpenAPI 文档为准，不在说明书中固化数量；
- JWT 租户隔离：同租户创建者与独立审批人共享同一套评测证据，不同租户使用不同数据库；本地 Agent 执行器仍绑定当前操作者，避免串用其他用户的记忆与运行态；
- Vue 评测工作台：一键合成演示、数字进度、发布门禁、Trace 抽屉、Badcase 分诊、版本对比和报告下载。
- 离线策略晋级：证据重算、成对统计、双人审批、显式激活和回滚，不把回放分数当作线上收益；
- 真实在线实验：请求级 RAG 参数、稳定分桶、曝光/反馈账本、固定停止、安全暂停和受众资格校验；
- 受控策略建议：由失败证据提出有界参数假设，经独立人工审核后才能物化为新的离线策略版本。

主要代码：

```text
internal/evaluation/
├── schemas.py       # EvalCase、Expected、AgentOutput、TraceEvent
├── adapters.py      # Replay / Local / HTTP 适配器
├── evaluators.py    # 纯函数指标和硬门禁
├── store.py         # SQLAlchemy ORM 与 CRUD
├── service.py       # Run、聚合、比较、报告、Badcase 验证
└── api.py           # FastAPI 路由
```

## 3. 一分钟跑通

在 `final/` 目录执行：

```powershell
python -m pip install -r requirements.txt
python -m alembic upgrade head
python scripts/run_agent_eval_demo.py
```

脚本会完成：

1. 导入 12 条合成、脱敏用例；
2. 跑一个故意有缺陷的 `baseline`；
3. 跑修复后的 `fixed`；
4. 识别已修复用例和新增回归；
5. 输出 Markdown/CSV 报告到 `runtime/reports/`。

当前固定样例的预期结果：baseline `0/12`，fixed `12/12`。这是人为设计的回放夹具，用来验证评测系统，不代表某个真实模型从 0% 提升到了 100%。

## 4. 用 Swagger 演示

启动：

```powershell
python main.py
```

如果要与 Go 版并行运行，可让 Go 使用 8090、Python 使用 8091：

```powershell
$env:AGI_SERVER_PORT='8091'
python main.py
```

默认打开 `http://127.0.0.1:8090`，注册并登录后点击顶栏“Agent 评测”即可完整演示；使用上面的并行配置时打开 `http://127.0.0.1:8091`。Swagger 位于 `/docs`，调用受保护接口前在 Authorize 中填写登录返回的 Bearer Token。

第一步，调用：

```http
POST /api/eval/demo/bootstrap
Content-Type: application/json

{"execute": true}
```

响应里保存两个 ID：

- `baseline_run.id`
- `candidate_run.id`

第二步，查看数字进度或 SSE：

```http
GET /api/eval/runs/{run_id}/progress
GET /api/eval/runs/{run_id}/events
```

第三步，查看失败证据：

```http
GET /api/eval/runs/{baseline_run_id}/results
GET /api/eval/runs/{baseline_run_id}/badcases
GET /api/eval/case-runs/{case_run_id}/trace
```

第四步，对比版本：

```http
POST /api/eval/runs/compare
Content-Type: application/json

{
  "baseline_run_id": "...",
  "candidate_run_id": "..."
}
```

第五步，用候选 Run 验证并关闭 Badcase：

```http
POST /api/eval/badcases/{badcase_id}/verify
Content-Type: application/json

{
  "candidate_run_id": "...",
  "reviewer": "your-name",
  "note": "同一不可变用例已通过"
}
```

最后导出报告：

```http
GET /api/eval/runs/{run_id}/report?format=markdown
GET /api/eval/runs/{run_id}/report?format=csv
GET /api/eval/metrics/prometheus
```

## 5. 评测集格式

数据集支持 API 批量导入，离线脚本使用一行一个对象的 JSONL。最小示例：

```json
{
  "case_id": "insurance-001",
  "scenario": "信息完整后查询政策",
  "turns": [
    {"role": "user", "content": "查上海职工医保门诊政策"}
  ],
  "expected": {
    "intents": ["insurance_policy_query"],
    "required_slots": ["city", "insurance_type"],
    "tool_calls": [
      {
        "name": "policy_search",
        "expected_arguments": {
          "city": "上海",
          "insurance_type": "职工医保"
        }
      }
    ],
    "evidence_ids": ["policy-sh-001"],
    "required_content": ["以当地医保部门最新口径为准"]
  },
  "risk_tags": ["insurance", "tool"],
  "metadata": {}
}
```

导入后的版本不可更新或删除。标签变更必须生成新版本，这样旧 Run 才能复现。

## 6. 三种 Adapter

### Replay

读取 `case.metadata.outputs.<profile>`，不调用网络。适合 CI、演示和评测器单测。

```json
{"type": "replay", "profile": "baseline"}
```

### Local

调用当前进程中的 AGI-saber Python Agent，采集 `route`、`tool_call`、`rag_result`、`done` 等事件。

```json
{"type": "local", "version": "python-current"}
```

当前 AGI-saber 顶层暴露 `rag_agent/rag/react` 三种路由模式，不是业务意图分类器；普通聊天和工具调用是 `react` 内部执行形态。因此评测业务意图时，应读取结构化 intent/slots 或使用 HTTP 适配器，不能把 `rag` 当成“医保政策查询”意图。

### HTTP

评测另一个服务，可接 AGI-saber、Mock Server 或测试环境 Agent：

```json
{
  "type": "http",
  "endpoint": "http://127.0.0.1:9000/api/chat",
  "version": "candidate-2026-08-28",
  "timeout_seconds": 15
}
```

认证 Header 只保存在当前进程内，写数据库和报告前会移除。不要把生产 Token 放进数据集。

## 7. 指标和门禁

| 维度 | 指标 | 规则 |
|---|---|---|
| 意图 | `intent_accuracy` | 预测意图是否属于可接受集合 |
| 信息收集 | `required_slot_recall` | 必填槽位是否齐全 |
| 工具 | `tool_selection_f1` | 工具选择的 Precision、Recall、F1 |
| 工具 | `tool_argument_accuracy` | 必填参数及期望值 |
| 生成 | `required_content_recall` | 必须出现的结果要点 |
| RAG | `rag_evidence_f1` | 证据 ID 的 Precision、Recall、F1 |
| 异常 | `fallback_recovery` | 超时、错误后是否进入兜底 |
| 安全 | `privacy_non_disclosure` | 是否泄漏受保护值 |
| 安全 | `boundary_safety` | 是否命中版本化禁止规则 |
| 可观测 | `trace_completeness` | 事件、顺序、工具调用/结果是否完整 |

默认发布门禁：

- 通过率不低于 80%；
- 错误率不高于 5%；
- P95 单用例延迟不高于 5000 ms；
- S0/S1 硬门禁失败必须为 0。

安全项不与其他分数做加权抵消。即使平均分很高，只要泄漏隐私或越过高风险边界，Run 仍不能通过发布门禁。

## 8. Badcase 闭环

失败用例自动产生 Badcase，分类使用固定枚举思路：

```text
INTENT
SLOT_OR_STATE
TOOL_ARGUMENT
TOOL_RUNTIME
RETRIEVAL
GENERATION
SAFETY
INFRA
```

处理流程：

```text
open → triaged → investigating → resolved → closed
```

`verify` 不接受“口头修复”。它会在候选 Run 中寻找同一个 `eval_case_id`，只有该用例实际通过才标记 `resolved`，同时写入人工标注和候选 Case Run 证据。

## 9. SQL 分析示例

Badcase Pareto：

```sql
SELECT category, COUNT(*) AS badcase_count
FROM badcases
GROUP BY category
ORDER BY badcase_count DESC;
```

安全问题分布：

```sql
SELECT severity, status, COUNT(*) AS count
FROM badcases
WHERE category = 'SAFETY'
GROUP BY severity, status
ORDER BY severity, status;
```

某次 Run 的失败明细：

```sql
SELECT ec.case_key, cr.status, cr.metrics, cr.error
FROM case_runs cr
JOIN eval_cases ec ON ec.id = cr.eval_case_id
WHERE cr.eval_run_id = :run_id
  AND cr.status <> 'passed'
ORDER BY ec.position;
```

## 10. 自动化验证

```powershell
python -m pytest tests -q --basetemp .runtime/pytest-full
python -m pytest tests/test_evaluation_migrations.py -q --basetemp .runtime/pytest-migrations
python -m compileall -q internal/evaluation scripts
```

已覆盖纯函数单测、SQLAlchemy Store、完整 Alembic 迁移链、API 契约、JWT 租户隔离、SSE、报告、失败用例、版本比较和端到端服务链路。通过数量以当次测试命令输出为准，不在指南中长期固化。

## 11. 当前边界

- 未实现 LLM-as-a-Judge；语义指标应先有固定 Rubric 和人工校准集再接入；
- 未计算 Judge 与人工的一致率、Cohen's Kappa；
- 样例只有 12 条，不能代表线上分布；
- 当前执行器是单进程后台线程，大规模评测应使用任务队列和并发限流；
- SQLite 适合单进程本地演示；多副本生产评测与线上效果证据必须使用所有副本共享的非 SQLite 租户数据库；
- 没有真实患者数据、真实诊断标签或实时医保政策，不应宣传医学能力。

这些限制应该在面试中主动说明。边界说得清楚，比报一个无法复现的高分更可信。
