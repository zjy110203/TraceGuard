# 第四轮交付报告

- 时间：2026-11-22 21:35 ~ 2026-11-23 00:00（GMT+8）
- 依据：《TraceGuard｜第四轮自主工程开发总控指令（R4）》及 DeepSeek 接入补充要求
- 保留第一至三轮全部交付物；本报告不覆盖任何历史文件
- 状态标记：PASS / FAIL / NOT_RUN / BLOCKED / READY_FOR_REVIEW

---

## 0. 结论摘要

R4 五个工作包（R4-0 ~ R4-4）与 DeepSeek 真实 LLM 接入全部完成，未虚报任何未执行项。

- **全量回归：333 passed, 0 failed（380s，留档 `.traceguard-tmp/r4/final_full_run2.log`）**
- 质量门禁：ruff check / ruff format / mypy（61 源文件）/ Vite build / vue-tsc 全绿
- 前端真实浏览器验收 **65/65 PASS**（14 路由、三种视口、console/4xx 零错误）
- DeepSeek 真实调用验证 PASS（981ms，prompt 106 + completion 67 tokens，schema_ok）；Benchmark 中真实推理 6 次调用 979 tokens，与 Scripted Policy 严格区分标注
- GT 映射仍为 UNRESOLVED：准确率类指标按门禁输出 `None(BLOCKED)`，不产生假数字
- 安全专项 16 项 PASS；test_s2 隔离后干净条件 50/50 PASS
- 阶段 10/11/13 维持 READY_FOR_REVIEW；阶段 12 维持 BLOCKED（无外部应用授权）；**无任何阶段自评 ACCEPTED**

### 关于最后一次定版回归（如实记录）

全量回归 333 passed 之后仅做了 lint/格式级修复（`benchmark/store.py` 导入排序与格式化；
`tests/test_security_r4.py` 两处 ruff 规则修复），已对这些改动的关联测试增量验证
（`tests/test_security_r4.py` 16 passed；`tests/test_benchmark_persistence.py` +
`tests/test_llm_exploration.py` + `tests/test_llm_policy.py` 22 passed）。
此后发起的定版全量回归被用户取消（执行至 64%，零失败），按用户指令**未重跑**。
因此"333 passed"为最后一个完整留档结果；取消运行的日志 `.traceguard-tmp/r4/final_full_run3.log` 保留供核对。

---

## 1. R4-0 第三轮成果复核与基线重验 — PASS

| 项 | 结果 |
|---|---|
| pytest 基线重验 | 302 passed（第三轮数字复现） |
| ruff / ruff format / mypy / Vite build / vue-tsc | 全绿 |
| `sandbox.py` 源码复核 | Oracle 保护、进程安全（只杀自有 PID）、Evidence 先回传后清理、路径归属收敛四项逐条核对 |
| 路径反例测试 | 新增 `tests/test_sandbox_paths.py` 7 项（无标记目录、大小写变体、Junction 内/外、标记范围、证据回传失败、清理失败） |
| 阶段 10 验收场景矩阵 | 14 项，见 `.workbuddy-run/fourth-round-audit.md` §E |

过程修复：pytest `--basetemp` 不创建父目录导致首轮 13F+41E → 预建 `.traceguard-tmp/r4`；
`.env` 两处行内注释导致 pydantic-settings 解析失败 → 原地清洗（不回显密钥）。

## 2. R4-1 前端产品级 UI/UX 重构 — PASS

- 调研：`docs/ui/R4-Skill-Plugin-Review.md`（不安装新插件）；审计：`docs/ui/R4-UI-UX-Audit.md`（P0 五项 + 端点实测 38）
- 设计系统：`docs/ui/TraceGuard-Design-System.md` + `TraceGuard-Page-Spec.md`；非“换色换字体”，包含 token、组件、页面规格与交互状态
- 真实重构：`styles.css` token 化；`router.ts` 修复 hash 查询串解析（P0）；AppShell 重写（面包屑/折叠侧栏/后端+SSE 双状态）；
  6 视图重写 + 10 视图子代理升级 + 2 新视图（Candidates/System）+ 3 新组件（PageHeader/MetricCard/ConfirmDialog）；
  `client.ts`/`types.ts` 扩展；后端最小增强（`/overview.recent_runs` cap 10、`/cases.approved_by`）
