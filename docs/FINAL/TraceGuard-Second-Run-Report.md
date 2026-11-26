# TraceGuard 第二轮自主工程开发 — 交付报告

- 报告时间：2026-11-22
- 对应指令：《TraceGuard｜第二轮自主工程开发总控指令 v2.0》
- 第一轮报告保留在 `docs/FINAL/TraceGuard-Final-Delivery-Report.md`，本文件不覆盖它
- 本报告所有数字均来自本机真实执行；未执行的项目一律标注，不估算、不编造

---

## 0. 结论摘要（先读这里）

**做成了什么**：把第一轮"核心功能原型"里三处硬缺口补上——契约体系从 4 份 draft 扩到
**12 份全覆盖**、前端从 **27 行静态页**做成 **17 视图的真实工作台**、工程化从 **零迁移** 做到
**可回滚的 Alembic + 增量迁移**；并把任务分发改成有**至少一次/可见性超时/死信**语义的 Broker。

**没做成什么**（明确记录，不掩饰）：
- **P6 Self-Healing 真实沙箱回放未开始**——自愈仍停留在结构校验，未做真 Playwright 回放
- **P7 Benchmark 深化未开始**——数据集/GT 仍未版本化落盘，无真实对照实验
- **P8 安全与可靠性测试仅完成"提示注入"一项**，SSRF/DNS/并发/长稳未做
- **真实 LLM 一次也没调用**（环境无任何凭据）
- **Redis / Celery / PostgreSQL / S3 全部未做真实集成验证**

**验证结果**：`pytest 294 passed`（第一轮 151 → 本轮 294），耗时 207.69s，真 Chrome + 真 TeamFlow；
ruff / format 全过；mypy 61 源文件 Success（含 alembic）；`vue-tsc --noEmit` 无错误。

**不为宣布完成而放水**：本轮新增的 143 项测试里，有 12 项是因测试失败而暴露的真实缺陷修复，
另有 2 项是我自己的错误假设被测试纠正；这些都在第 2.4 节逐条列出。

---

## 1. 第一轮复核（独立重跑，不采信历史日志）

### 1.1 环境与版本控制

| 项 | 实测 |
|---|---|
| 项目根 | `D:/桌面/TraceGuard`（未变化） |
| 代码根 | `D:/桌面/TraceGuard/traceguard` |
| Git | **非 Git 仓库**（根目录与 traceguard 均无 `.git`）→ 依指令不初始化、不建分支、不改历史 |

### 1.2 基线重跑

| 命令 | 结果 |
|---|---|
| `pytest -q` | **151 passed**，1 warning，191.79s |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 77 files already formatted |
| `mypy packages apps benchmark` | Success，47 源文件 |
| `python -m scripts.demo` | 三场景真 Chrome 跑通（PASSED / UI 撒谎 / DB 故障） |

结论：第一轮成果成立且可复现。

### 1.3 代码审计（分类，不混为一谈）

- `grep TODO|FIXME|XXX` → 0 处
- `grep NotImplementedError` → 0 处
- `grep mock|fake|stub` → 仅命中 `locators.py` 的策略名、`orchestrator.py` 的自我声明、`contracts/__init__.py` 说明文字；**无 mock 冒充真实功能**
- 7 处 `pass` 逐条核查：2 处 SQLAlchemy 基类空体、1 处空页容错、4 处异常兜底——均合法

### 1.4 复核发现的实质缺口（本轮立项依据）

| 缺口 | 实测证据 |
|---|---|
| 契约 8/12 为 placeholder | `registry.json`：C01/C02/C03/C04/C07/C08/C09/C10 无 schema、无样例、无语义校验 |
| 前端是空壳 | `apps/web/src` 仅 3 文件 27 行，`App.vue` 硬编码"M0 skeleton"文案，零 API 调用 |
| 无迁移机制 | `alembic` 已装但无 `alembic.ini`、无 `alembic/` 目录 |
| 分发无队列语义 | `submit()` 直接 `ThreadPoolExecutor.submit`，无重投/死信/可见性超时 |
| 无 LLM 抽象 | 只有 `ScriptedPolicy`，无 Provider、无结构化输出、无调用 Trace |
| 附件无处可存 | `EvidenceRow` 无 `attachment_ref` 列（C06 契约有此字段） |
| 文档串过期 | `packages/__init__.py` 称"the rest are placeholders"，与现状严重不符 |

