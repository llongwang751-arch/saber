# AGI-saber 对话问题审计与源码优化报告

> **当前架构说明（2026-09-14）**：Go `845e8f7` 和 Python 当前实现都保留 `rag_agent / rag / react` 三路路由。报告类请求会执行 `research → writer → review → doc` 固定 DAG，`doc_agent` 负责写入文档库并回填 RAG。

> 审计范围：本轮长对话中的项目教学、Python 当前实现、Go 参考实现，以及本次完成的代码修复。  
> 核心原则：Go 版是设计参考，不是绝对正确答案；所有面试表述必须区分“已实现、已验证、设计中”。

---

## 1. 这轮对话真正暴露了什么问题

问题不只是“你对项目不熟”，教学本身也有明显缺陷。下面把责任拆开，避免后续继续用错误方式学习。

### 1.1 过早进入追问和评分

在你尚未建立整体模型时，直接追问 Replan、熔断、降级、评测和源码细节，并频繁给分，结果更像压力面试而不是教学。你只能短期复述术语，无法解释模块之间为何这样连接。

优化方式：以后严格按以下顺序：

1. 先用一个真实请求走完整链路；
2. 再解释每个组件的职责边界；
3. 接着看对应源码；
4. 然后比较替代方案和取舍；
5. 最后才做面试追问。

### 1.2 讲得太快，一次塞入太多名词

Router、Planner、TaskGraph、GraphRuntime、Harness、Trace、ReAct 和 Replan 曾在很短时间内同时出现，导致你把组件、算法和 Agent 角色混为一谈。

优化方式：每轮只引入一个新概念，并固定使用同一套比喻：

- Router：分诊台，只选路线；
- Planner：计划员，生成结构化任务卡；
- TaskGraph：任务和依赖的数据，不执行；
- GraphRuntime：图级调度器，真正安排节点执行；
- Harness：单步执行保护策略；当前 Python 版仍主要内嵌在 Runtime；
- Tool/SubAgent：真正干活的执行单元；
- Trace：后台过程证据，不是模型隐秘思维。

### 1.3 多次把“设计目标”说成“当前实现”

对话中先后把双 Rerank、0.30 无答案阈值、0.85 父块语义去重、SQLite FTS5 + 向量回落、完整五层记忆等说得像已落地，后来查看源码后又纠正。这会直接让面试回答失真。

优化方式：以后每项能力统一标记：

- `Implemented`：代码主链已经接通；
- `Verified`：有自动化测试、评测报告或运行证据；
- `Partial`：有接口或局部实现，但未闭环；
- `Designed`：只在文档、简历或方案中；
- `Missing`：当前没有。

### 1.4 对源码结论缺少即时证据

对话中多次出现“我看过了”“我确认了”，但没有立刻给出文件、函数和行为证据。用户无法区分真实核验与口头判断。

优化方式：源码结论至少包含三部分：

1. 文件和函数；
2. 当前行为；
3. 测试或可复现步骤。

### 1.5 术语纠正不够稳定

语音识别把 RAG 识别成 IG/RNG，把 Rerank 听成 RRK，把阈值说成“域值/预值”。回答有时顺着错误词继续讲，增加了混乱。

优化方式：遇到关键术语先做一次短纠正，再继续内容：

> 这里是 RAG，不是 IG；RRF 是融合算法，Rerank 是精排，二者不是一个东西。

### 1.6 没有充分响应“听我讲完”和“放慢”

语音中你多次要求先讲完，但助手仍穿插回应。教学节奏也常在你刚说“差不多”时马上推进下一个模块。

优化方式：语音教学改为：

- 用户开始复述后不插话；
- 用户说“讲完了”再反馈；
- 一次反馈只纠正最多三个关键点；
- 用户说“没懂”时退回例子，不继续堆术语；
- 不把“差不多”自动理解为已经掌握。

### 1.7 示例链路曾经前后不一致

“上传文档”和“聊天请求”曾被混在一条链里；“普通 ReAct 的最终总结”有时被说成 TaskGraph 节点，有时又说由主 Agent 汇总；四个子 Agent 与 Router/Planner/Runtime 也曾混淆。

优化方式：始终分开三条链：

```text
上传链：文件 → 解析 → 父子分块 → Embedding/索引 → 持久化

普通聊天链：消息 → Router → Plan-and-ReAct → Tool → 汇总答案

知识库聊天链：消息 → Router → 普通 RAG
                         └→ 命中研究/总结/报告/文档/方案/分析关键词 → Agentic RAG
```

### 1.8 评测讲法曾经不够严谨

