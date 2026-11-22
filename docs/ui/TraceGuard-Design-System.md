# TraceGuard 设计系统 v1.0

- 依据：R4-UI-UX-Audit.md；视觉方向参考 Linear（导航/任务）、Sentry（事件详情/诊断）、Datadog（指标/监控）、GitHub Actions（Step 时间线/运行历史）的设计思想，不复制界面。
- 原则：信息密度优先、状态语义明确、不靠颜色单独传达状态、复用现有技术栈（无 UI 库、纯 CSS tokens + 少量通用组件）。

## 1. 色彩 Tokens

| Token | Light | Dark | 语义 |
|---|---|---|---|
| `--tg-bg` | `#f7f7f5` | `#171716` | 页面背景 |
| `--tg-panel` | `#ffffff` | `#202020` | 卡片/面板表面 |
| `--tg-panel-2` | `#f2f2ef` | `#2a2a29` | 次级表面（表头、代码块） |
| `--tg-border` | `rgba(0,0,0,.14)` | `rgba(255,255,255,.15)` | 边框 |
| `--tg-border-strong` | `rgba(0,0,0,.28)` | `rgba(255,255,255,.30)` | 强调边框 |
| `--tg-text` | `#1f1e1c` | `#ecebe8` | 正文 |
| `--tg-muted` | `#6f6d69` | `#a5a4a0` | 次要文字 |
| `--tg-accent` | `#185fa5` | `#7fb6e8` | 主色：链接、选中、主按钮 |
| `--tg-accent-bg` | `rgba(24,95,165,.10)` | `rgba(127,182,232,.16)` | 选中背景 |
| `--tg-success` | `#1d7a3d` | `#7ed49a` | PASSED / 健康 |
| `--tg-danger` | `#b3261e` | `#ef9a9a` | FAILED / 错误 / 破坏性操作 |
| `--tg-warn` | `#8a5a00` | `#f2c14e` | WARNING / PENDING / NOT_RUN |
| `--tg-info` | `#5b4ea6` | `#b3a8ec` | INFO / 中性提示 |

规则：
1. 状态永远"颜色 + 文字"双通道（StatusPill 必须带文字标签）。
2. PASSED=绿、FAILED=红、PENDING/BLOCKED/NOT_RUN=黄、CANCELED/ROLLED_BACK=灰、IN_PROGRESS=蓝。
3. 所有正文对比度 ≥ 4.5:1，次要文字 ≥ 3:1。

## 2. 字体与间距

- 字号体系：`12px`(辅助) / `13px`(表格/正文密) / `14px`(正文) / `16px`(页面标题) / `22px`(指标大数)。全站不再出现 11px。
- 字重：400 正文、500 标题与强调、600 仅页面主标题。
- 行高：正文 1.6，表格 1.5。
- 间距刻度：4 / 8 / 12 / 16 / 24 / 32。
- 页面容器：`max-width 1280px`（数据表格页可全宽），边距 `24px 28px`。
- 卡片：`border-radius 8px`，padding `16px`，间距 `16px`。
- 控件高度：input/select/button 统一 `30px`（紧凑）。
- 表格密度：行高 `36px`，cell padding `6px 10px`。

## 3. 图标

使用内联 SVG（stroke 1.5px, 16px 网格），仅导航分组与状态标识使用，不超过 12 个图标；不引入图标库依赖。

## 4. 组件清单（本轮实际建立）

| 组件 | 文件 | 说明 |
|---|---|---|
| AppShell | App.vue | 侧边栏 + 顶栏 + 内容区；顶栏含面包屑、全局刷新、服务状态点 |
| Sidebar | App.vue | 分组导航（八组），当前路由高亮 |
| Breadcrumb | App.vue | 由路由段生成，可点击回退 |
| PageHeader | `components/PageHeader.vue` | 标题 + 副标题 + 操作区（统一 `.head`） |
| StatusBadge | `components/StatusPill.vue`（扩展） | 颜色+文字双通道 |
| MetricCard | `components/MetricCard.vue` | 大数 + 标签 + 可点击 |
| DataTable | 全局 table 样式 + Pagination | 密度统一、排序后续按需 |
| FilterBar | `components/FilterBar.vue` | 筛选控件行（带 label） |
| Timeline | StepsView 内实现 | Step 执行时间线（GitHub Actions 风格） |
| EvidencePreview | EvidenceView 内实现 | 截图/JSON/文本预览 + SHA-256 展示 |
| EvidenceGraph | GraphView 内实现 | SVG 节点-边图 + 联动子图 |
| DiffViewer | RepairsView 内实现 | before/after 双栏 |
| EmptyState / LoadingState / ErrorState | StateBlock.vue（扩展） | 三态 + NOT_IMPLEMENTED/NOT_CONNECTED 专用文案 |
| ConfirmDialog | `components/ConfirmDialog.vue` | 替代 alert()，用于取消 Run/批准修复 |
| Pagination | Pagination.vue（沿用） | 统一页码样式 |

禁止为凑组件数过度抽象；单视图专用的结构留在视图内。

## 5. 状态语义（全站统一）

| 标记 | 含义 | 展示 |
|---|---|---|
| `NOT_IMPLEMENTED` | 功能存在但后端未实现 | 灰徽标 + 说明，按钮禁用 |
| `NOT_CONNECTED` | 数据源未接入（如真实 LLM） | 黄徽标 + 原因 |
| `NOT_RUN` | 实验未执行 | 黄徽标，不计入任何统计 |
| `BLOCKED` | 等待人工门禁 | 黄徽标 + 门禁说明 |
| `PARTIAL` | 部分数据可用 | 蓝徽标 + 缺口提示 |

## 6. 交互质量基线

每个页面必须覆盖：Loading / Empty / Error / Success / Partial / Disabled。
- 请求失败必须可见（区分"失败"与"空"），提供重试。
- 所有按钮有真实行为；未接入功能明确标记而非隐藏。
- SSE 只做实时提示与失效信号，持久化状态以 REST 为准。
- 前端不做 Verdict 判定、不绕过审批与 Oracle 保护（现状已满足，保持）。

## 7. 响应式断点

- `≥1440`：标准布局。
- `1280~1439`：标准布局，卡片列数自适应。
- `1024~1279`：侧边栏收窄为图标+文字紧凑模式（192px→56px 折叠可手动展开）。
- `<1024`：侧边栏折叠为顶部抽屉；表格横向滚动。
- `390×844`（移动）：仅保证可读不崩溃（表格横向滚动、表单单列），非主要使用场景。

## 8. 可访问性

- 全部表单控件配 `<label>`（sr-only 可）。
- 可点击卡片用 `<button>` 或补 role/tabindex/Enter 处理。
- 焦点环：`:focus-visible` 统一 2px accent 描边。
- 颜色不单独传达状态（见 §1 规则 1）。
