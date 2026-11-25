# TraceGuard — Agentic Web UI Testing & Quality Intelligence

面向 Web UI 的**智能测试与质量分析平台**：真实 Playwright/Chrome 执行内核 +
Event/Evidence 证据链 + 多假设诊断 + 证据图 + Oracle 保护的自愈沙箱 +
UI-FaultBench 评测 + 产品级 Vue3 工作台。

> 设计依据：`docs/design/`（11 篇规格）+ `docs/roadmap/`（十四阶段实施路线）。
> 当前状态：第五轮（R5）—— R5-4.11 工程基线收口 + R5-5.1 事件持久化 + **R5-5.1-R 长稳与事件流最终验收 PASS** + **R5-5.2 场景化 Agentic Testing 基础能力完成** + **R5-5.3 设计评审整改完成（DR1 + DR2）**。
> Python 全量 **626 passed / 1 skipped**、Product E2E 24/24、Browser E2E 150/150、安全回归 93 passed、前端门禁全绿（R5-5.1-R 最终验收，2026-11-25）。
> **Git 状态（三基线分列，不得混用）**：
> 1. **仓库 HEAD（DR2 本轮基线）**：`580f9c8`，分支 `r5-5-product-completion`，17 ahead / 0 behind main；
> 2. **代码功能基线**（最后一次代码变更）：`8819694`（R5-5.3 设计文档提交触发，业务代码零改动）；
> 3. **本轮最新 CI**：run `45453575120`（head_sha=580f9c8），5/5 Job success。
> **历史 CI 证据（独立陈述）**：R5-5.1-R 文档验收基线 `6a21f47`：run `45441655349`，5/5 success；
> R5-5.1-R 门禁证据基于 `60d89e7`（run `45439716584`）——三套证据源分开陈述，不混用。
> 下一步：R5-5.3 Agent 决策与场景能力升级——设计评审整改（DR1 + DR2）已完成，**等待 M0 独立架构评审**，未启动 M1～M5 开发。
> 完整文档导航见 [`docs/00-DOCUMENTATION-INDEX.md`](docs/00-DOCUMENTATION-INDEX.md)。

## 快速启动

```powershell
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 复制环境变量样例（真实密钥不入库）
cp .env.example .env
#   注：产品 Explorer 的 llm 策略当前一律 409（D8 未批准），
#   DEEPSEEK_API_KEY 只对 `scripts/verify_llm_provider.py` 的一次性连通性探测有意义。

# 3. 一键启动平台 API(8000) + TeamFlow(8001) + 前端工作台
.\.venv\Scripts\python.exe -m scripts.serve
```

打开 **http://127.0.0.1:8000** 进入工作台。健康检查：`/health`（8000 平台 / 8001 TeamFlow）。

