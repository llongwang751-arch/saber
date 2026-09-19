# AGI-saber Python/FastAPI 学习路线

目标不是把所有代码背下来，而是能独立运行、定位问题、增加一条评测规则，并在面试中用证据解释设计取舍。建议按下面顺序边做边学。

## 1. 前置知识

先达到“会用”即可，不必学完整本教材：

- Python：函数、类、类型标注、异常、生成器、线程和 `async/await`；
- Web：HTTP 方法/状态码、JSON、文件上传、SSE、JWT；
- FastAPI：Pydantic 请求模型、依赖注入、Router、中间件、TestClient；
- 数据库：表、主外键、索引、事务、SQLAlchemy Session、Alembic；
- 测试：等价类、边界值、状态迁移、故障注入、pytest fixture；
- LLM 应用：Prompt、Token、RAG、Embedding、工具调用、Agent、Trace；
- 数据工具：基础 SQL、Pandas/Excel 思路。项目的 CSV/XLSX 解析可直接作为练习。

## 2. 代码阅读地图

```text
请求入口
  main.py
    → internal/handler/handler.py
    → internal/application/api.py
    → internal/evaluation/api.py

Agent 主链路
  internal/agent/agent.py
    → router / planner / tools / rag / memory

业务持久化
  internal/application/models.py
    → store.py
    → local_repos.py
    → alembic/versions/

质量评测
  evaluation/schemas.py
    → adapters.py
    → evaluators.py
    → service.py
    → store.py

前端
  web/src/App.vue
    → stores/
    → components/
```

每读一个文件，只回答四个问题：输入是什么、输出是什么、状态存在哪里、失败如何暴露。

## 3. 四周路线

### 第 1 周：跑通与 HTTP 契约

动手目标：

1. 从空环境安装依赖、升级迁移、启动服务；
2. 注册、登录，在 Swagger 中携带 Bearer Token；
3. 调用 `/healthz`、`/readyz`、`/api/status`；
4. 上传一个 TXT，确认文档、版本和 RAG 分块入库；
5. 故意使用错误 token、超大文件、空消息，记录状态码和错误体。

面试要会回答：401 和 403 的区别、为什么密码不能明文存、JWT 的缺点、SSE 与 WebSocket 的区别、中间件如何生成 request ID。

### 第 2 周：RAG、Agent 与可观测性

动手目标：

1. 跟踪 `/api/chat/stream` 到 `UnifiedAgent.process_stream`；
2. 画出 route → RAG/tool/ReAct → memory 的时序；
3. 在未配置 Embedding 时验证本地词法检索；
4. 配置 Embedding 后比较召回结果；
5. 阅读标准 Trace 事件，不记录模型隐式思维链。

面试要会回答：为什么分块需要 overlap、Embedding 维度变化为何要重建索引、RRF 解决什么问题、Prompt Injection 如何污染 RAG、工具调用为什么必须记录参数摘要和错误。

### 第 3 周：质量评测闭环

动手目标：

1. 运行 12 条合成用例；
2. 读懂 `EvalCase`、`Expected`、`AgentOutput`；
3. 手写一条“信息缺失必须追问”的用例；
4. 给 baseline 制造错误工具参数，观察指标和 Badcase；
5. 跑 candidate，验证 fixed/regression；
6. 写一条 pytest 覆盖新规则。

面试要会回答：为什么数据集版本不可变、哪些指标适合规则评测、什么时候用 LLM-as-a-Judge、为什么安全要做硬门禁、Badcase 如何证明已经修复、如何避免只修一个 Case 却引入回归。

### 第 4 周：数据、生产化与现场表达

动手目标：

1. 用 SQL 查 Badcase Pareto、失败率和 P95；
2. 导入猪场 CSV，观察幂等、脏数据和缺字段；
3. 新建两个账号，验证文档、Skill、养殖和评测隔离；
4. 跑当前提交的全量测试、Vue 构建、Alembic check，并保存实际输出；
5. 准备 3 分钟和 10 分钟两套演示话术。

面试要会回答：SQLite 何时不够、事务 outbox 解决什么一致性问题、单进程后台线程的限制、如何迁移任务队列、如何做并发限流/重试/幂等、如何保护隐私和 API Key。

## 4. 面试前必须能现场完成的任务

- 在 5 分钟内启动并登录；
- 能从 500 日志中的 request ID 找到真正异常；
- 能解释一个 Trace 从用户消息到最终回答的事件序列；
- 能新增一条 Eval Case 和一个断言；
- 能写 SQL 统计 Badcase Top N；
- 能说清 Replay 数据为什么不代表真实模型效果；
- 能指出项目的真实边界，不把“可演示”说成“已生产部署”。

## 5. 推荐练习题

1. 给 HTTP Adapter 增加 500、超时、畸形 JSON 故障注入测试；
2. 新增 `department_matching_accuracy` 指标并写边界用例；
3. 给评测页面增加风险标签筛选；
4. 做 20 条人工标注样本，计算两位标注员的一致率；
5. 把单进程 Run Executor 替换为任务队列，说明幂等键和取消语义；
6. 为上传接口补反向代理 body limit，验证 chunked request 场景；
7. 为生产数据库设计 JSONB/GIN 索引并解释查询模式。

## 6. 自我验收标准

你真正学会这个项目的标志不是“看完了”，而是：

- 不看答案能画出一条完整链路；
- 修改失败规则后，能预测哪些测试会失败；
- 能用日志、数据库和 Trace 三种证据定位同一个 Badcase；
- 能区分模型问题、Prompt 问题、检索问题、工具问题、数据问题和基础设施问题；
- 面试官追问边界时，能给出下一步设计，而不是只重复技术名词。
