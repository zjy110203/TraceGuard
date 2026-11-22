# TraceGuard 交付报告（自主会话阶段成果）

> 生成时间：2026-11-22（GMT+8，日期以系统时钟为准）
> 性质：**如实的阶段性交付报告**，不是"全部完成"声明。
> 所有测试数量、版本、路径来自真实运行与代码，未编造。
> 基线真值目录：`D:/桌面/TraceGuard/.workbuddy-run/`（progress/phase-status/test-results/changes/decisions/safety-log/resume/current-task）。

---

## 0. 结论摘要（先读这里）

- 本会话从真实基线（阶段 0/1 + 阶段 2 最小闭环，54/54 测试）出发，连续实现并验收了**阶段 2 收口、3、4、5、6、7、8、9、10、11、13 全链路**。
- 全量测试：**151 passed, 1 warning in 190.31s**；ruff/format 全过；mypy 47 源文件 Success。均为真实执行（含真 Chrome 驱动真 TeamFlow），非 mock。
- 阶段 5 的 **LLM 决策循环未接入**（环境无任何 LLM 凭据，只查变量名确认，未硬编码/输出凭据）；确定性工具层与可注入 Policy 接口已实现并测试。
- 阶段 12 **保持 BLOCKED**：无明确授权的外部 Web 应用；未做外部泛化，也未编造安全/性能/可靠性数字。
- 故**不宣称" TraceGuard 全部完成"**。已达成的阶段均有 READY_FOR_REVIEW 级证据，待人工评审冻结后才可置 ACCEPTED。

---

## 1. 项目最终架构（本会话落地范围）

```
模块化单体 + 进程内异步 worker（Celery/Redis 的诚实 dev/test 替代）
┌ apps/teamflow       被测应用：登录/Workspace/项目 CRUD、5 类故障注入、独立状态查询、GT/故障配置 eval 面隔离
├ packages/core       统一配置/日志/错误/DB 基座（SQLite 默认，PostgreSQL 可切换）
├ packages/contracts  C01~C12 注册表 + C05/C06/C11/C12 draft JSON Schema（均非冻结）
├ packages/execution  ★确定性内核
│   ├ verdict         四态 TestVerdict + 冻结聚合（INCONCLUSIVE 兜底，无 Primary 覆盖）
│   ├ models          append-only TestCaseVersion + Oracle + 仓库
│   ├ oracle          只读独立状态/UI 观察，绝不读 GT/eval
│   ├ browser/events  Playwright 执行、Screenshot/DOM/Network/Trace 证据、C05/C06 记录
│   ├ runner          自动采观察 → Oracle → Verdict → Run/Step 报告
│   ├ tasks/orchestrator  Task(逻辑)/Run(append-only,attempt)/Step + 状态机 + 进程内 worker + SSE
│   ├ repair          Oracle-Protected Self-Healing：11 态 + 断言/Oracle 保护 + CAS 晋升 + 回滚
│   └ pipeline        阶段13 全链路：TestCase→执行→证据→图→诊断→Probe→处置→修复
├ packages/evidence   EventStore/Outbox/ConsumerInbox/缺口检测/EvidenceManager/CorrelationGraph（关系表，不引入图数据库）
├ packages/agents     诊断 v1（八维度/多假设/确定性 CONFIRMED）、Explorer 工具层、Compiler、Probe Planner
├ apps/api            health/version + workbench REST/SSE + evidence/graph 查询 API
├ benchmark           UI-FaultBench：GT 隔离/数据集分区/三类评测/指标口径/5 消融；13 条用例
└ tests               16 个测试文件，151 用例
```

四项结果维度始终分离：`execution_status` / `test_verdict` / `diagnosis_status` / `diagnosis_outcome`。诊断字段在非诊断流程保持 `NOT_STARTED`。

---

## 2. 十四阶段完成情况（真实状态）

