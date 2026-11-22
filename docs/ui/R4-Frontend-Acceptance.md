# R4 前端验收报告

- 时间：2026-11-22（R4-1.5）
- 方法：真实浏览器（本机 Chrome，Playwright headless），目标 http://127.0.0.1:8000（FastAPI 托管构建产物，非 vite dev、非 mock）。
- 脚本：`scripts/ui_acceptance.py`（可重复执行）。
- 结果：**65/65 检查通过，0 失败**（首轮 64/65，390px 溢出已修复后复验通过）。

## 1. 覆盖范围

| 检查类别 | 内容 | 结果 |
|---|---|---|
| 主路由加载 | 14 条路由（12 原有 + candidates + system）全部 200、非空白 | PASS |
| Console / Page Error | 每条路由收集 console.error 与未捕获异常 | 全部为 0 |
| API 失败 | 每条路由收集 ≥400 的 /api/ 响应 | 全部为 0 |
| 交互 | MetricCard 点击导航、面包屑渲染、Graph 节点点击（图为空，验证空态）、Benchmark NOT_RUN/空态、Repairs 页面可达 | PASS |
| 响应式 | 1440×900 / 1024×768 / 390×844 无横向溢出 | PASS（修复 390px 表格溢出后） |
| 基础无障碍 | Tab 键焦点进入文档；全表单控件补 label | PASS |
| 截图 | docs/ui/screenshots/*.png（14 路由 + 响应式抽查） | 已生成 |

## 2. 真实数据验证

- 工作台首页展示真实后端数据：Task 3 / Run 3 / TestCase 13 / Evidence 0（本地库既有数据），Verdict 分布与 Run 状态分布来自 `/api/v1/overview`。
- 无静态假数据：所有页面数据来自真实 fetch；空数据页面显示具体空态文案而非 0 填充。

## 3. 已知限制（如实记录）

1. **改造前截图无法补拍**：项目无版本控制，重构前的构建产物在重新 build 时被覆盖。改造前视觉状态以 `R4-UI-UX-Audit.md` 的文字审计为准。此项标记 NOT_RUN（不可回溯）。
2. Graph 节点点击交互因当前库中关联图为空而跳过（空态验证通过）；需要产生新证据后人工复核。
3. SSE 实时刷新在无活跃 Run 时仅验证连接状态展示，未验证帧推送（R3 已有 SSE 测试覆盖服务端行为）。
4. 审批门禁的后端拒绝路径（agent/ai/system 批准被拒）由后端测试覆盖（test_repair.py），前端只做客户端预校验。

## 4. 本轮前端变更清单（代码）

| 文件 | 变更 |
|---|---|
| src/styles.css | 重写：设计系统 tokens + 全局工具类（原 17 视图重复样式上移） |
| src/router.ts | 重写：hash 查询串解析（修复 `/runs?task=` 落空 P0 bug）+ 新路由声明 |
| src/App.vue | 重写：AppShell（面包屑、可折叠侧边栏、后端/SSE 双状态点） |
| src/api/client.ts | +subscribeEventsSafe（连接态回调）、requestRoot、subgraph depth、health/version/dispatchStatus/evidenceAudit |
| src/api/types.ts | +RunBrief/EvidenceAuditRow；Overview+recent_runs；CaseRow+approved_by |
| 新组件 | PageHeader / MetricCard / ConfirmDialog |
| StatusPill | Token 化配色 + 新状态语义（NOT_RUN/BLOCKED/NOT_CONNECTED 等） |
| 新视图 | CandidatesView（机器候选用例审核）、SystemView（服务状态/调度/审计聚合） |
| 重写视图 | Overview（工作台首页）/ Runs（修 N+1、去调试残留）/ RunDetail（错误语义、ConfirmDialog、Tab 高亮）/ Repairs（独立审批表单、必填校验、Diff 双栏）/ Events（双源独立分页、SSE 连接态）/ Graph（SVG 图渲染 + 子图联动 + 属性面板 + 边分页）/ Benchmark（指标表格化、NOT_RUN 语义、数据集版本展示） |
| 子代理升级 | Intents/Cases/CaseDetail/CaseVersions/Tasks/Steps/Assertions/Evidence/Diagnosis/Probes（label、错误语义、样式上移、键盘可达性） |
| 后端最小增强 | /overview +recent_runs（cap 10，消除 N+1）；/cases +approved_by；vite 代理 +/health /version |

## 5. 检查项逐条结果

完整逐条输出见脚本运行日志（65 项）；本报告 §1 为汇总。