对话中将 Recall、Hit、MRR、NDCG 混用的风险较高，还没有第一时间指出评测 Adapter 使用标准答案决定是否开 RAG 会造成标签泄漏。

优化方式：评测必须先冻结“输入”，标准答案只能参与评分，不能反过来控制系统路由。

---

## 2. 对照 Go 后发现：Go 版也不是标准答案

### 2.1 报告意图过宽

Go 的 `reportIntent` 复用 `needsSubAgentPlan`，只要出现“总结、报告、文档、方案、分析”等词，就可能进入带保存副作用的 Agentic RAG。

证据：

- [Go routeDecide](../../../AGI-saber/internal/application/chat/runtime_process.go)
- [Go needsSubAgentPlan](../../../AGI-saber/internal/application/chat/plan_graph.go)

问题：

- “总结一下文档”本来只需要只读 RAG；
- 过宽路由会增加成本、延迟和误写入风险；
- 写入副作用不应该由模糊关键词触发。

### 2.2 Review 没有修改正文

Go 固定流水线是：

```text
Research → Writer → Review → Doc
```

Doc 同时依赖 Writer 和 Review，但保存的仍主要是 Writer 初稿；Review 更像元数据和提示，没有形成自动修订闭环。

证据：[Go subAgentPipelineNodes](../../../AGI-saber/internal/application/chat/plan_graph.go)

### 2.3 失败依赖也会推进下游

Go Runtime 把 Done、Skipped、Failed、Cancelled 都当成终态并调用 `MarkDone`，下游节点因此可能在必需材料缺失时继续执行。

证据：

- [Go Runtime 推进逻辑](../../../AGI-saber/internal/application/chat/runtime_graph.go)
- [Go TaskGraph MarkDone](../../../AGI-saber/internal/domain/graph/graph.go)

这适合“尽力回答”的可选信息，不适合报告保存、高风险判断或强一致任务。根本原因是图模型没有区分必需依赖和可选依赖。

### 2.4 Rerank 输出校验不完整

Go Reranker 会接受不完整评分，未打分候选使用 `-1` 继续排序；对 NaN、越界分数和候选数量不一致也缺少完整校验。

证据：[Go LLMReranker](../../../AGI-saber/internal/domain/rag/reranker.go)

### 2.5 无答案门禁没有闭环

Go 配置和主 RAG 合成链没有提供经过校准的 Rerank 拒答门禁。因此“召回到弱相关段落”仍可能进入答案生成。

结论：Python 不应逐行复刻这些问题，而应保留接口思想、修正异常语义。

---

## 3. 本次已经落地的 Python 优化

### 3.1 恢复冻结 Go 的报告路由

当前 Python 以 Go `845e8f7` 为行为基线：`use_rag=true`、知识库已加载且问题包含“研究、调研、总结、报告、文档、方案、分析”任一关键词时进入 `rag_agent`；同样条件下的普通问题走 `rag`，其余请求走 `react`。

这属于“复刻了基准行为”，不是“已经解决副作用误触发”。当前没有要求同时出现创建、产物、保存三类词，也没有可靠识别“不保存”等否定表达，因此“总结一下文档”仍可能触发报告 DAG 和文档保存。若要上线，应把保存意图拆成独立分类或在写入前增加显式确认，但这会改变冻结 Go 的对外语义，需要以新版本策略发布。

源码：

- [报告关键词判断](../internal/agent/planner.py)
- [主路由接入](../internal/agent/agent.py)
- [Planner 确定性分流](../internal/agent/planner.py)

行为变化：

```text
“总结一下这份文档”                  → Agentic RAG
“生成一份报告，但不要保存”          → Agentic RAG（当前风险）
“生成 Markdown 报告并保存到文档库” → Agentic RAG
```

### 3.2 恢复四角色固定报告 DAG

当前任务图与 Go 冻结基线一致，固定为四个节点：

```text
Research
  ↓
Writer
  ↓
Review
  ↓
Doc 保存
```

Research 生成检索发现，Writer 生成 Markdown，Review 输出审查意见，Doc 读取 Writer 草稿并保存到文档库、可回填 RAG。Review 结果会进入上游结果与文档元数据，但当前不会修改正文，也不是保存硬门禁；代码中没有 Writer Revision 或 Final Gate。

源码：

- [报告任务图](../internal/agent/planner.py)
- [四角色实现](../internal/agent/subagents.py)
- [上游结果带执行角色](../internal/agent/graph_runtime.py)