| 阶段 | 状态 | 核心实现 | 证据 |
|---|---|---|---|
| 0 工程初始化 | ACCEPTED(基线) | 骨架/依赖/测试/CI | 架构说明与M0-M1退出证据.md |
| 1 TeamFlow + 故障注入 | ACCEPTED(基线) | 5 类故障、GT eval 面隔离、数据重置 | 同上 |
| 2 专业 UI 自动化框架 | READY_FOR_REVIEW | 13 用例、Trace、Locator 优先级、参数化、冻结 Verdict | 架构说明与阶段2-4退出证据.md；test-results.md |
| 3 调度编排与工作台 | READY_FOR_REVIEW | Task/Run/Step、状态机、进程内 worker、SSE、REST | test_orchestration.py（6 项） |
| 4 Event 与 Evidence | READY_FOR_REVIEW | 幂等/Outbox/Inbox/缺口/完整性/脱敏/生命周期 | test_evidence.py（13 项） |
| 5 Explorer Agent | **IN_PROGRESS** | 工具层/预算/安全/Trace/真探索；**LLM 决策循环缺凭据未接入** | test_explorer_compiler.py（8 项） |
| 6 TestCase Compiler | READY_FOR_REVIEW | 确定性编译 + Candidate + 人工审批门禁 | 同上 |
| 7 Diagnosis v1 | READY_FOR_REVIEW | 八维度 + 多假设 + 确定性 CONFIRMED + Disposition | test_diagnosis.py（9 项） |
| 8 Evidence Graph | READY_FOR_REVIEW | 关系表图 + 可追溯边 + 同源独立性 + 冲突 | test_graph.py（9 项） |
| 9 Active Diagnosis Planner | READY_FOR_REVIEW | 五项校验 + 审计 + 脱敏 + EIG 评分 + 预算 | test_planner.py（9 项） |
| 10 Oracle-Protected Self-Healing | READY_FOR_REVIEW | 11 态 + 断言/Oracle 保护 + 沙箱 + CAS + 回滚 | test_repair.py（9 项） |
| 11 UI-FaultBench | READY_FOR_REVIEW | GT 隔离 + 三类评测 + 8 指标口径 + 5 消融 | test_benchmark.py（8 项） |
| 12 外部泛化/安全/可靠性 | **BLOCKED** | 无外部应用授权；未做外部泛化，未编造报告 | decisions.md |
| 13 全链路集成与十场景 | READY_FOR_REVIEW | pipeline + 10 场景真执行证据 | test_full_chain.py（11 项） |

状态说明：READY_FOR_REVIEW 表示证据齐全但**未经人工评审冻结**；按总控指令，只有具备验收证据且经评审的 ACCEPTED 才算阶段完成。本会话不擅自将自评阶段标为 ACCEPTED。

## 3. 六里程碑验收

| 里程碑 | 状态 | 说明 |
|---|---|---|
| M0 工程环境 + TeamFlow | ACCEPTED(基线) | 既有退出证据 |
| M1 专业 UI 自动化平台 | READY_FOR_REVIEW | 阶段 2 + 3 达标，待评审 |
| M2 证据基础设施 + Agentic Testing | IN_PROGRESS | 阶段 4/6 达标；阶段 5 LLM 循环缺凭据 |
| M3 Evidence Graph + 智能诊断 | READY_FOR_REVIEW | 阶段 7/8/9 达标，待评审 |
| M4 安全测试自愈 | READY_FOR_REVIEW | 阶段 10 达标（沙箱为结构校验，真回放留集成），待评审 |
| M5 Benchmark + 泛化 + 安全 + 交付 | IN_PROGRESS | 阶段 11/13 达标；阶段 12 BLOCKED |

## 4. C01～C12 契约状态

| 契约 | 状态 | 位置 |
|---|---|---|
| C01 TestIntent / C02 TestCase / C03 Oracle / C04 Execution | 领域模型实现，JSON Schema **未冻结** | `packages/execution/models.py`, `verdict.py`, `tasks.py`；registry=placeholder |
| C05 Event / C06 Evidence | **draft schema** | `packages/contracts/C05_Event.schema.json`, `C06_Evidence.schema.json` |
| C07 Evidence Graph | 实现，Schema 未冻结 | `packages/evidence/graph.py`；registry=placeholder |
| C08 Diagnosis / C09 Probe / C10 Disposition | 实现（枚举/模型），Schema 未冻结 | `packages/agents/diagnosis.py`, `probes.py` |
| C11 Repair | **draft schema** | `packages/contracts/C11_Repair.schema.json` |
| C12 Benchmark | **draft schema**（基线既有） | `packages/contracts/C12_Benchmark.schema.json` |

registry.json 中 C05/C06/C11/C12 标 `draft`，其余 `placeholder`；**无任何契约标 frozen**。不冒充冻结。

## 5. 技术栈与依赖版本（本地实测，P01 仍待确认）

Python 3.13.14｜FastAPI 0.165.0｜uvicorn 0.54.0｜Pydantic 2.14.3｜SQLAlchemy 2.1.9｜httpx 0.28.1｜Playwright 1.65.2（系统 Chrome `channel="chrome"`，不下载二进制）｜pytest 9.2.0｜ruff 0.17.5｜mypy 1.20.2。
未安装/未使用：celery、redis、sse-starlette、MinIO、LangGraph、Browser Use、任何 LLM SDK。Docker daemon 未启动。
数据库：SQLite 默认（`data/traceguard.db`），SQLAlchemy 方言中立，PostgreSQL 可换 URL（**未实连验证**）。