### 1.5 进度统计修正

第一轮对话摘要称"11 个 READY_FOR_REVIEW"，与阶段表不符。逐行核对后真实分布为：
**ACCEPTED 2（基线 0/1）、READY_FOR_REVIEW 10、IN_PROGRESS 1（阶段 5）、BLOCKED 1（阶段 12）**。
已修正 `progress.md` 中阶段 12/13 的过期行。

---

## 2. 第二轮开发

### 2.1 完成项总览

| 编号 | 任务 | 状态 | 真实证据 |
|---|---|---|---|
| P0 | C01~C12 契约体系 | READY_FOR_REVIEW | 12 Schema、101 条语义规则、50 份样例、77 测试 |
| P1 | Vue3 工作台与后端集成 | READY_FOR_REVIEW | 17 视图、3360 行、13 个新端点、4 项真 Chrome UI 测试 |
| P2 | 数据库工程化与迁移 | READY_FOR_REVIEW | 2 个迁移、10 项迁移测试、PG 离线 DDL 校验 |
| P3 | Broker 抽象与死信 | READY_FOR_REVIEW | 已接入编排器、10 项语义测试、`/dispatch/status` |
| P4 | 存储抽象 | READY_FOR_REVIEW | 内容寻址 Blob 存储、10 项测试、补 `attachment_ref` 列 |
| P5 | LLM Provider 与决策循环 | READY_FOR_REVIEW | Provider/结构化输出/Trace/白名单、16 项测试 |
| P6 | Self-Healing 沙箱回放 | **NOT_STARTED** | 见 §6 |
| P7 | Benchmark 深化 | **NOT_STARTED** | 见 §7 |
| P8 | 安全与可靠性 | **PARTIAL** | 仅注入防护；SSRF/并发/长稳未做 |
| INT | 全链路集成 | READY_FOR_REVIEW | 3 项串联真执行测试 |
| DELIVER | 本报告 | 完成 | — |

### 2.2 新增/修改规模

| 项 | 数量 |
|---|---|
| 后端 Python 文件 | 65（本轮前 47 → 65） |
| 后端代码行 | 11,339 |
| 测试文件 | 25 |
| 前端文件 | 28（3 → 28） |
| 前端代码行 | 3,360（27 → 3,360） |
| 契约 Schema | 12 |
| 契约样例 | 50（19 合法 / 19 非法 / 12 语义反例） |
| 迁移脚本 | 2（初始 + 增量） |
| 平台 API 路由 | 39（工作台 11 + 证据 11 + 扩展 14 + 基础 3） |

### 2.3 关键设计决策

1. **契约由代码生成，不手写 JSON**。Schema 从真实 dataclass 推导，`test_on_disk_schemas_match_current_code`
   在 dataclass 改动而 Schema 未重建时直接失败——从结构上消灭"Schema 与代码漂移"。
2. **合法样例由真实对象构造，非法样例由变异生成并逐个验证确实失败**。构建脚本无法构造出
   失败样本时直接 `SystemExit`，避免"样例通过=假覆盖"。
3. **平台迁移只拥有 10 张表**。平台实际横跨 4 个 DeclarativeBase，且 TeamFlow 与平台共用 `Base`；
   若不隔离，平台迁移会连带 `tf_*`/`eval_*`，把两个独立部署的应用耦合在一起。
4. **不声明外键**。`tg_run` 为 append-only，证据/图可跨部署引用 run，硬 FK 会让归档与跨库迁移
   变成删除失败。引用完整性由服务层保证，测试断言引用列均有索引。引入 FK 属 schema 语义变更，
   列为待人工决策。
