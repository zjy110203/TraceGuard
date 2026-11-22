# TraceGuard — Agentic Web UI Testing & Quality Intelligence

面向 Web UI 的**智能测试与质量分析平台**：真实 Playwright/Chrome 执行内核 +
Event/Evidence 证据链 + 多假设诊断 + 证据图 + Oracle 保护的自愈沙箱 +
UI-FaultBench 评测 + 产品级 Vue3 工作台。

> 设计依据：`docs/design/`（11 篇规格）+ `docs/roadmap/`（十四阶段实施路线）。
> 当前状态：第四轮（R4）完成，333 项测试全绿。阶段状态与诚实边界见下方"状态纪律"。
> 完整文档导航见 [`docs/00-DOCUMENTATION-INDEX.md`](docs/00-DOCUMENTATION-INDEX.md)。

## 快速启动

```powershell
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 复制环境变量样例（真实密钥不入库；如需 DeepSeek 真实推理，在 .env 填 DEEPSEEK_API_KEY）
cp .env.example .env

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
| 证据图 | `packages/agents/graph.py` | 可信关联、同源独立性、冲突分析、子图查询 |
| 主动诊断 Planner | `packages/agents/planner.py` | Probe Registry + 审计脱敏 + EIG 评分 + 预算停止 |
| 自愈沙箱 | `packages/execution/sandbox.py` | 子进程隔离 TeamFlow + 真回放 + Oracle 保护 + 人工审批 + CAS 晋升 |
| UI-FaultBench | `benchmark/` | GT 隔离 + 分区版本 + 三类评测 + 消融 + DeepSeek 真实 LLM 对照 |
| URL 安全 | `packages/core/url_guard.py` | SSRF / DNS rebinding / redirect 防护 |
| 工作台 | `apps/web/` | Vue3 + TS，14 路由，设计系统 token，SSE 实时状态 |

## 有一条命令的最小闭环演示

无需手动起服务，脚本自己启动 TeamFlow、用真 Chrome 跑场景并打印闭环结果：

```powershell
.\.venv\Scripts\python.exe -m scripts.demo
```

正常路径 → `PASSED`；UI 撒谎注入 → `PASSED` 但 UI 断言失败被记录；DB 故障 → `FAILED`。
报告 JSON 落在 `data/runs/`。

## 测试与质量

```powershell
.\.venv\Scripts\python.exe -m pytest tests/    # 333 passed
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy packages apps benchmark
cd apps/web; npm run build; npx vue-tsc --noEmit
```

- 全量测试：**333 passed**（第四轮最终留档）
- 真实浏览器 UI 验收：**65/65**（`docs/ui/R4-Frontend-Acceptance.md` + 截图）
- 安全专项：16 项（`docs/security/R4-Security-Test-Report.md`）
- `test_s2` 隔离后 50/50（`docs/audit/R4-test-s2-isolation-report.md`）
- 交付报告：`docs/FINAL/TraceGuard-Fourth-Run-Report.md`

## 状态纪律（不冒充完成）

- 阶段 10/11/13 为 **READY_FOR_REVIEW**，阶段 5 IN_PROGRESS，阶段 12 **BLOCKED**（无外部应用授权）
- 未批准 GT 映射前，Benchmark 准确率指标输出 `None(BLOCKED)`，不产生假数字
- LLM 不决定 CONFIRMED / 审批；GT 不暴露给 Agent；INCONCLUSIVE 不计 PASSED
- DeepSeek 真实推理与 Scripted Policy 严格区分标注；密钥只在本地 `.env`，不入库
- 30 分钟长稳测试 NOT_RUN（如实标注）

## 目录结构

```
apps/api          平台主服务（FastAPI）：工作台 API、治理面、Benchmark Facade
apps/teamflow     被测应用：项目 CRUD + 可控故障注入 + 独立状态查询 + Eval 控制面
apps/web          Vue3 + TS 产品级工作台（14 路由 + 设计系统）
apps/workers      异步 Worker 骨架
packages/contracts C01～C12 契约 Schema 与版本治理
packages/core      配置、结构化日志、错误、数据库基座、UrlGuard
packages/execution 执行内核、Oracle、Verdict、RepairSandbox
packages/agents    Explorer / Compiler / Diagnosis / Graph / Planner / LLM Provider
packages/evidence  Event / Evidence / Outbox / 内容寻址存储
benchmark/         Dataset / GT / 三类评测 / 消融 / 持久化
tests/             333 项单元与集成测试
infra/             Docker Compose（PostgreSQL 可选）
docs/              规格设计、调研、每轮交付与验收报告（见文档索引）
scripts/           serve / demo / run_benchmark / ui_acceptance / verify_llm_provider
```