## 6. 核心模块实现情况与测试数量

全部 16 个测试文件，合计 **151 个真实测试**（54 基线 + 97 本会话新增）：

| 模块 | 测试文件 | 用例数 |
|---|---|---|
| 核心/平台 API | test_core.py(4), test_platform_api.py(2) | 6 |
| 执行模型/Verdict | test_execution_models.py(7), test_verdict.py(29) | 36 |
| TeamFlow 阶段1 | test_teamflow_stage1.py | 8 |
| 阶段2 E2E + 用例库 | test_e2e_stage2.py(4), test_case_library.py(15) | 19 |
| 阶段3 编排 | test_orchestration.py | 6 |
| 阶段4 证据 | test_evidence.py | 13 |
| 阶段5/6 Explorer+Compiler | test_explorer_compiler.py | 8 |
| 阶段7 诊断 | test_diagnosis.py | 9 |
| 阶段8 图 | test_graph.py | 9 |
| 阶段9 Planner | test_planner.py | 9 |
| 阶段10 自愈 | test_repair.py | 9 |
| 阶段11 Benchmark | test_benchmark.py | 8 |
| 阶段13 全链路十场景 | test_full_chain.py | 11 |

（上方分项合计与各文件实际收集数一致；权威数字以 `pytest -q` 输出 151 passed 为准。）

## 7. 关键技术与实现要点

- **冻结 Verdict**：无 Primary 覆盖策略；冲突/证据缺失/Oracle 不可用 → INCONCLUSIVE；空必需断言集非法。
- **Trust boundary**：Oracle 只读独立业务状态与 UI 观察；永不读 `/__eval__`、Ground Truth、故障配置。诊断可读 eval 平面事件，但仅作诊断证据，**不参与 Verdict**。
- **Event 可靠性**：至少一次投递 + Consumer Inbox 幂等；`idempotency_key` 同键异载荷冲突审计；sequence 按实例；缺口标 PENDING_CONFIRMATION 不伪造丢失；可信 workspace 注入（客户端伪造被覆盖）。
- **证据分层**：raw/normalized/derived 严格分离，derived 必带来源与规则版本；SHA-256 与 source_trust 独立；脱敏不改原始哈希；生命周期只报告不删除。
- **关系图**：仅从校验过的 context_refs 建关联边；时间接近不建因果；direct_causal 必带显式依据；同源派生只算一个独立源；supports/contradicts 双侧保留。
- **确定性诊断**：8 独立维度；多假设双侧证据；CONFIRMED 由命名规则（R-DB/API/NET/UI/TIME/LOC）+ 权威事件触发，无 LLM 参与；证据不足 INCONCLUSIVE；Flaky 是稳定性特征非根因。
- **安全自愈**：允许 locator/等待/步骤/数据引用修复，禁止改产品代码/正式 Oracle/删必需断言；晋升用 CAS 校验基础版本，VERSION_CONFLICT 不覆盖新版本；人工审批不可绕过。
- **Benchmark**：GT 独立管理器不暴露给 subject；Trusted/Agent 分区 + 版本冻结；8 指标均含 unit/分子/分母/排除/缺失/多根因/波动口径；INCONCLUSIVE 不计 PASSED；无凭据的 LLM 基线标 NOT_RUN。

## 8. 测试与 Benchmark 实验结果

- 全量门禁（末次真实运行）：`pytest -q` → **151 passed, 1 warning in 190.31s**；`ruff check` All checks passed；`ruff format --check` 76 files already formatted；`mypy packages apps benchmark --ignore-missing-imports` Success: 47 source files。
- E2E 全链路十场景（test_full_chain.py，真 Chrome + 真 TeamFlow，11 项通过）覆盖：
  1 正常创建（PASSED）｜2 UI 成功但持久化失败（FAILED/PERSISTENCE/PRODUCT_DEFECT）｜3 API 500（FAILED/API）｜4 网络超时（真实记录）｜5 Locator 变化（步骤失败、不自动确认/修复）｜6 异步时序｜7 目标不可达（NOT_EVALUATED，绝不误判 PASSED）｜8 多源证据冲突（Oracle 分歧不自动 PASSED，双侧证据保留）｜9 Probe 越权（拒绝+审计）｜10 测试脚本修复（审批后 CAS 晋升 v1→v2，断言/Oracle 未变）。
- Benchmark 实验：本次实现的是**可重复评测框架与指标口径**（确定性基线可运行）；**未运行真实 LLM 对照实验**（无凭据），其基线在报告中如实标 NOT_RUN，未编造 Macro-F1/准确率数字。可复现命令见第 12 节。