5. **失败不自动重试**。执行失败的重试必须新建 Run（append-only 历史），这是用户动作而非队列决策；
   失败一律进死信并附原因。
6. **LLM 权限判定写在代码里，不写在提示词里**。模型返回后才校验白名单，页面注入说服模型
   返回越权工具仍会被拒。
7. **前端零 Mock**。所有页面 fetch 真实端点；无数据时显示明确空态，不填示例行。

### 2.4 本轮发现并修复的真实缺陷（逐条可复核）

| # | 缺陷 | 如何被发现 | 修复 |
|---|---|---|---|
| 1 | 契约 8/12 仅有 placeholder | 只读审计 | 全量生成 12 份 Schema + 语义规则 |
| 2 | 前端 27 行空壳、零 API 调用 | 只读审计 | 重建为 17 视图工作台 |
| 3 | 无 Alembic、无迁移 | 审计（目录不存在） | 建立迁移体系 + 2 个迁移 |
| 4 | 首版自动生成迁移只建 7/10 表（漏 `ev_*` 4 张） | `test_migrated_schema_matches_the_models` 表清单对比 | 补 `EventBase` 进平台元数据 |
| 5 | TeamFlow 表污染平台元数据 | 同上（`tf_*`/`eval_*` 出现在期望集合） | 按 `PLATFORM_TABLES` 显式重建 MetaData + 隔离测试 |
| 6 | `EvidenceRow` 缺 `attachment_ref`（C06 有、ORM 无，附件引用静默丢失） | `test_evidence_row_links_to_the_stored_blob` 抛 `TypeError: invalid keyword` | 补列 + 增量迁移 `1d906be60a54` |
| 7 | `benchmark/` 缺 `__init__.py`，mypy 把 `engine.py` 识别为两个模块 | mypy 报 "Source file found twice" | 补包标记 |
| 8 | 规则索引缺 9 条实现中已存在的规则码 | `test_rule_index_matches_implementation` | 补齐索引 |
| 9 | `packages/__init__.py` 与 `apps/web/__init__.py` 过期文档串 | 审计 | 更正为现状描述 |
| 10 | 前端 favicon 404 污染控制台 | `test_workbench_renders_real_cases_and_probes` 捕获 console error | 内联 data-URI 图标，从根消除 |
| 11 | 分发无队列语义（无重投/死信） | 设计审计 | Broker + Dispatcher 接入 |
| 12 | `shutdown()` 重复定义（旧线程池版本 + 新调度器版本） | ruff F811 + mypy no-redef | 合并为单一定义，保留旧签名 |

**测试纠正了我的错误假设（未改代码，改测试）**：
- `EvidenceRef.role` 合法值为 `support`/`contradict`（小写），我写成 `supporting`
- `EventEnvelope`/`EvidenceRecord` 实际位于 `packages.execution.events`，我误当作 `packages.evidence.events`
- `PRAGMA index_list` 第 2 列是 `unique` 标志而非索引名
- `promote()` 自身就拒绝未审批 Oracle（比我以为的"先提升后校验失败"更严格）
- `ev_event` 无 `run_id` 列（run_id 存在 `context_refs` JSON 内）

---

## 3. 契约体系（C01~C12）

### 3.1 完成情况

12 份全部有 Schema、合法样例、非法样例、语义反例、规则索引、版本记录。
全部 `version = 0.2.0-draft`、`status = draft`；**无一份标记 frozen**。

