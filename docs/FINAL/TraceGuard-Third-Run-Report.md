# 第三轮交付报告

- 时间：2026-11-22 20:05 ~ 21:35（GMT+8）
- 依据：《TraceGuard｜第三轮自主工程开发总控指令（R3）》
- 保留第一、二轮全部交付物；本报告不覆盖任何历史文件
- 状态标记：PASS / FAIL / NOT_RUN / BLOCKED / READY_FOR_REVIEW

---

## 0. 结论摘要

**R3-W6（最高优先级）达成**：Self-Healing 现在拥有真实的隔离沙箱——子进程 TeamFlow + 独立 SQLite + 独立端口 + 真实 Chrome 回放，基线/候选/对照三方对比，5 项端到端测试全绿。**沙箱只产出证据，晋升仍必须经人工审批 + CAS。**

**R3-W7、R3-W8 未完成**：如实标注 NOT_STARTED（原因与入口计划见 tasks）。第三轮的可自主预算集中给了 W6，宁可少做、不虚报。

全量测试：**302 passed**（第二轮 294 → 297[晚间修复] → 302[沙箱 5 项]），ruff/format/mypy/前端全绿。

---

## 1. 第二轮复核结果

PASS：基线重验 297 passed（含晚间修复的 3 项回归）；ruff/format/mypy/前端全绿。
契约符合性审计见 `docs/audit/R3-Contract-Conformance.md`：12 Schema 无漂移、101 规则可追溯、
C10 维持 routing 视图、C03 优先级维持 BLOCKED。
前端统计澄清：17 个渲染分支（视图）、12 条主路由、39 个 API 端点——第二轮的"18 路由"是把渲染分支计入了。

## 2. 历史问题处理结果

| 问题 | 处置 | 状态 |
|---|---|---|
| P6 沙箱未真回放 | 本轮实现 RepairSandbox | READY_FOR_REVIEW |
| P7 Benchmark 未落盘 | 未开始 | NOT_RUN |
| P8 SSRF/并发/长稳 | 未开始；文件/进程安全前提已随 W6 落地 | NOT_RUN |
| `test_s2` 偶发 | 立项专项，未执行 | BLOCKED |
| 平台守卫拦截 pytest Temp 清理 | 改用项目内 `--basetemp`，未绕过守卫 | PASS |

## 3~8. R3-W6 Self-Healing 真实沙箱

### 3. 实现（`packages/execution/sandbox.py`）

- 生命周期：open（唯一 ID + 归属标记 + 路径收敛校验）→ 子进程 TeamFlow（独立 SQLite/端口/PID/stderr 落盘）→ replay（真 Chrome，每轮重置数据）→ compare → evidence（SHA-256 + `on_evidence` 回传）→ close（只杀自己 PID、只删归属目录）
- 判定四态：VALIDATED / NO_BEHAVIORAL_CHANGE / NOT_VALIDATED / SANDBOX_FAILED
- 与状态机关系：沙箱只产证据；VALIDATING→VALIDATED 与 PROMOTING 仍由 `RepairService` 承担，人工审批与 CAS 未变

### 4. 真实回放证据 — PASS

`test_locator_drift_is_repaired_in_the_sandbox`：
基线（`s6` 指向重构后不存在的 locator）真 Chrome 回放 **FAILED**；候选（修复 locator、Oracle 仅版本号升级）同种子回放 **PASSED**；对照用例（TC-CREATE-002）前后均 PASSED → VALIDATED。证据含 target_url、target_pid、SHA-256。

### 5. Assertion Preservation — PASS（既有）

`AssertionPreservationChecker` + `OracleIntegrityChecker` + 禁改类型白名单（既有 9 项 repair 测试）；沙箱层额外验证候选 round-trip 身份一致。

### 6. Oracle Integrity — PASS（既有）

候选 Oracle 只允许版本号标签升级，`primary/supporting/expected/adjudication_rule` 结构不变（测试构造候选时仅 bump version，沙箱对比不涉及 Oracle 语义）。

### 7. 人工审批与晋升 — PASS（既有 + 本轮强化）

`promote()` 在 Oracle 未审批时**直接拒绝**（第二轮已验证）；审批人 `agent/ai/system` 被拒。沙箱不提供任何自动晋升入口。

### 8. CAS 与回滚 — PASS（既有）

VERSION_CONFLICT 记录不静默重试、PROMOTED→ROLLED_BACK 保留历史（既有 9 项 repair 测试）。