## 9. 安全审计结果（本地可测部分）

已验证：越权 caller/越界 workspace/任意 SQL 全部被拒并审计；页面注入文本不能提升工具权限；敏感字段脱敏；Trace/事件/证据 sha256；凭证未入代码/日志/报告（`.env` 仅查键名，未读真实值）；GT 与业务面隔离；工具白名单 + 预算 + 停止条件；无任意 shell/文件访问。
未覆盖（阶段 12 BLOCKED）：SSRF/DNS Rebinding/Redirect/Prompt Injection 的系统性外部攻击面、跨真实账号越权、对象存储异常、Redis/DB 故障注入、Worker 重启恢复、外部 Outbound 限制——**未做，不冒充通过**。

## 10. 性能与可靠性

- 实测规模：151 用例、13 条测试用例、单次全链路 10 场景约 120s、完整测试套件约 190s（含多次真 Chrome 启动）。
- 未建立生产 SLO、并发上限、资源配额的量化基线（阶段 12 要求"根据实测确定"，本环境无外部 broker/多浏览器并发场景），故**不编造数字**。

## 11. 部署与启动

依赖已在项目内 `.venv`。启动命令（Windows，工作区 `D:/桌面/TraceGuard/traceguard`）：

```
.\.venv\Scripts\python.exe -m uvicorn apps.teamflow.main:app --app-dir . --port 8001
.\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --app-dir . --port 8000
```
平台 API 默认装配 workbench + evidence + graph（Settings.workbench_enabled=True），TeamFlow URL 由 `teamflow_host/port` 推导。
Docker Compose 配置存在于 `infra/docker-compose.yml`，但 daemon 未启动，**未做容器化验证**。

## 12. 完整测试命令与演示

```
cd "D:/桌面/TraceGuard/traceguard"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy packages apps benchmark --ignore-missing-imports
.\.venv\Scripts\python.exe -m scripts.demo                     # 最小闭环一键演示（真 Chrome）
.\.venv\Scripts\python.exe -m pytest -q tests/test_full_chain.py  # 十场景全链路
```

接口说明：平台 API `/docs`（OpenAPI 自动生成）含 `/health`、`/version`、`/api/v1/{cases,tasks,runs,events,evidence,graph}`；TeamFlow `/docs` 含业务 API 与 token 门控的 `/__eval__` 控制面。

## 13. 未完成事项 / 已知限制 / 后续建议

**未完成**
1. 阶段 5 LLM 决策循环（需 LLM 凭据；Policy 接口已就绪，接入后需做 Browser Use/Playwright PoC 与真实推理评测）。
2. 阶段 12 外部应用泛化、系统性安全/性能/可靠性报告（需外部应用书面授权与更完整环境）。
3. C01-C04/C07-C10 的正式 JSON Schema 冻结与契约测试（当前为领域模型 + draft）。
4. Alembic 迁移脚本与 PostgreSQL 实连验证；Docker 容器化验证。
5. 正式 Oracle / 正式 TestCase / RepairProposal 的人工审批记录与评审冻结（不自动绕过）。
6. 真实 LLM 对照实验与 Macro-F1 等量化结果（无凭据，未编造）。

**已知限制**
- 进程内 worker 是 Celery/Redis 的 dev/test 替代；进程退出后排队任务不持久，不满足生产可用性。
- 阶段 10 沙箱校验是结构/断言/Oracle 守卫，完整真回放晋升留待与阶段 13 集成深化。
- `data/runs/` 的历史报告为过去会话产物，本报告数字均来自本次运行日志，未复用旧数据充数。

**后续建议（按依赖顺序）**
1. 提供受控 LLM 凭据 → 接入 Explorer Policy → 完成阶段 5 并做 Browser Use PoC。
2. 指定人工审批角色与记录格式 → 冻结正式 Oracle/TestCase/Repair 审批路径。
3. 取得 1~2 个授权外部应用 → 执行阶段 12 泛化与安全/可靠性验证（SLO 以实测为准）。
4. 补 Alembic + PostgreSQL 实连 + Docker 验证，完成部署文档。
5. 运行真实 LLM 对照与消融实验，填充 Benchmark 量化结果（INCONCLUSIVE 不计 PASSED）。

---

**最终声明**：本交付物如实反映当前进度——11 个阶段具备 READY_FOR_REVIEW 级真实证据，阶段 5 部分完成，阶段 12 阻塞。未将任何未通过项改为通过，未以 Mock/固定输出冒充真实运行，未编造实验数据，未提前宣布"全部完成"。