| 契约 | 对象 | 规则数 | 样例（合/非/语义） |
|---|---|---|---|
| C01 | TestIntent | 2 | 1 / 1 / 1 |
| C02 | TestCase | 10 | 1 / 1 / 1 |
| C03 | Oracle | 7 | 1 / 1 / 1 |
| C04 | Execution | 6 | 1 / 1 / 1 |
| C05 | Event | 8 | 1 / 1 / 1 |
| C06 | Evidence | 8 | 2 / 2 / 1 |
| C07 | Evidence Graph | 5 | 2 / 2 / 1 |
| C08 | Diagnosis | 9 | 1 / 1 / 1 |
| C09 | Probe | 8 | 3 / 3 / 1 |
| C10 | Disposition | 5 | 1 / 1 / 1 |
| C11 | Repair | 10 | 1 / 1 / 1 |
| C12 | Benchmark | 14 | 4 / 4 / 1 |
| **合计** | | **101** | **19 / 19 / 12** |

### 3.2 结构与语义分离

- **结构**：JSON Schema（Draft 2020-12），由 `schema_gen.py` 从注解生成
- **语义**：`semantic.py` 的 101 条跨字段规则，JSON Schema 无法表达的部分，例如
  - 每个必需断言必须有且仅有一个 Oracle 绑定（`C02_ORACLE_BINDING_MISSING`）
  - CONFIRMED 诊断必须写明确定性规则（`C08_CONFIRMED_WITHOUT_RULE`）
  - NOT_RUN 实验不得携带指标（`C12_NOT_RUN_HAS_METRICS`）
  - 禁止修复类型：改 Oracle / 删断言 / 降严格度（`C11_FORBIDDEN_CHANGE`）

**两段式校验**：结构失败即短路，不跑语义（避免在破碎结构上产生噪声）。

### 3.3 已知未定语义（记录，不擅自补全）

- C10 是 Diagnosis 的 routing 视图，字段全部来自 `Diagnosis`，**无新增字段**；若未来要独立
  Disposition 记录（含路由人与理由），需人工确认字段后再扩展
- C03 多来源 Oracle 的优先级/可信等级仍未定（沿用第一轮 P03 待确认）

---

## 4. 工程化

### 4.1 前端工作台

**技术栈**：Vue 3.5 + TypeScript 5.6 + Vite 6（未引入 vue-router，自写 60 行 hash router，
避免为单页应用增加依赖）。

**17 个视图**：

| 页面 | 路由 | 数据来源 |
|---|---|---|
| 项目总览 | `#/overview` | `/overview` |
| TestIntent | `#/intents` | `/intents`（GET/POST） |
| TestCase 列表 | `#/cases` | `/cases` |
| TestCase 详情 | `#/cases/:id` | `/cases/{id}` |
| TestCase 版本 | `#/cases/:id/versions` | 同上 `versions[]` |
| Task | `#/tasks` | `/tasks`（GET/POST）+ 提交 Run |
| Run 列表 | `#/runs` | `/tasks/{id}/runs` + SSE |
| Run 详情 | `#/runs/:id` | `/runs/{id}` + `/report` |
| Step 执行记录 | `#/runs/:id/steps` | `/runs/{id}` |
| Assertion 结果 | `#/runs/:id/assertions` | `/report` |
| Event | `#/events` | `/events` + `/evidence/events` + SSE |
| Evidence | `#/evidence` | `/evidence/records` + `/gaps` |
| 证据图 | `#/graph` | `/evidence/graph/*` + 子图查询 |
| 诊断 | `#/diagnosis`、`#/runs/:id/diagnosis` | `/runs/{id}/diagnosis` |
| Probe 审计 | `#/probes` | `/probes` + `/probes/audit` |
| 修复审核 | `#/repairs` | `/repairs` + approve/reject |
| Benchmark | `#/benchmark` | `/benchmark/datasets` + `/experiments` |

**质量要求落实**：加载态/空态/错误态（统一 `StateBlock`）、分页（`Pagination`）、状态色
（`StatusPill`）、详情跳转、SSE 实时、刷新按钮、Repair 审批走后端门禁（前端按钮无法绕过）。

**验证方式**：`tests/test_workbench_ui.py` 用真 Chrome 加载构建产物，断言页面出现
磁盘上的真实 `case_id`、后端独有的 `state.project_count` 与裁决规则，
并断言空数据页显示空态且无表格行；无控制台错误、无 404。