仍未完成：让 Review 触发有界修订、按严重程度决定“拒绝保存/允许保存但标记人工确认”，以及用标注报告测量门禁漏报率和误报率。这些只能作为后续设计，不能在面试中说成已经落地。

### 3.3 TaskGraph 增加必需/可选依赖

节点新增 `optional_depends_on`：

- `depends_on` 默认是必需依赖；
- 必需依赖失败、取消或跳过，下游级联 `SKIPPED`；
- 显式放入 `optional_depends_on` 的依赖失败后，下游才允许透明降级；
- Planner 输出也支持该字段；
- 配置非法时会在图校验阶段拒绝。

源码：[TaskGraph 依赖语义](../internal/graph/task_graph.py)

这让“新闻失败但天气仍可回答”和“Review 失败禁止保存报告”能够表达为两种不同任务，而不是靠 Runtime 猜测。

### 3.4 接入无答案门禁

新增 `rag.no_answer_threshold`，默认值为 `0.30`。

关键约束：

- 只对成功 Rerank 后的 0~1 分数生效；
- 不对 RRF 分数生效，因为 RRF 是排名融合值，不是稳定概率；
- 最高 Rerank 分数低于阈值时，返回“未找到足够相关的可靠内容”；
- 0.30 是初始配置，不是已经证明最优的生产阈值。

源码：

- [配置](../config/config.py)
- [回答门禁](../internal/rag/rag.py)

### 3.5 强化 Rerank 输出校验

现在遇到以下情况会整体回退 RRF，而不是使用部分错误分数：

- JSON 解析失败；
- 候选数量不一致；
- 编号缺失或越界；
- NaN/Infinity；
- 分数超出 0~10。

源码：[LLMReranker](../internal/rag/reranker.py)

这只解决显式故障。模型格式正确但排序错误属于隐性质量失败，仍要靠离线评测和 Badcase 发现。

### 3.6 修复评测标签泄漏

旧实现根据 `expected.evidence_ids` 判断是否打开 RAG，相当于运行前偷看标准答案。

现在改为：

```yaml
metadata:
  input:
    use_rag: true
```

输入配置决定系统行为，`expected` 只用于评分。

源码：[Evaluation Adapter](../internal/evaluation/adapters.py)

### 3.7 图谱权重真正接入 RRF

原实现配置了 `kg_weight`，一级 RRF 却使用固定 `1.0`。现在图谱贡献使用配置权重，避免图谱路径无意中压过语义和关键词路径。

源码：[HybridStore 三路 RRF](../internal/rag/hybrid.py)

仍需通过消融实验选择合理权重，不能因为配置接通就宣称质量提升。

---

## 4. 自动化验证

本次新增或更新的测试覆盖：

- 报告关键词在知识库模式下进入 `rag_agent`；
- 四角色固定依赖、Doc 文档保存与回填；
- 普通 `react` 不暴露或调用报告子 Agent；
- 必需依赖失败级联跳过；
- 可选依赖失败允许降级；
- 非法可选依赖被拒绝；
- NaN、越界和不完整 Rerank 输出回退；
- 低分 Rerank 触发无答案拒答；
- RRF 低分不会被错误套用阈值；
- 评测标准答案不再控制 RAG 开关；
- 配置解析与默认值。
- 无答案阈值遍历、错误成本选择与 Markdown/JSON 报告；
- Rerank Closed/Open/Half-Open 状态转换、冷却探测和 RRF 回退；
- 远程 Rerank 失败后本地确定性重排，再失败回退 RRF；
- Embedding、Milvus、Elasticsearch、图谱检索独立熔断；
- Trace 脱敏、幂等持久化与租户隔离读取；
- RAG 索引 upsert、有界重试及从主存储重建；

复核时应在同一提交运行 `python -m pytest tests -q`，并用 `python scripts/run_go_python_conformance.py` 生成带源码哈希的 Go/Python 一致性报告。静态文档不记录会随用例增长而失效的固定通过数。测试通过只证明行为符合已有合同，不等于 RAG 质量已经提升；阈值质量仍需要真实 Golden Query、人工可回答标签、线上 Badcase 和消融报告。

---

## 5. 还没有做好的地方

### P0：上线可信度

1. **无答案阈值真实数据闭环**  
   代码已能遍历阈值并报告 Precision、Recall、F1、错误回答率、错误拒答率和加权成本；下一步替换示例数据为人工标注业务集，并绑定模型、Prompt、知识库和配置版本。

2. **补齐报告修订与保存门禁**  
   当前 Review 只产出意见，Doc 仍保存 Writer 草稿。下一步先实现有界修订和结构化终审，再用已标注报告测量关键问题拦截率、误报率和修改后通过率。

