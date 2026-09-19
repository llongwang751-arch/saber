# 腾讯医疗 AI Agent 质量评测实习生：项目讲解与面试准备

## 1. 先确定项目定位

项目不要讲成“医疗诊断 Agent”。准确说法是：

> 我在 AGI-saber 的 Python/FastAPI 分支中实现了一套 Agent 离线评测与 Badcase 闭环。它把意图、信息收集、工具调用、RAG 证据、异常兜底、安全边界和 Trace 变成可版本化、可回归的测试对象。样例使用合成数据，医学与政策事实必须由专家提供。

这个定位与岗位更贴近，也避免被追问“你凭什么判断诊断正确”。

## 2. JD 对应关系

| JD 能力 | 项目证据 | 面试演示 |
|---|---|---|
| 评测集设计与执行 | `EvalCase`、`Expected`、不可变 Dataset Version、checksum | 导入 12 条 JSONL，展示版本号 |
| 意图、多轮、信息收集 | 意图集合、必填槽位、多轮 turns、状态更正用例 | 打开 `multiturn_slot_correction` |
| 工具调用与异常兜底 | 工具名/参数/顺序、timeout/error、fallback 指标 | 打开 `tool_timeout_fallback` Trace |
| RAG 与结果生成 | evidence F1、必要内容召回、引用证据 ID | 打开 `rag_prompt_injection` |
| Trace 标注 | 标准事件模型、sequence、tool_call/tool_result 配对 | 调 `/case-runs/{id}/trace` |
| 自动化能力 | Python、FastAPI、SQLAlchemy、Alembic、pytest、SSE | 展示测试与 Swagger |
| Badcase 闭环 | 自动归因、severity、owner、annotation、verify | 用 candidate Run 验证关闭 |
| 医疗安全/隐私 | S0/S1 独立硬门禁、隐私不回显、越界禁止规则 | 展示 emergency/privacy 用例 |
| 数据分析 | Run 汇总、P50/P95、分类 Pareto、CSV、Prometheus | 展示报告和 SQL |

## 3. 三分钟项目讲法

### 背景

AGI-saber 原本更关注 Agent 能力本身，例如 RAG、工具调用、记忆和任务图。但“能运行”不等于“质量可控”，尤其在医保问答、服务匹配和报告解释场景，错误工具参数、缺信息抢答、无依据结论和安全越界都需要单独测。

### 任务

我把目标定义为一个可复现的离线评测闭环：同一份不可变用例能运行不同 Agent 版本；每个失败有 Trace 证据；修复后必须在候选 Run 中通过同一用例才能关闭 Badcase。

### 方案

1. 用 Pydantic 固化 `EvalCase → AgentOutput → EvaluationReport` 契约；
2. 用 Adapter 隔离 Replay、本地 Agent 和 HTTP Agent；
3. 可确定的指标全部用规则计算，避免让 LLM Judge 评判工具名、参数、顺序这类客观事实；
4. 用 SQLAlchemy 保存评测集、Run、Case Run、Badcase 和人工标注；
5. 数据集版本不可变，配置、结果、Trace 与规则证据一起落库；
6. S0/S1 安全问题走独立硬门禁，不能被平均分抵消；
7. FastAPI 提供后台执行、数字进度、SSE、对比和报告导出。

### 结果

当前仓库可验证事实：

- 业务、评测、策略和实验路由均已接入 FastAPI，具体数量以当次 OpenAPI 为准；
- 当前 Alembic 迁移头为 `0016_verified_runtime_identity`，共 16 段；
- 评测业务表由 Alembic 统一版本化，不在面试材料中固化易过期的表数量；
- 10 个确定性指标；
- 12 条合成演示用例；
- 全量 pytest 以仓库当前实跑结果为准；执行命令是 `python -m pytest tests -q`。

演示中的 baseline `0/12`、fixed `12/12` 是故意设计的 Replay Fixture，只证明评测闭环能识别问题与回归，不能说成真实模型效果提升。

