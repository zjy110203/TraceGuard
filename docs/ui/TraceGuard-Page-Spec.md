# TraceGuard 页面规格 v1.0（R4-1.3）

信息架构按业务流程重组。现有 12 条路由与 38 个 API 端点全部保留，导航只重新分组 + 少量新增聚合页。

## 导航结构

```
工作台     /overview          项目工作台（重构为指挥中心）
测试设计   /intents           TestIntent（含探索入口说明）
          /candidates        Candidate 审核（复用 cases 过滤 candidate 状态）
测试资产   /cases             正式 TestCase
          /cases/:id         用例详情（含 Oracle/断言/版本）
          /cases/:id/versions 版本历史
执行中心   /tasks             Task（队列）
          /runs              Run 列表（Task→Run 层级）
          /runs/:id          Run 详情（Tab: 概览/Steps/断言/诊断/报告）
证据中心   /events            Event（编排 + 证据两源）
          /evidence          Evidence 记录（含完整性）
          /graph             Evidence Graph（真实图渲染）
智能诊断   /diagnosis         诊断（含 Hypothesis/Probe）
          /probes            Probe 审计
修复中心   /repairs           修复提案（Diff/沙箱验证/审批/晋升门禁说明）
评测       /benchmark         UI-FaultBench（数据集/实验/指标）
系统       /system            服务状态 + 审计（新增，聚合既有能力）
```

变更说明：
- `/candidates`：新增聚合视图，复用 `/cases` 接口按状态过滤（无新后端）。
- `/system`：新增聚合视图，用 `/health`、`/version`、`/dispatch/status`、`/events/audit`、`/probes/audit` 组装（无新后端）。
- 其余路由保持原有语义与数据流。

## 页面规格要点

### 工作台 /overview
- 顶部 MetricCard 行：Task / Run（24h 窗口如有）/ TestCase / Evidence / 待审批修复 / 待处理诊断。
- 中部双栏：左"最近 Run"（状态、verdict、时长，点击进入详情）；右"系统状态"（platform / teamflow / worker / SSE 连接态，来自 /health + /dispatch/status）。
- 底部：最近 Event 时间线（复用 events 接口，10 条）。
- 统计口径：只展示后端真实返回的计数；无口径的"成功率"不展示。

### 执行中心
- Task→Run→Step 三级钻取路径固定：列表行点击 → 详情 Tab → Step 时间线。
- Run 详情 Tab 高亮当前页；Steps 用时间线（每步状态图标 + 时长 + 定位器摘要）。
- 取消 Run 需 ConfirmDialog；结果以 REST 回读为准。

### 证据中心
- Event：源切换（编排/证据）各自独立分页；SSE 实时帧面板与持久化表明确分离，连接断开显示"实时连接已断开"。
- Evidence：行展开预览（JSON 缩进、SHA-256、来源链）；raw/derived 徽标用 token 色。
- Graph：SVG 力导向布局（简单库无需依赖：半径布局 + 连线），节点按 node_type 着色+图例；点击节点查询子图并在侧栏展示 properties/independence_group/source_ref；边明细接分页。

### 诊断中心
- Hypothesis 卡片：结论 + 置信度说明 + supporting/refuting evidence 链接。
- 顶部提示"Agent 结论仅供辅助，不构成正式 Verdict"。

### 修复中心
- 每个提案独立审批表单（不再共享页面级输入）。
- Diff 双栏视图（before/after）。
- 审批必填校验：approver 非空且 ≠ agent/ai/system；驳回必填 reason。
- 门禁说明置顶：批准≠晋升；Oracle 受保护；沙箱验证结果展示 verdict。

### Benchmark
- 三个区块：数据集（版本/哈希/规模）、实验（状态徽标 NOT_RUN/COMPLETED/BLOCKED）、指标表（每行一个指标：值 + 单位 + 状态；空值显示 NOT_RUN 而非 0）。
- 空状态明确区分"无数据集"与"实验未运行"。
- GT 映射未批准时，准确率类指标行显示 BLOCKED。

### 系统
- 服务状态卡：platform health/version、dispatch 队列、模型提供者（NOT_CONNECTED 直至真实 LLM 接入）、worker 配置。
- 审计：events/audit 与 probes/audit 两表。

## 状态覆盖要求

全部页面实现 Loading / Empty / Error / Partial / Disabled 五态（StateBlock 统一承载）。