- 验收：`docs/ui/R4-Frontend-Acceptance.md` + `docs/ui/screenshots/*.png`（65/65 PASS）
- 过程修复：`serve.py` sys.path、390px 表格横向溢出（`.split > * {min-width:0}` + `.table-wrap`）

## 3. DeepSeek 真实 LLM 接入 — PASS

- Key 经 `.env` 注入：`ensure_env_file_vars()`（`packages/core/config.py`）在入口显式调用；
  `provider_from_env()` 保持不自动加载 .env 以维持测试 hermetic；Key 未入源码/日志/报告/截图
- LLMResponse / CallTrace 增加 `prompt_tokens` / `completion_tokens`，真实 usage 被捕获
- 真实调用验证（`scripts/verify_llm_provider.py`）：PASS，981ms，106+67 tokens，schema_ok
- 真实推理与 Scripted Policy 严格区分：Benchmark Track B 方法 B 标注为 DeepSeek 真实 6 调用/979 tokens；
  verdict 6/6 与真实执行一致，机制识别弱于带证据规则诊断（如实记录）
- 修复真实缺陷：严格 schema 偶发缺 `confidence` → required 仅留必填字段 + prompt JSON 样例

## 4. R4-2 UI-FaultBench 深化 — READY_FOR_REVIEW

- GT 来源核查与映射提案：`docs/benchmark/R4-GT-Mapping-Proposal.md`（5 个 fault_id 全部 UNRESOLVED；候选机制仅启发式，非批准）
- 持久化：`benchmark/store.py`（GT/数据集/实验 append-only + SHA-256 完整性 + 拒绝覆盖 + 幂等恢复），7 项测试 PASS
- 服务层隔离：GT 不挂 `app.state`，HTTP 面只暴露只读 Facade（`DatasetManagerView` / `ExperimentManagerView`）
- 引擎门禁：UNRESOLVED GT 时 `primary_cause_accuracy` / `selective_accuracy` / `macro_f1` = None(BLOCKED)
- 真实实验两轮留档：`docs/benchmark/R4-Benchmark-Results.md`——
  Track A 三指标 1.0；Track B 四方法 6 场景真 Chrome（D 覆盖 0.5、B 覆盖 1.0）；
  Track C 真沙箱 VALIDATED（assertion_preservation 1.0）；消融两轴（移除 evidence_graph → coverage 0.5→0）
- UI 场景“GT=PASSED 但断言 FAILED”的口径差异记录为人工决策项，不擅自改 GT

## 5. R4-3 本地安全专项 — PASS（部分 NOT_RUN）

- 威胁模型：`docs/security/TraceGuard-Threat-Model.md`（12 组件矩阵 + 5 信任边界 + 鉴权建议）
- `UrlGuard`（`packages/core/url_guard.py`）：scheme 白名单、私网/link-local/IPv6/mapped/userinfo 拒绝、多地址全检、可注入 resolver、redirect 校验
- `tests/test_security_r4.py` 16 passed：SSRF 9 项、DNS rebinding 模拟 2 项、redirect 4 项（本机测试服务器）、
  prompt injection 2 项（伪造 CONFIRMED 与越权 FORBIDDEN_KINDS 均失败）、默认值与凭据不泄漏
- 报告：`docs/security/R4-Security-Test-Report.md`、`R4-Reliability-Test-Report.md`
- **NOT_RUN**：30 分钟长稳（预算给了前端与真实实验）；指向系统目录的 UNC 反例 BLOCKED（安全约束）
- 使用边界：当前平台无接受外部 URL 的输入入口，UrlGuard 作为强制基础设施交付，未来动态 URL 功能必须接入