Linux/macOS 将 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python` 即可。

## 核心能力

| 能力 | 入口 | 说明 |
|---|---|---|
| 真实 UI 执行 | `packages/execution/` | 真 Chrome + Playwright，Verdict 与 execution_status 分离，Trace/Locator/断言证据 |
| 契约与审批门禁 | `packages/contracts/`、`apps/api/workbench.py` | C01～C12 Schema；Candidate 必须人工审批才能晋升 |
| Event → Evidence 链 | `packages/evidence/` | Outbox/ConsumerInbox/缺口检测/内容寻址 Blob，append-only |
| 智能诊断 | `packages/agents/diagnosis.py` | 八维度、多假设、双侧证据、CONFIRMED/INCONCLUSIVE 严格判定 |
| 场景化探索 | `packages/agents/scenarios.py` | 场景注册表（6 场景数据化定义）、校验/查重、`implemented=False` 阻塞登记 |
| 目标断言 | `packages/agents/goal_assertions.py` | 11 类结构化断言 + 组合 + required/optional + 逐项证据，INCONCLUSIVE 不冒充 FAILED |
| 证据图 | `packages/evidence/graph.py` | 可信关联、同源独立性、冲突分析、子图查询 |
| 主动诊断 Planner | `packages/agents/probes.py` | Probe Registry + 审计脱敏 + EIG 评分 + 预算停止 |
| 自愈沙箱 | `packages/execution/sandbox.py` | 子进程隔离 TeamFlow + 真回放 + Oracle 保护（库级实现；产品侧仅提供提案查看 / 批准 / 拒绝，提案创建与晋升未暴露为 API） |
| UI-FaultBench | `benchmark/` | GT 隔离 + 分区版本 + 三类评测 + 消融（LLM 基线 `NOT_RUN`，无真实模型对照） |
| URL 安全 | `packages/core/url_guard.py` | SSRF / DNS rebinding / redirect 防护 |
| 工作台 | `apps/web/` | Vue3 + TS，15 路由，设计系统 token，SSE 实时状态 |

## 有一条命令的最小闭环演示

无需手动起服务，脚本自己启动 TeamFlow、用真 Chrome 跑场景并打印闭环结果：

```powershell
.\.venv\Scripts\python.exe -m scripts.demo
```

正常路径 → `PASSED`；UI 撒谎注入 → `PASSED` 但 UI 断言失败被记录；DB 故障 → `FAILED`。
报告 JSON 落在 `data/runs/`。

## 测试与质量

```powershell
.\.venv\Scripts\python.exe -m pytest tests/    # 626 passed / 1 skipped（R5-5.1-R 最终验收口径）
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy packages apps benchmark
cd apps/web; npm run build; npx vue-tsc --noEmit

