# 02：Runtime、Harness 与可靠工具执行

## 1. 用一句话分开它们

Runtime（运行时）负责把计划实际跑起来：找可运行节点、调用工具、更新状态、处理重试和取消。

本项目语境下的 Harness（执行保障层）是更大的工程集合：上下文装配、工具权限、预算、错误契约、沙箱、轨迹、持久化、评测及门禁。它不是一个训练算法，也不只是一份测试集；这里没有一个单文件包办所有 Harness 能力。

```text
Harness：上下文 + 权限/预算 + 观测 + 存储/恢复 + 评测
             ┌───────────────────────────────┐
             │ Runtime：任务图 → 节点调度 → 执行 │
             │               ↓              │
             │       工具 / 模型 / 子代理       │
             └───────────────────────────────┘
```

## 2. 源码阅读顺序

| 文件 | 重点 |
|---|---|
| [task_graph.py](../../internal/graph/task_graph.py) | Node、NodeStatus、依赖合法性、`ready_nodes` |
| [graph_runtime.py](../../internal/agent/graph_runtime.py) | `GraphConfig`、`execute`、`_execute_single_node`、racing |
| [tools.py](../../internal/tools/tools.py) | Tool、ToolResult、ToolError、ToolCallContext、注册表 |
| [cancel.py](../../internal/agent/cancel.py) | 取消令牌与注册表 |
| [snapshot.py](../../internal/repo/snapshot.py)、[restore.py](../../internal/agent/restore.py) | 持久化了什么，恢复了什么 |
| [executor.py](../../internal/sandbox/executor.py)、[validator.py](../../internal/sandbox/validator.py)、[docker.py](../../internal/sandbox/docker.py) | 权限与真实执行边界 |

## 3. DAG 解决的是依赖，不是“智能程度”

DAG 是有向无环图。节点是一项工作，有向边表示必须先完成的条件，无环避免相互等待。

例如先检索两个独立来源，再汇总：

```text
检索 A ──┐
         ├── 汇总 → 保存
检索 B ──┘
```

图结构要检查节点 ID、依赖存在性、环。运行时还要区分：

- 必需依赖失败：不能当成成功继续执行下游。
- 可选依赖失败：允许使用剩余证据继续，但不能假装失败来源有结果。
- 竞速备选：一条成功路径可能替代同组其他路径，与“所有来源都必须完成”不同。

当前运行时取一批 ready 节点、启动线程、等待本批结束，再开始下一轮；信号量限制并发。它有批次屏障，不是哪个节点一结束就立即激活所有下游的最优异步调度器。YAML 的 `max_parallel` 当前为 2；不能据此承诺每个请求总共只有两个线程，因为上下文、改写和多个请求还有各自并发。

### 追问：怎么估算工作流耗时？

理想下界是关键路径总耗时，还受并发配额、排队、批次等待和重试影响。两个 1 秒和 10 秒的独立工具如果同批执行，依赖短工具的下游仍可能等待长工具，正是批次调度的代价。真实优化要先记录每个节点排队与执行耗时，而不是直接增加线程数。

## 4. 工具不是一个裸函数

`Tool` 除执行函数，还携带名称、描述、参数、matcher 等。运行时根据工具名从允许集合取工具，不应该执行模型随口生成的任意名称。

`ToolResult` 包含成功标志、文本/结构化 payload、错误、耗时和 metadata；`ToolError` 包含 code 与 retryable 等。这样才能区分“业务没有数据”“网络失败”“参数错误”，而不是把所有失败变成一段供模型猜测的字符串。

正常流程是：检查取消→节点 RUNNING→检查工具→组装上游参数→发出 tool_call→执行→解释结构化错误或结果→记录成功/失败→更新任务与事件。

### Function Calling、Tool Use、MCP 的准确口径

Tool Use 是系统调用工具完成任务的能力。本项目通过模型输出 JSON 计划和本地执行实现这一点。

查看 [llm.py](../../internal/llm/llm.py) 的 `_call_chat`：请求体当前包含 model/messages/temperature，没有原生工具声明和工具调用消息处理。因此不能说已经接入模型厂商原生 Function Calling 协议。

