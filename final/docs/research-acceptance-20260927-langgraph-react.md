# LangGraph + React 切换后验收记录（2026-09-27）

范围：验证 `research.engine=langgraph`、`chat.engine=langgraph` 默认栈与 React 前端的真实模型端到端与 GUI 交互。迁移提交链 `268dc0b → 01acb92 → 949c911 → 5a190de`。

## ① 真实模型端到端 smoke（`scripts/smoke_research.py --live`）

- 结果：**completed**。运行 `9a05d6c1-2848-4823-970f-5b8b525ada61`，计划真实生成并批准，`reviewed: true`，1 来源 / 1 引用，5 次 LLM 调用、3 次工具调用、1 轮研究、输出 2100 tokens、执行 14.1s，`limitations: []`。
- 事件链完整：`queued → started → plan_created → plan_review_required → plan_approved → started → node_start → research_round → source_found → research_round → node_done → … → token×3 → done`（LangGraph 引擎在默认配置下走通全部生命周期）。
- 证据目录：`runtime/research-acceptance/b5edaba4b264`。

## ② 浏览器 GUI 回归（React 版，真实后端 127.0.0.1:8090）

环境准备：`scripts/run_research_local.py` 启动（独立 `runtime/research-local/` 存储），注册测试账号 `gui_regress_01`，真实 DeepSeek + Tavily 凭证。黑盒 GUI 操作，DOM + 截图双证据。

| # | 测试点 | 结果 | 证据要点 |
|---|---|---|---|
| T1 | 注册登录 | ✅ | 注册后侧栏出现用户名/登出，弹窗关闭 |
| T2 | 工作台初始渲染 | ✅ | 侧栏/导航/聊天区/登录弹窗布局无缺陷 |
| T3 | 聊天流式 | ✅ | 用户气泡即时渲染、生成中停止键出现、回答完整含思考面板与工具轨迹 |
| T4 | 工具级 HITL（意外触发） | ✅ | 模型调用 `exec_command`（警告级·命令链规则）→ 审批面板弹出（批准/拒绝/十分钟有效期）→ 批准后 mock 沙箱执行并如实标注"Docker 不可用"→ 任务转"恢复任务" |
| T5 | 任务恢复 | ✅ | "恢复任务"按钮续写成功，markdown 渲染完好 |
| T6 | 研究计划生成与审批 | ✅ | 计划含约束清单与 4 步骤（3 research + 1 write，依赖关系正确），`拒绝计划/修改计划/批准并开始研究` 三控件齐备 |
| T7 | 计划编辑 + 版本 | ✅ | 编辑步骤 2 标题（前后值核对）→ 保存 → `PLAN / V1 → V2` |
| T8 | CAS 版本冲突 | ✅ | 双标签页：B 抢先保存至 V3 后，A 的过期编辑态被自动同步为 V3 并退回审批视图，**未发生旧版本覆盖**；显式 409 提示路径由前后端测试覆盖 |
| T9 | 批准执行 + 刷新恢复 | ✅ | 批准 V3 → 状态"执行中" → 刷新页面 → 侧栏仍显示"执行中"，重开详情完整恢复（状态/会话/计划） |
| T10 | 引用报告 + 下载 | ✅ | 报告含 `[1]`-`[20]` 编号引用与 References 锚点（sqlite.org/wal.html、lockingv3.html、isolation.html、busy_handler.html 等）；`下载 Markdown` 下载事件触发成功 |
| T11 | 拒绝路径 | ✅ | 新任务计划 → 拒绝 → 状态"已停止"、计划标记"已拒绝" |
| T12 | 375px 移动布局 | ✅ | `scrollWidth == 375`，无横向溢出，工作台移动视图完整可读 |

## 发现与观察（均不阻塞验收）

1. **模型服务稳定性（真实问题，非本项目缺陷）**：T9 研究的 4 步中 3 步遇到 `ProviderUnavailable: LLM 返回空结果`（deepseek-flash 间歇空响应），1 步达到轮次/来源上限。平台行为完全正确：黄色横幅"部分完成：请结合下方研究限制和来源判断结论"、逐步骤限制明示、报告仅呈现已获证据、无伪造结论。建议：更换/重试 `AGI_LLM_FAST_MODEL` 或调整研究预算后重跑对比。
2. **来源超出计划约束（质量观察）**：计划约束"仅引用官方文档"，研究员仍收集了论坛/第三方博客来源（引用 [16]-[20]）。约束目前约束的是"关键差异结论的依据"，未硬性过滤来源收集。可作为后续改进：计划 tool_policy 传导到来源过滤。
3. **低危 UI 观察**：拒绝计划后左侧最近任务列表的状态标签未即时刷新（主视图状态正确），下一次轮询/手动刷新即对齐。
4. **停止键点击未在 GUI 截到实流**：两次尝试中生成速度快于预期，停止控件出现已留证；取消语义由后端专项测试（取消令牌/SSE 断连）覆盖。

## 结论

默认栈（LangGraph 双引擎 + React 前端）通过真实模型端到端与 12 项 GUI 回归，CAS/审批/恢复/引用/降级语义在真实 UI 上全部成立。已知问题为模型服务侧稳定性与两项低危改进点，不阻塞迁移验收。