3. **RAG Trace 生产治理**  
   当前已生成统一 `trace_id`，记录原问题、改写查询、每路候选与排名、二级 RRF、Rerank 后候选、父块证据和拒答原因；落库前脱敏密钥与个人信息，按用户隔离查询，并支持 30 天保留、过期清理和用户删除。下一步增加跨服务 span、字段级加密和访问审计。

4. **完整多轮评测**  
   当前 Local Adapter 主要重放 user turn；还需要可靠复现 assistant/tool 历史、会话隔离、业务意图和 slots。

### P1：故障与降级

1. 抽离真正的 `InvocationHarness`，统一参数校验、超时、重试、权限、幂等和指标；
2. 三态熔断已抽成通用能力，并覆盖远程 Rerank、Embedding、Milvus、Elasticsearch 和知识图谱；下一步接入集中指标与告警；
3. 实现真正的 SQLite FTS5 + 本地向量双路回落，而不是轻量词法搜索；
4. RAG Chunk 投影已增加 Milvus upsert、有界重试和从主存储重建；SQLite 路径还有同事务 Outbox、自动补偿、dead 状态和租户隔离的人工重放。长期记忆另以 SQLite/PostgreSQL 权威库和 Outbox-first 事件驱动投影：SQLite 更新本地账本，生产 PostgreSQL 消费者才驱动 Milvus/Neo4j，并具备租约、重试、dead-letter 与周期对账；真实外部服务仍需完整环境故障注入；
5. 对写文档、入库和工具副作用使用幂等键。

### P2：质量与成本

1. 父块已有 0.85 字符 3-gram/编辑相似度近重复去重；下一步对照 Embedding 语义去重的质量和成本；
2. 当前已有远程 LLM、可选懒加载 Cross-Encoder、本地确定性重叠重排和 RRF 回退；下一步安装具体权重并做质量、延迟和资源实测；
3. 只在多轮指代或检索困难时做查询改写；
4. Embedding、改写和 Rerank 缓存；
5. 图谱增益消融和建图失败补偿；
6. Prompt 注入隔离、文档 ACL、工具最小权限和审计日志；
7. RAG Lab 页面展示切分、各路召回、RRF、Rerank、父块和最终 Prompt，但隐藏密钥与内部敏感信息。

---

## 6. 后续教学应如何进行

每个模块固定使用下面的教学模板，不再跳跃：

### 第一步：人话目标

先回答“它解决什么问题”，不出现超过三个新名词。

### 第二步：一个请求走全链

例如只用：

```text
“根据已上传资料生成 Markdown 报告并保存。”
```

从入口一直走到输出，中途不切换例子。

### 第三步：源码锚点

指出入口函数、核心数据结构和异常分支；不逐行朗读。

### 第四步：为什么这样设计

至少比较一个替代方案：

- 为什么不是所有请求都 Planner；
- 为什么不是大块直接检索；
- 为什么 RRF 后还要 Rerank；
- 为什么失败依赖要分必需和可选；
- 为什么 Review 不能只记录意见。

### 第五步：真实不足

明确当前实现没有什么，禁止把设计稿说成完成态。

### 第六步：用户复述

用户讲完后只纠正三个最关键问题，不打断，不马上跳下一章。

### 第七步：面试表达

最后压缩成 30 秒、90 秒和追问版，而不是一开始就背答案。

---

## 7. 现在可以怎样准确介绍这次优化

> 我没有把 Go 版当作绝对正确实现，而是先冻结 `845e8f7` 并恢复可执行的一致性合同。Python 当前同样保留 `rag_agent / rag / react` 三路路由和 Research、Writer、Review、Doc 四节点报告 DAG；Review 尚不修改正文或阻止保存，宽关键词路由也仍是已知风险。Python 扩展侧，TaskGraph 区分必需与可选依赖；RAG 只用有效远程 Rerank 分数做无答案拒答，并输出可解释、脱敏且有保留期限的 Trace；远程 Rerank 可熔断并依次切换可选 Cross-Encoder、本地确定性重排和 RRF；Embedding 与三路检索也独立熔断；父块具备近重复去重；RAG 索引可重建，长期记忆采用 DB 权威行加事务 Outbox，本地 SQLite 更新投影账本，生产 PostgreSQL 消费者驱动 Milvus/Neo4j。评测开关不再读取标准答案，图谱权重也真正接入 RRF。工程契约与 Replay 基准均不能替代真实外部基础设施、真实业务标注和线上故障注入。