## 4. 为什么规则评测优先

面试官可能问：“大模型输出这么灵活，为什么不用 LLM-as-a-Judge？”

回答思路：

- 工具名、必填参数、JSON Schema、调用顺序、证据 ID、超时和 Trace 完整性都有确定答案，用 LLM Judge 反而引入随机性和成本；
- 语义完整性、表达清晰度等规则难覆盖的部分，才适合固定 Rubric 的 Judge；
- Judge 上线前需要人工校准集，观察准确率、分歧样本和 Cohen's Kappa；
- 高风险安全失败必须人工复核，不能让 Judge 自己证明自己正确。

一句话：

> 能确定性判断的绝不交给模型；必须用模型判断的，要版本化 Rubric 并用人工样本校准。

## 5. 为什么评测集必须版本化

如果标签被直接修改，旧 Run 的分数会失去解释力。项目采用：

- Dataset 是逻辑集合；
- Dataset Version 保存一批不可变 Case；
- 内容做 canonical JSON checksum；
- 相同内容重复导入幂等返回原版本；
- 标签修改必须创建新版本；
- Run 固定引用一个 Dataset Version。

这样才能回答：“模型变好了，还是标准答案变了？”

## 6. 多轮与工具调用怎么测

多轮不只测最终答案。需要检查：

- 必要信息有没有收齐；
- 用户纠正城市后，旧状态有没有被覆盖；
- 信息不完整时是否先追问；
- 是否选对工具；
- 参数值是否来自最新对话状态；
- 工具失败后有没有伪造成功结果；
- 有副作用的工具有没有重复调用。

项目中的 `multiturn_slot_correction` 会把城市从北京更正为上海，期望工具参数必须是上海。`tool_timeout_fallback` 则要求 timeout Trace 后出现 fallback，且禁止编造“已审核通过”。

## 7. 安全为什么不能做普通加权分

假设一个用例在语言流畅、意图和工具上都得高分，但泄漏了身份证号。如果安全分只占 10%，总分仍可能及格，这不符合发布风险。

项目处理：

- 隐私泄漏默认 S0；
- 严重越界规则可标 S0/S1；
- 命中后 `hard_gate_failed=true`；
- Run 发布门禁直接失败；
- 证据只记录“泄漏了几个受保护值”，不把原值再次写进日志。

## 8. Trace 怎么设计

标准事件包括：

```text
user_message
intent_predicted
slot_extracted
retrieval
llm_call
tool_call
tool_result
guardrail
fallback
final_response
error
```

每个事件有 sequence、name、payload、status、trace/span ID、耗时。当前确定性检查会验证：

- 必要事件是否存在；
- sequence 是否有序且唯一；
- 每个期望工具是否先有 `tool_call`，之后有 `tool_result`；
- RAG、兜底和安全场景是否留下对应证据。

面试时要强调：Trace 不是“把思维链存下来”，而是存可观测的业务事件、工具输入输出摘要和错误。不要记录模型隐式推理，也不要落明文隐私。

## 9. Badcase 怎么闭环

失败后自动按首要失败维度分类：

```text
INTENT / SLOT_OR_STATE / TOOL_ARGUMENT / TOOL_RUNTIME
RETRIEVAL / GENERATION / SAFETY / INFRA
```

状态：

```text
open → triaged → investigating → resolved → closed
```

项目的关键点是 `verify`：必须提供候选 Run，系统找到同一个不可变 Case 的候选结果；只有候选结果为 passed 才能 resolved，并记录 reviewer、candidate run、case run 和人工标注。

如果面试官追问如何防止“修这个坏那个”，回答：做 baseline/candidate 全量 compare，列出 fixed、regressions、unchanged failures；新增 regression 时发布门禁不通过。

## 10. SQL 可能怎么问

### 找 Badcase Top N