# 统一门禁（推荐）：quick / python / frontend / security / product / browser / all（默认 quick）
.\.venv\Scripts\python.exe scripts\verify_all.py --mode all
```

- 全量测试：**626 passed / 1 skipped**（627 项收集；R5-5.1-R `gates/python2`，2026-11-25）。历史快照：601/1/2（R5-4.11~R5-5.2 时期，2 项 junction 用例受宿主批量删除守卫误杀，单独重跑全过）、484/530（更早）
- 统一门禁：`scripts/verify_all.py`（每轮唯一输出目录 + 独立测试库；PASS/FAIL/SKIP/BLOCKED/EMPTY/NOT_RUN 六态，只有 PASS 才算通过；`--out` 必须绝对路径）
- 产品级 E2E：**24/24**（`scripts/e2e_product_flow.py`，真实浏览器 + 隔离 SUT）
- 真实浏览器 UI 验收：**150/150**（15 路由 ×（5 分辨率横向溢出 + 4 项逐路由检查）+ 7 项深链 + 8 项附加，`scripts/ui_acceptance.py`）
- 场景化 Agentic Testing：`tests/test_agentic_scenarios.py` **34 项**（含 8 项真实 Chrome 执行层；6 场景中 4 个有真实浏览器证据）
- 安全专项：**93 passed**（`verify_all --mode security`，R5-5.1-R `gates/security2`）。历史记录 92 passed + 1 skipped / 92 passed + 1 failed 保留，差异来自宿主批量删除守卫是否触发，不是口径选择
- GitHub Actions：五 Job（Python / Web / Security / Product E2E / Browser E2E）。**证据源分列，不得混用**：
  1. R5-5.1-R 文档验收：提交 `60d89e7`，run `45439716584`，5/5 success（验收证据 `.traceguard-tmp/r5/r55r-final/`）；
  2. R5-5.1-R 报告收口 HEAD `6a21f47`：run `45441655349`，**已完成**，5/5 success；
  3. R5-5.3 DR1 提交 `8819694`：run `45446643591`，**已完成**，5/5 success；
  4. **当前仓库 HEAD `580f9c8`（DR2 本轮基线）**：run `45453575120`，**已完成**，5/5 success。
- 当前代码功能基线：R5-5.2 收口（6 场景注册表、11 类断言、ScriptedAgentPolicy、Trace Hash v2）；
  R5-5.3 设计文档版本：**DR2 整改后**（含 TestCaseVerdict 分离、执行授权凭证、Secret Reference、
  数据清理租约、Path Spec 三概念、scenario_digest、Hash v3 唯一清单、D8 口径统一）。
- 阶段报告：`docs/r5/`（R5-5.1-R 长稳验收、R5-5.1-R5-5.2 源码整改、R5-5.3 设计文档等）

## 阶段状态冻结（R5-5.2 收口，2026-11-25）

| 模块 | 已完成 | 未完成 | 当前限制 |
|---|---|---|---|
| EventStore | 持久化、回放、恢复、保留窗口裁剪、跨重启实例游标 | 生产级扩展（多进程/分布式） | 单机 SQLite |
| SSE | 实时投递、v2 游标恢复、缺口识别、多客户端并发、故障区间分离 | 更大规模部署 | 单机 |
| Explorer | 目标驱动探索、ToolGateway、Trace Hash v2、Compiler 硬准入、场景注册表驱动 | 自主 LLM 决策 | 决策来自 ScriptedProvider / ScriptedAgentPolicy |
| Scenario | 注册、查重、结构校验、4 场景真实浏览器验证 | project-edit / form-validation 两场景；每场景四类用例矩阵全量 | 2 场景 `implemented=False`（阻塞条件已登记） |
| Goal Assertion | 11 类结构化断言、组合断言、required/optional、逐项证据、读取时复算 | 表格行数、列表计数、元素属性、多元素关系、状态转换等复杂断言 | 观测层 `text_excerpt` 3000 字符截断、元素标签 ≤60 字 |
| Candidate | 生成、人工审核门禁（`review_confirmed` + `uncertainty_acknowledged` 服务端强制） | 身份认证 | reviewer 为本地审计字段，非可信身份 |
| Repair | 沙箱与库级能力（提案生成、CAS 晋升在库级实现） | 产品完整闭环 | 产品侧仅 4 个路由（查看/批准/拒绝），无创建提案与晋升路由 |
| Benchmark | 评测框架、GT 隔离、分区版本、三类评测、消融 | 正式准确率 | GT 共 6 条全部 `UNRESOLVED`，指标输出 `None(BLOCKED)` |

## 当前已实现能力 / 明确未实现能力

**已实现**：真实 Playwright/Chrome 执行内核；C01～C12 契约与人工审批门禁；Event→Evidence 链（Outbox/缺口检测/内容寻址）；EventStore 持久化 + SSE v2 游标恢复；目标驱动探索（Explorer + ToolGateway + Trace Hash v2 + Compiler 硬准入）；场景注册表（6 场景定义 / 4 场景真实浏览器验证）；结构化 Goal Assertion（11 类）；八维度多假设诊断；证据图；Probe Planner；自愈沙箱（库级）；UI-FaultBench 框架；SSRF/DNS rebinding 防护；Vue3 工作台（15 路由 + SSE）。

**明确未实现（不冒充）**：真实 LLM 推理（D8 未批准，`llm` 策略一律 409）；project-edit / form-validation 场景（SUT 缺编辑路由 / 缺可重复种子）；Candidate 身份认证（L2）；Repair 产品闭环（创建/晋升路由）；Benchmark 正式准确率（GT 未批准）；S3BlobStore（L4）；驱动级即时取消（L3，动作超时兜底）；分布式任务队列。

## 下一阶段：R5-5.3 Agent 决策与场景能力升级（DR2 整改完成，等待 M0 评审）

将 ScriptedProvider 为主的探索体系升级为严格约束下可自主决策的 Agentic Testing 系统。设计与准入材料已完成 DR1 + DR2 两轮整改：

- [`docs/r5/R5-5.3-总体设计与实施规格.md`](docs/r5/R5-5.3-总体设计与实施规格.md) —— 场景体系 / Goal Assertion（含 TestCaseVerdict 分离）/ 决策引擎（含执行授权凭证）/ Trace / 架构图 / M0～M5
- [`docs/r5/R5-5.3-接口与数据契约台账.md`](docs/r5/R5-5.3-接口与数据契约台账.md) —— 8 个接口逐项输入输出/权限/错误处理/测试要求
- [`docs/r5/R5-5.3-D8-真实LLM接入审批准备.md`](docs/r5/R5-5.3-D8-真实LLM接入审批准备.md) —— 调用范围/费用/隐私/风险/回滚（口径已统一），等待 D8 审批
- [`docs/r5/R5-5.3-开发任务与验收矩阵.md`](docs/r5/R5-5.3-开发任务与验收矩阵.md) —— M0～M5 任务、依赖、验收门槛、风险清单
- [`docs/r5/R5-5.3-DR2-第二轮设计一致性与安全准入整改记录.md`](docs/r5/R5-5.3-DR2-第二轮设计一致性与安全准入整改记录.md) —— DR2-01～DR2-12 整改记录（含待确认决策）

**未取得 M0 评审结论前不进入 M1～M5 正式开发**；D8 批准前不启用真实模型、不解禁 409。

## 状态纪律（不冒充完成）

- 未批准 GT 映射前，Benchmark 准确率指标输出 `None(BLOCKED)`，不产生假数字（GT 共 6 条记录、全部 `UNRESOLVED`：1 条基线 + 5 个故障场景，尚无人工批准映射）
- LLM 不决定 CONFIRMED / 审批；GT 不暴露给 Agent；INCONCLUSIVE 不计 PASSED
- 真实 DeepSeek 推理保持 **BLOCKED**（D8 未批准）：Explorer 的 `llm` 策略返回 409，Scripted 结果标注 `NOT_REAL_INFERENCE`
- 外部网站/应用测试 **BLOCKED**（OriginPolicy 仅放行本地已批准 origin）
- 30 分钟长稳已执行两轮（R5-5.1-R：31 分钟、7,436 事件、0 缺口、0 重复；R5-7 复核：10/10 链 PASSED、内存无漂移，隔离 TeamFlow 端口 8765）
- 浏览器崩溃 / 页面消失 → 步骤记 `BLOCKED`、Run 记 `execution_status=FAILED`，不产出 PASSED/FAILED 业务结论
- 脚本执行完（`PLAN_EXHAUSTED`）不等于目标完成；只有 Goal Evaluator 能给 `GOAL_VERIFIED`
- 未实现场景返回 `UNSUPPORTED_SCENARIO` 并附 `blocked_reason`，不回放其他场景脚本冒充成功

## 目录结构

```
apps/api          平台主服务（FastAPI）：工作台 API、治理面、Benchmark Facade
apps/teamflow     被测应用：项目 CRUD + 可控故障注入 + 独立状态查询 + Eval 控制面
apps/web          Vue3 + TS 产品级工作台（15 路由 + 设计系统）
apps/workers      占位（仅 1 行 docstring，无任何实现；生产异步 Worker 需 Redis/Celery）
packages/contracts C01～C12 契约 Schema 与版本治理
packages/core      配置、结构化日志、错误、数据库基座、UrlGuard
packages/execution 执行内核、Oracle、Verdict、RepairSandbox
packages/agents    Explorer / Compiler / Diagnosis / Probe Planner / LLM Provider
packages/evidence  Event / Evidence / Outbox / 内容寻址存储
benchmark/         Dataset / GT / 三类评测 / 消融 / 持久化
tests/             627 项单元与集成测试（626 passed / 1 skipped）
infra/             Docker Compose（PostgreSQL 可选）
docs/              规格设计、调研、每轮交付与验收报告（见文档索引）
scripts/           serve / demo / run_benchmark / ui_acceptance / verify_llm_provider
```