### 4.2 数据库与迁移

- `alembic.ini` / `alembic/env.py` / `alembic/script.py.mako` / 2 个版本脚本
- URL 解析顺序：`TRACEGUARD_DATABASE_URL` → `Settings.database_url`，**不读 ini 的静态值**，
  保证迁移与运行服务指向同一库
- 平台元数据涵盖 **4 个 DeclarativeBase**：`Base`（tg_*）、`EventBase`（ev_*）、
  `EvidenceBase`（evd_*）、`GraphBase`（gr_*）
- `alembic check` 已纳入测试：模型改动未配迁移时直接失败

### 4.3 PostgreSQL

- 已安装 `psycopg 3.3.9`（项目 venv 内）
- `alembic upgrade head --sql` 可正确渲染 PostgreSQL DDL（测试断言 10 张表 + BEGIN/COMMIT）
- **未连接任何 PG 实例**，无连接测试、无数据校验 → **不声称 PostgreSQL 验收通过**

### 4.4 Redis / Celery

- 未安装 `redis`、`celery`
- `RedisBroker` 只有构造期守卫：无包即 `BrokerError`；有包也需 `allow_unverified=True`；
  方法体一律抛错 → **未做任何 Redis 集成验证**
- 进程内 Broker 为唯一可用实现，**未冒充生产级队列**

### 4.5 存储

- `LocalBlobStore`：内容寻址（key=SHA-256）、两级分片、原子写入（temp+rename）、
  单文件上限、`verify()` 重新哈希检测篡改
- `S3BlobStore`：同 Redis 处理，拒绝冒充

---

## 5. AI 能力

### 5.1 分层现状（不混为一谈）

| 层 | 实现 | 真实 LLM |
|---|---|---|
| Provider 抽象 | `LLMProvider` 协议 + `OpenAICompatibleProvider` | 代码可运行，**未经真实端点验证** |
| 结构化输出 | `TracedClient` + jsonschema 校验 + 重试 | 已用 Scripted 验证重试与拒绝 |
| 调用 Trace | prompt/response 哈希、长度、延迟、schema_ok | 已验证 |
| 决策循环 | `LLMPolicy.next_action` 实现既有 `Policy` 接口 | 循环真实；模型为 Scripted |
| 工具权限 | `ToolGateway` + 代码级白名单 | 已验证（注入无法越权） |
| Compiler | 确定性规则，`model_version=deterministic-rules-NOT-LLM` | 非 LLM |

### 5.2 凭据处理