## 9~12. R3-W7 Benchmark

**NOT_RUN**。GT 权威来源已确认（`eval_scenario`/`eval_ground_truth` + seed 的期望配置），
但 `primary_cause` 的 fault→机制映射需人工确认；数据集落盘与三类实验未实现。
入口方案见 `third-round-tasks.md`。

## 13~17. R3-W8 安全与可靠性

**NOT_RUN**（威胁模型与专项测试未开始）。本轮已落地的安全相关前提（均有测试）：

- BlobStore key 强制 64 位十六进制（路径穿越面为零）
- 沙箱目录归属标记 + 收敛校验：测试证明他人目录不被删除
- 子进程只由沙箱创建、PID 在册、关闭时定向终止
- `_child_env()` 防御 Windows 环境块大小写重复损坏（本轮真实缺陷）
- serve.py 绑定 127.0.0.1，无公网暴露

## 18. 本机安全操作记录 — PASS

- 全部操作限于 `D:/桌面/TraceGuard`；未触碰系统目录/注册表/PATH/防火墙/电源/启动项
- 未申请管理员权限；未安装系统级服务；未同步到其他项目
- 临时物清理：`packages/.traceguard-tmp/`（本轮误建的沙箱根）已按归属删除；
  `ps_*.log` 调试残留已删；`sb_foreign_0001` 是测试设计的一部分，由测试自行重建
- 测试子进程（沙箱 TeamFlow）只由沙箱启动并定向终止，从未批量杀进程

## 19. 全量测试 — PASS

```
pytest 302 passed, 1 warning, 354.05s（--basetemp=.traceguard-tmp/pytest）
ruff check / format：全过（103 文件）
mypy（含 alembic）：Success
前端：vite build ✓ + vue-tsc 0 错误
```

## 20. 阶段与里程碑

| 阶段 | 第三轮后 | 变化 |
|---|---|---|
| 0~1 | ACCEPTED（基线） | — |
| 2~4 | READY_FOR_REVIEW | — |
| 5 | IN_PROGRESS | 真实 LLM 未接入 |
| 6~9 | READY_FOR_REVIEW | — |
| **10** | **READY_FOR_REVIEW（实质推进）** | 沙箱真回放已实现；待评审，未自行置 ACCEPTED |
| 11 | READY_FOR_REVIEW | Benchmark 落盘未做，不能 ACCEPTED |
| 12 | BLOCKED | 本地安全部分前提已就绪；外部泛化仍等授权 |
| 13 | READY_FOR_REVIEW | 完整验收前不置 ACCEPTED |

里程碑 M1~M5 维持第二轮状态；M4（安全自愈）实质增强。

## 21. 未完成事项

| 项 | 状态 |
|---|---|
| R3-W7 数据集/GT/实验落盘 + 三类评测 | NOT_RUN |
| R3-W8 威胁模型 + SSRF/DNS/Redirect 专项 | NOT_RUN |
| 并发与长稳测试 | NOT_RUN |
| `test_s2` ≥50 次重复验证 | BLOCKED（需 fixture 隔离改造先行） |
| 沙箱接入工作台端点（异步 + SSE） | NOT_RUN |
| 真实 LLM / PG / Redis / S3 / 外部应用 | BLOCKED / NOT_RUN |

## 22. 后续人工决策清单

| 事项 | 说明 |
|---|---|
| 阶段 10 评审 | 沙箱实现是否接受为真沙箱；接受后是否晋升阶段状态 |
| R3-W7 `primary_cause` 语义映射 | fault_id → 失败机制的对应关系需人工确认，否则根因准确率不纳入 |
| R3-W8 威胁模型评审 | 12 个主体的信任边界需架构评审 |
| 沙箱是否允许访问网络 | 当前仅访问沙箱内 TeamFlow（127.0.0.1）；是否放宽需确认 |
| `test_s2` 隔离改造 | 模块级 TeamFlow fixture 改独立 DB 是否批准 |
| 契约冻结 / 外键 / C03 优先级 / 审批人 | 延续前两轮待确认项 |

## 23. 一句话总结

第三轮把 Self-Healing 从"状态机 + 结构校验"推进到"**真浏览器、真隔离、真对比、真证据**"，
并顺带消灭了三个 Windows 特有的真实缺陷（沙箱根路径、环境块损坏、证据与清理的矛盾）。
Benchmark 与安全专项按指令要求如实保持 NOT_RUN，等待下一轮预算。