```sql
SELECT category, COUNT(*) AS cnt
FROM badcases
GROUP BY category
ORDER BY cnt DESC
LIMIT 5;
```

### 找候选版本新增失败

思路是以 `eval_case_id` 对齐两个 Run 的 `case_runs`：baseline 为 passed、candidate 非 passed，即为 regression。

```sql
SELECT b.eval_case_id
FROM case_runs b
JOIN case_runs c ON c.eval_case_id = b.eval_case_id
WHERE b.eval_run_id = :baseline_run_id
  AND c.eval_run_id = :candidate_run_id
  AND b.status = 'passed'
  AND c.status <> 'passed';
```

### 为什么动态指标存 JSON

指标和 Trace 结构迭代快，全部拆列会频繁迁移；生命周期、外键、状态等高频过滤字段保留为关系列，动态结果放 JSON。生产 PostgreSQL 可用 JSONB 和 GIN；本地 SQLite 以可运行和测试为主。

## 11. Python/FastAPI 可能怎么问

### 为什么用同步 SQLAlchemy

当前评测 Run 在线程后台执行，SQLite 本地演示采用同步 SQLAlchemy，逻辑直观且测试稳定。HTTP 接口是同步依赖，FastAPI 会放到线程池。若迁移到大规模 PostgreSQL 并发执行，可以切 AsyncSession，执行层用 Celery/Redis 或任务平台，API 只负责调度。

### SSE 与轮询怎么选

- `/progress` 适合轮询和断线重连；
- `/events` 只在状态变化时发送，适合实时进度；
- 结果仍以数据库为准，SSE 不是事实存储；
- 客户端断开不会取消 Run，取消要显式调用 `/cancel`。

### 为什么 Adapter

评测逻辑不应该绑定某个 Agent 实现。Adapter 把不同响应统一成 AgentOutput，评测器只依赖稳定契约。这样可对比本地分支、测试环境 HTTP 服务和固定 Replay。

## 12. 现场演示顺序

1. 在终端运行 `python scripts/run_agent_eval_demo.py`；
2. 展示 baseline 与 fixed 的真实输出 JSON；
3. 打开一份 baseline Markdown 报告；
4. 启动 FastAPI，打开 `/docs`；
5. 调 `/api/eval/demo/bootstrap`；
6. 打开 timeout 或 privacy 的 Trace；
7. 查看 Badcase 分类和 hard gate；
8. 调 `/runs/compare` 展示 fixed/regressions；
9. 用 `/badcases/{id}/verify` 完成闭环；
10. 展示同一提交的 pytest 实跑结果和完整 Alembic 迁移链。

不要先花时间展示聊天页面。这个岗位关心的是你能不能把 Agent 拆成可测链路并定位问题。

## 13. 不能说过头的地方

- 不说“做了医疗诊断准确率评测”；
- 不说“baseline 到 fixed 的 100% 提升是真实模型提升”；
- 不说“LLM Judge 已上线”，当前没有；
- 不说“支持生产级分布式评测”，当前执行器是单进程线程；
- 不使用旧简历里无法由当前运行复现的吞吐/P95 数字；
- 不声称一个人完成了仓库全部模块，只讲自己能解释、能运行、能修改的部分。

## 14. 下一步最值得补什么

优先级建议：

1. 把 12 条样例扩展到 50～100 条，并做标签评审流程；
2. 接一个 Mock HTTP Agent，做 timeout、500、畸形 JSON 故障注入；
3. 增加 Macro-F1、混淆矩阵和按风险/场景切片；
4. 加人工评审页面和双人标注一致率；
5. 引入 LLM Judge，只评语义维度并校准 Cohen's Kappa；
6. PostgreSQL + 任务队列并发执行，补成本与 Token 指标；
7. 接 CI 回归门禁，在候选版本新增失败时阻断发布。

如果面试时间很近，先熟练讲清现有闭环和边界，不要为了堆技术临时接一个无法解释的模型。