- 仅按**变量名**检查 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` 是否存在，**从未读取或输出其值**
- `provider_from_env()` 在无凭据时返回 `None`；调用方必须显式处理"LLM 不可用"
- 测试用 `monkeypatch.delenv` 清空后断言返回 `None`，防止未来误判为"已接入"

### 5.3 真实探索循环验证

`tests/test_llm_exploration.py`：Scripted 策略驱动真实 Chrome 完成 4 步登录探索，
5 次决策全部留下 Trace（`schema_ok=True`），`trace.agent_version` 含 `-NOT-REAL-INFERENCE`，
产物可被 Compiler 编译为 Candidate。**这证明循环与网关真实、模型不是。**

---

## 6. Self-Healing（P6：未开始）

**现状不变**：RepairProposal 11 态状态机、断言保护、Oracle 完整性检查、CAS 晋升、
VERSION_CONFLICT、回滚均已实现并有 9 项测试；但"沙箱"仍是**结构校验**，
**没有真实 Playwright 回放**。

**本轮新增的相关证据**：`test_second_round_chain` 证明 `promote()` 在 Oracle 未审批时直接拒绝，
既不能自批也不能跳过 Oracle 审查。

**为什么没做**：本轮把预算投给了 P0/P1/P2/P3/P4/P5（可完全自证的部分）。
P6 需要隔离临时工作区 + 隔离数据库 + 真回放 + 回归对比，是独立工作量，不应草率交付。

**下一步（明确可执行）**：
1. `RepairSandbox`：cwd 复制到 `tmp/repair/<proposal_id>/`，DB 指向临时文件
2. 在沙箱内用真实 Chrome 回放候选版本，记录 Verdict 与步骤
3. 与基版本回放结果对比（同场景、同数据重置）
4. 回放证据写入 Evidence，作为晋升前置条件
5. 晋升失败/回滚保留历史版本完整性测试

---

## 7. Benchmark（P7：未开始）

**现状**：三类评测引擎、8 个指标口径、5 个消融轴已实现（8 项测试），
GT 与 Agent 分区隔离；无真实模型调用时标 `NOT_RUN` 且禁止携带指标。

**未做**：
- 数据集 / Ground Truth / 实验结果**未版本化落盘**（仍在内存）
- 无真实对照实验（人工 / 单次 LLM / Explorer+Compiler 的横向对比）
- Benchmark 页当前显示空态（这是诚实行为，不是缺陷）

**为什么不做数据集播种**：`DatasetManager.create()` 要求每个场景都有 GT，
而 GT 的 `primary_cause` 必须是对根因的判断。用场景描述冒充根因就是编造，
所以本轮**拒绝**为了填满页面而生成 GT。

---

## 8. 安全与可靠性（P8：部分完成）

### 8.1 已完成的真实验证

| 项 | 证据 |
|---|---|
| Prompt Injection 无法越权 | `test_prompt_injection_cannot_smuggle_a_tool_past_the_whitelist`：页面注入说服模型返回 `click`，仍被白名单拒绝 |
| 未知工具双重拦截 | enum（schema）+ 白名单（代码）两层，测试分别覆盖 |
| 裸 SQL 拒绝 | 既有 `test_planner.py`：`sql`/`query` 参数一律 `parameter_schema` 拒绝 |
| 跨 workspace 越权拒绝 | 既有 planner 测试 |
| 自审批修复拒绝 | `test_self_approval_of_repair_is_refused`：`approver=agent` → 422 |
| 路径穿越面为零 | Blob key 强制 64 位十六进制，非法 key 直接 `StorageError` |
| 篡改检测 | `test_verify_detects_corruption_on_disk`：改写文件后 `verify()` 返回 False |
| 敏感字段脱敏 | 既有 planer 测试（token/password/secret/api_key/authorization） |
| 演示/测试数据不落仓库 | 意图写入改用临时目录夹具；smoke 产生的 `benchmark/intents.json` 已删除 |

### 8.2 未完成（不声称覆盖）

- SSRF / DNS Rebinding / Redirect 安全
- 跨进程并发写入、长时间稳定性、资源泄漏
- Worker 崩溃恢复（只验证了死信，未验证进程被杀后的恢复）
- 证据访问授权（当前无鉴权层，仅本地单用户场景）

---

## 9. 最终项目状态

### 9.1 十四阶段

| 阶段 | 第二轮后状态 | 变化 |
|---|---|---|
| 0 工程初始化 | ACCEPTED（既有基线） | — |
| 1 TeamFlow + 故障注入 | ACCEPTED（既有基线） | — |
| 2 专业 UI 自动化框架 | READY_FOR_REVIEW | 维持 |
| 3 调度编排与工作台 | READY_FOR_REVIEW | **增强**：Broker/死信/状态端点 |
| 4 Event 与 Evidence | READY_FOR_REVIEW | **增强**：Blob 存储、`attachment_ref` |
| 5 Explorer Agent | **IN_PROGRESS** | **推进**：Provider/结构化输出/Trace/LLM 形态循环；真实 LLM 仍未接入 |
| 6 TestCase Compiler | READY_FOR_REVIEW | 维持（成功晋升路径本轮补测） |
| 7 Diagnosis v1 | READY_FOR_REVIEW | 维持 |
| 8 Evidence Graph | READY_FOR_REVIEW | 维持 |
| 9 Active Diagnosis Planner | READY_FOR_REVIEW | 维持 |
| 10 Self-Healing | READY_FOR_REVIEW | 维持（沙箱未真回放，见 §6） |
| 11 UI-FaultBench | READY_FOR_REVIEW | 维持（未深化，见 §7） |
| 12 外部泛化/安全/可靠性 | BLOCKED | 仍无外部授权；本地安全测试部分完成 |
| 13 全链路集成与交付 | READY_FOR_REVIEW | **增强**：第二轮链路（含探索→编译→审批→执行→诊断） |

**无任何阶段由本轮改为 ACCEPTED**——本轮全部工作未经人工评审。

### 9.2 测试总览

| 类别 | 数量 |
|---|---|
| 第一轮既有 | 151 |
| 契约 | 77 |
| 迁移 | 10 |
| 存储 | 10 |
| Broker | 10 |
| LLM 策略 | 14 |
| LLM 探索 | 2 |
| 工作台端点 | 13 |
| 工作台 UI（真 Chrome） | 4 |
| 第二轮全链路 | 3 |
| **合计** | **294** |

### 9.3 已知风险

1. **`test_s2_ui_success_persistence_fail` 观察到 1 次偶发失败**（约 590 次运行中出现 1 次）。
   单独运行与相邻同跑均通过，未复现。假设：多个模块级 TeamFlow 服务共享同一 SQLite 文件。
   已做一处解耦（模块 fixture 启动时清故障/清数据），但**未证明根因已消除**。
2. 平台无鉴权层：任何能访问 API 的客户端都可读全部证据（仅适用本地单用户）
3. 无外键：引用完整性依赖服务层，缺少 DB 级兜底

---

## 10. 未完成与需人工决策

| 事项 | 类型 | 说明 |
|---|---|---|
| P6 沙箱回放 | 未开始 | 见 §6 |
| P7 数据集/实验落盘与对照 | 未开始 | 见 §7 |
| P8 SSRF/并发/长稳 | 未开始 | 见 §8.2 |
| 真实 LLM 接入 | 阻塞 | 无凭据；不得自行创建账户或购买 API |
| 契约冻结 | 人工门禁 | 12 份均为 draft，冻结需评审 |
| 外键引入 | 人工门禁 | 属 schema 语义变更 |
| 外部应用泛化（阶段 12） | 阻塞 | 无授权 |
| 正式 Oracle / TestCase 晋升 / Repair 审批人 | 人工门禁 | 未指定 |
| C03 多源 Oracle 优先级与冲突裁决 | 待确认 | 第一轮 P03 延续 |

---

## 11. 复现命令

```bash
cd "D:/桌面/TraceGuard/traceguard"