`new_mcp_tool` 当前是向指定 endpoint 发送 HTTP JSON 的适配器。命名不等于完整实现 MCP：官方协议还定义初始化协商、工具发现、调用消息等契约，应逐项核对后再声称兼容。[MCP Schema Reference](https://modelcontextprotocol.io/specification/2025-11-25/schema)

## 5. 重试：记住两个容易出错的细节

当前配置 `max_retries=3`，代码使用 `range(max_retries)`，所以普通工具最多是三次总尝试，不是首次加三次重试。`skill_` 开头的重模型工具只尝试一次，且小于 300 秒的正数步骤超时会被提升到 300 秒。HTTP 请求总超时和工具超时可能不一致，要一起检查。

结构化错误决定是否继续：参数/取消等通常不重试，网络和超时等可以重试。当前 HTTP 异常分类把 4xx 作为不重试，因此 429 限流还需要单独完善；不能说已完整实现所有 Retry-After 语义。

### 为什么“失败就重试”很危险？

工具向数据库写入成功，但响应在网络上丢了。客户端看到超时，再执行一遍，就可能写两份报告。超时只能说明没收到成功结果，不能证明没有发生副作用。

建议的生产设计（不是当前全局已有能力）：

```text
业务幂等键 = 用户/租户 + 任务 ID + 节点 ID + 动作版本
执行前登记 → 执行外部动作 → 保存结果/状态 → 重试查旧结果
```

外部 API 也要支持幂等键或可查询的业务唯一 ID；仅在本地登记“执行中”不能解决所有崩溃窗口。读操作可以更积极地重试，转账、发信、删除和写报告应保守处理，必要时进入人工核对。

当前 `X-Request-ID` 在实验曝光账本等位置有幂等用途，不能外推成所有工具 exactly-once。

## 6. 取消与超时为什么不等于强制停止？

`CancelToken` 是协作式取消。运行时在节点前、等待和重试处检查；ToolCallContext 传递 deadline 和取消信息。阻塞中的第三方调用若不合作，Python 线程无法被安全地直接杀掉。

因此至少有三种不同状态：用户不再等待、运行时不再接受结果、外部动作实际停止。前端 Abort 也不自动证明第三者成立。

当前取消注册表属于用户 Agent。同一用户多并发请求如果共享 cancel_all，可能互相影响。改进方向是请求级取消令牌、工具端超时/取消协议，以及对已开始副作用的最终结果补录。

Racing 是多条备选路径竞争，首先成功者提供结果，并协作取消其他路径。适合可替代的只读来源；不能让两个会写入的工具竞速后以为“输家没有发生动作”。

## 7. 快照、恢复、持久执行不是同一个词

运行时会保存任务图等状态，`restore.py` 有长期记忆和近期聊天恢复逻辑。这些帮助展示状态、恢复上下文和排查中断。

但一个真正的持久执行器还需要回答：

1. 进程在外部写入后、快照前死亡，重启是否重复写？
2. 多 worker 是否能同时认领同一个节点？
3. RUNNING 节点失联多久变为可恢复？
4. 子代理的模型响应是否持久化，可否复用？
5. 恢复使用原工具版本还是新版本？

当前不能仅凭快照 API 宣称完成了这些保障。可以讲“有状态快照和恢复基础，持久调度与副作用恢复还需补齐”。

## 8. 沙箱到底防什么？

Docker 后端配置包括网络关闭、只读根目录、临时目录、内存/CPU/PID 限制、capability 丢弃和 no-new-privileges；命令还经过校验器。实际生效取决于配置、后端可用性和挂载范围。

本地执行后端不能等同 Docker 隔离。Prompt 中“禁止危险操作”不是操作系统权限；工具参数 `confirm=true` 也不自动证明用户亲自确认，因为模型可能自己生成它。可信审批应由服务端验证批准身份、具体动作与一次性令牌。

另一个待验证边界：杀掉超时的 Docker CLI 进程是否一定清理容器与容器内任务？不能只看 `subprocess.run(timeout=...)` 就断言所有子任务已回收；生产需要跟踪 container ID 并验证终止与清理。

## 9. 执行轨迹是什么，不是什么？

Trace 是可观察的系统事件：路由、规划摘要、节点、工具参数、返回状态、检索证据和最终回答。界面把一个字段命名为 Thought，不意味着展示模型私有完整思维链。

好的工具事件至少需要 task/trace/node/attempt ID、时间、状态、错误码、耗时、后端来源，以及经过脱敏的参数和结果摘要。不要把 API Key、用户私密原文、完整敏感文档写入日志。

看到 `{}` 时先对照三个层次：后端原始事件→响应/存储序列化→前端展示字段。Replay 数据只写事件名也会出现空 payload。增加“执行成功”标签不如补齐可核对事件；没有 payload 时不能断言真实工具执行了。

## 10. 本章测试与练习

源码关联测试：[test_graph_runtime.py](../../tests/test_graph_runtime.py)、[test_task_graph.py](../../tests/test_task_graph.py)、[test_mcp_structured_contract.py](../../tests/test_mcp_structured_contract.py)、[test_trace_persistence.py](../../tests/test_trace_persistence.py)。

```powershell
python -m pytest tests/test_graph_runtime.py tests/test_task_graph.py tests/test_mcp_structured_contract.py -q
```

这些是建议复现命令，测试中的替身调用不证明生产远程服务可靠。

练习：手画 A→B→C，令 B 第一次超时、第二次成功，写出状态和 attempt 事件；再把 B 改成“已写数据库但响应超时”，解释原测试为什么不足以覆盖重复写。最后比较“重试 B”和“重规划成 D”的安全条件。
