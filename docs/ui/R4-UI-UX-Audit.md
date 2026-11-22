# R4 前端 UI/UX 审计报告

- 时间：2026-11-22（R4-1.2）
- 方法：全部 27 个前端文件逐文件阅读（17 视图 + 3 组件 + composables/api/router/App/main/styles），并重新核查后端端点。

## 1. 数字核查（不沿用历史假定）

| 项 | R3 记录 | R4 实测 | 结论 |
|---|---|---|---|
| 主路由 | 12 | 12（router.ts:13-26） | 一致 |
| 渲染分支 | 17 | 17（App.vue:26-60 switch 分支） | 一致 |
| 平台 API 端点 | 39 | 38（36 个 @router + 2 个 @app，apps/api 全量 grep） | **-1 差异**：R3 计数含一处变体；以 38 为准 |
| 视图代码量 | — | src 全部约 3,353 行；视图中 60%~75% 是 scoped 样式 | 样式上移收益最大 |

## 2. 总体结论

- **无静态假数据**：所有视图数据均来自真实 fetch（client.ts 无 mock 层），空态兜底属正常防御。
- 状态覆盖基本具备：StateBlock 提供 loading/error/empty，Pagination/StatusPill 已组件化。
- 主要短板：①一个真实功能 bug；②错误被吞；③N+1 请求；④样式 17 处重复定义；⑤表单可访问性全局缺失；⑥Benchmark/Graph 两页只是"只读统计表"，与 R4 产品化要求差距最大。

## 3. 发现的问题（按严重度）

### P0 功能缺陷

| # | 问题 | 位置 |
|---|---|---|
| 1 | **查询串路由断裂**：TasksView 链接 `/runs?task=...`，但 App.vue 用 `route.split('/')` 取首段，`runs?task=T1` 匹配不到 `case 'runs'`，落入 default 渲染 Overview。核心链路"Task→其 Runs"从 UI 上是断的 | TasksView:89、App.vue:27 |
| 2 | **后端错误被吞成空态**：RunDetailView/AssertionsView 的 report 加载 `catch → null`，请求失败显示"尚未生成报告"，真失败与真未生成不可区分 | RunDetailView:11-17、AssertionsView:9-15 |
| 3 | **RunsView N+1 串行请求**：逐 task `await api.taskRuns(id)`，task 多时首屏极慢 | RunsView:27-33 |
| 4 | **RepairsView 批准无客户端校验**：approver 为空也可点批准；审批人/记录/理由为页面级共享状态，多提案并存易填错对象 | RepairsView:9-11, 99 |
| 5 | 调试残留 `route {{ route }}` 打印在页面上 | RunsView:82 |

### P1 交互与状态

- DiagnosisView 错误双重渲染（StateBlock error + 行内 error 各一遍）；未查询时缺"请先输入 run_id"引导态。
- EventSource 无 onerror 监听：连接断开后"停止实时"按钮仍亮着，用户以为还在实时。
- EventsView 两数据源共享分页变量，切换源只重置页码，语义混乱。
- RunDetailView 取消失败用阻塞式 `alert()`。
- CaseVersionsView 为取版本列表复用整个 caseDetail 接口（信息重复展示）。
- Overview 卡片可点击但无 role/tabindex/键盘支持。

### P2 一致性与可维护性

- `.head`/`.btn`/`.muted`/`.small`/`.hint` 等在 17 个视图 scoped 中逐行重复（详见附录 A）。
- `.small` 三处不一致：全局 12px、Overview 13px、CaseDetail 11px。
- 全部表单 input/select 无 label（仅 placeholder），屏幕阅读器不可用。
- EvidenceView badge 写死色值，暗色模式对比度不足；layer 过滤逻辑对"非 raw 即 derived"的假设脆弱。
- client.ts 有 3 个方法从未被视图调用（api.task / evidenceRecord / repair）。

## 4. SSE 与持久化状态关系（符合 R4 约束）

- RunsView：SSE 仅作失效信号，收到 run.* 帧后重拉 REST——REST 是权威。✓
- EventsView：SSE 帧进"实时帧"本地面板（上限 50 条），持久化表格仍以 REST 为准。✓
- 无前端决定 Verdict、无绕过审批的逻辑。✓

## 5. Benchmark / Graph 现状（R4-1.4 深化重点）

**BenchmarkView（97 行，最短）**：两张只读表；metrics 压成 `k=v·k=v` 纯文本；无 track/状态筛选、无实验详情、无分页、无与 Run/Evidence 的关联链接。
**GraphView（191 行）**：无任何图渲染（子图查询结果是原始 JSON `<pre>`）；node_type 只做计数；properties/independence_group/source_ref 全未展示；边明细 `slice(0,100)` 无提示无分页。

## 6. R4-1.4 改造范围（由本审计导出）

1. 修 P0 五项（路由解析、错误语义、N+1、审批校验、调试残留）。
2. 样式层重构：公共类上移 styles.css，视图 scoped 样式只留页面特有部分。
3. 信息架构重组为七组导航（工作台/测试设计/测试资产/执行/证据/智能/修复/评测/系统）。
4. Overview 升级为真正工作台首页（服务状态、待办、最近失败、Benchmark 入口）。
5. GraphView 引入真实 SVG 图渲染 + 节点联动子图。
6. BenchmarkView 消费 R4-2 持久化数据：指标表格化/条形可视化、状态语义化（NOT_RUN/BLOCKED/空数据区分）。
7. 全表单补 label、修复暗色对比度、键盘可达性。

## 附录 A：跨视图重复 CSS 类清单（节选）

`.head`(17 视图)、`.btn`(17+2 组件)、`.muted`(17，与全局 styles.css 重复)、`.small`(17，与全局重复)、
`.hint`(11)、`.actions`(7)、`.form`(3)、`.meta .k`(3 处宽度不一)、`.split`(4)、`.kv`(2)、
`.plain`(5)、`.error`(5)、`.card`(2)、`.payload`(2)、`.search`(2)。