# 后端全量（真 Chrome + 真 TeamFlow）
./.venv/Scripts/python.exe -m pytest -q

# 静态门禁
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m ruff format --check .
./.venv/Scripts/python.exe -m mypy packages apps benchmark alembic --ignore-missing-imports

# 契约重建（改 dataclass 后必须执行）
./.venv/Scripts/python.exe -m scripts.build_contracts

# 迁移
TRACEGUARD_DATABASE_URL="sqlite:///./data/dev.db" ./.venv/Scripts/python.exe -m alembic upgrade head
TRACEGUARD_DATABASE_URL="postgresql+psycopg://u:p@h:5432/db" ./.venv/Scripts/python.exe -m alembic upgrade head --sql

# 前端
cd apps/web && npm run build && npx vue-tsc --noEmit && cd ../..

# 启动（API 同时托管构建后的工作台）
./.venv/Scripts/python.exe -m scripts.serve --no-teamflow
```

---

## 12. 一句话总结

第二轮把"能不能用"推进到"能不能被评审"：契约、前端、迁移、队列语义、存储、LLM 抽象
六块补齐并逐项有测试；**自愈沙箱、Benchmark 深化、安全纵深三块没做，明确记在这里**，
下一次接手应从 `P6 → P7 → P8` 继续，而不是重做已完成部分。