## 6. R4-4 test_s2 偶发失败专项 — PASS

- Fixture 隔离：`tests/test_full_chain.py` 改 function 级独立 TeamFlow（独立 SQLite/端口/场景 seed，run-log.jsonl 留档 PID/端口/DB 路径）；业务语义与断言零修改
- 第一轮 50 次：48 passed + 2 次 setup 期 error（无隔离日志条目，与并行 ruff format 窗口吻合 → 判定并发工具干扰）
- 干净条件复验（期间零文件修改）：**50/50 PASS**；报告 `docs/audit/R4-test-s2-isolation-report.md`

## 7. 最终全量回归与失败归因

| 轮次 | 结果 | 说明 |
|---|---|---|
| 首轮 | 13F + 41E | basetemp 父目录缺失（pytest 不自动创建）→ 预建后 302 passed |
| 中途（serve 运行中） | 3F | 服务进程占用资源 → 停服 |
| 中途（并行负载） | 14F + 57E | 并发工具干扰，不作为交付证据 |
| 中途 | 8F + 324P | 全部集中在 sandbox 清理/路径测试，小组合跑 12/12 通过 → 判定负载相关 |
| 干净全量 | 1F + 331P | 唯一失败：`test_marker_content_is_ownership_scoped` 的 `close()` 中 `shutil.rmtree` 抛出 SystemExit |

**根因（真实发现）**：宿主 WorkBuddy sandbox-center shim（`sitecustomize.py`）劫持 `shutil.rmtree`，
本轮累计删除文件达到 50 时抛 `SystemExit(1)`（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`）；
`SystemExit` 继承 `BaseException`，穿透原有 `except OSError`，才导致集中失败。

**修复**：`packages/execution/sandbox.py` 的 `close()` 增加 `except SystemExit`，将外部阻断记录为
`cleanup blocked: SystemExit` 并写入 `result.cleanup_error`，不传播、不冒充成功；
新增回归测试 `test_systemexit_from_delete_guard_is_recorded_not_propagated`；
测试自身的兜底清理也豁免该 shim 的 SystemExit。修复后完整回归 **333 passed**。

## 8. 阶段状态与限制

- 阶段 10（Self-Healing）：READY_FOR_REVIEW —— 沙箱真实回放与路径安全强化，审批/CAS 门禁未变
- 阶段 11（UI-FaultBench）：READY_FOR_REVIEW —— 持久化/门禁/实验完成，GT 映射待人工批准
- 阶段 13（全链路集成与交付）：READY_FOR_REVIEW —— 全链路 + 333 passed 回归
- 阶段 5：IN_PROGRESS（真实 LLM 已接入 Explorer 调用层，但完整决策循环评测未重做）；阶段 12：BLOCKED（无外部应用授权）
- 所有阶段均**未自评 ACCEPTED**，需正式评审冻结

## 9. 交付物索引

| 类别 | 路径 |
|---|---|
| 本报告 | `docs/FINAL/TraceGuard-Fourth-Run-Report.md` |
| 追踪文件 | `.workbuddy-run/fourth-round-{progress,tasks,audit,decisions,safety,test-results,changes,resume}.md` |
| 前端 | `docs/ui/R4-*.md`、`docs/ui/TraceGuard-Design-System.md`、`TraceGuard-Page-Spec.md`、`screenshots/` |
| Benchmark | `docs/benchmark/R4-GT-Mapping-Proposal.md`、`R4-Benchmark-Results.md`、`benchmark/data/` |
| 安全/可靠性 | `docs/security/TraceGuard-Threat-Model.md`、`R4-Security-Test-Report.md`、`R4-Reliability-Test-Report.md` |
| 隔离专项 | `docs/audit/R4-test-s2-isolation-report.md`、`.traceguard-tmp/r4/s2-isolation/run-log.jsonl` |
| 回归日志 | `.traceguard-tmp/r4/final_full_run2.log`（333 passed） |
