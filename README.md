# TraceGuard — Agentic Web UI Testing & Quality Intelligence

面向 Web UI 的**智能测试与质量分析平台**：真实 Playwright/Chrome 执行内核 +
Event/Evidence 证据链 + 多假设诊断 + 证据图 + Oracle 保护的自愈沙箱 +
UI-FaultBench 评测 + 产品级 Vue3 工作台。

> 设计依据：`docs/design/`（11 篇规格）+ `docs/roadmap/`（十四阶段实施路线）。
> 当前状态：第五轮（R5）进行中 —— R5-4.11 工程收口 + R5-5.1 事件持久化 + R5-5.2 场景化 Agent 完成，**601 passed / 1 skipped / 2 项仅因宿主批量删除守卫失败**（本机 2026-11-24 `python-final4` 留档）；
> 产品 E2E 24/24、浏览器验收 150/150、前端门禁全绿。阶段状态与诚实边界见下方"状态纪律"。
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
.\.venv\Scripts\python.exe -m pytest tests/    # 601 passed / 1 skipped / 2 failed（宿主删除守卫；R5-4.11~R5-5.2）
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy packages apps benchmark
cd apps/web; npm run build; npx vue-tsc --noEmit

# 统一门禁（推荐）：quick / python / frontend / security / product / browser / all（默认 quick）
.\.venv\Scripts\python.exe scripts\verify_all.py --mode all
```

- 全量测试：**601 passed / 1 skipped / 2 failed（宿主删除守卫）**（R5-4.11~R5-5.2；484/530 为更早历史快照）
- 统一门禁：`scripts/verify_all.py`（每轮唯一输出目录 + 独立测试库；PASS/FAIL/SKIP/BLOCKED/EMPTY/NOT_RUN 六态，只有 PASS 才算通过）
- 产品级 E2E：**24/24**（`scripts/e2e_product_flow.py`，真实浏览器 + 隔离 SUT）
- 真实浏览器 UI 验收：**150/150**（15 路由 ×（5 分辨率横向溢出 + 4 项逐路由检查）+ 7 项深链 + 8 项附加，`scripts/ui_acceptance.py`）
- 安全专项：9 个文件 / **92 passed + 1 skipped**（`verify_all --mode security`，R5-4.10 `verify/security-final2/report.json`）。该用例在宿主批量删除守卫拦截时走 `pytest.skip` 并附证据，**不判 PASS**；更早的 `security-final`（92 passed + 1 failed）保留为历史记录，两者差异来自测试断言拆分，不是口径选择（详见 R5-4.11 报告 §4）
- 阶段报告：`docs/r5/`（R5-4.9 稳定性收口、R5-7 系统验收复核等）

## 状态纪律（不冒充完成）

- 未批准 GT 映射前，Benchmark 准确率指标输出 `None(BLOCKED)`，不产生假数字（GT 共 6 条记录、全部 `UNRESOLVED`：1 条基线 + 5 个故障场景，尚无人工批准映射）
- LLM 不决定 CONFIRMED / 审批；GT 不暴露给 Agent；INCONCLUSIVE 不计 PASSED
- 真实 DeepSeek 推理保持 **BLOCKED**（D8 未批准）：Explorer 的 `llm` 策略返回 409，Scripted 结果标注 `NOT_REAL_INFERENCE`
- 外部网站/应用测试 **BLOCKED**（OriginPolicy 仅放行本地已批准 origin）
- 30 分钟长稳已执行两轮（R5-7 复核：10/10 链 PASSED、0 错误、内存无漂移，隔离 TeamFlow 端口 8765）
- 浏览器崩溃 / 页面消失 → 步骤记 `BLOCKED`、Run 记 `execution_status=FAILED`，不产出 PASSED/FAILED 业务结论

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
tests/             530 项单元与集成测试
infra/             Docker Compose（PostgreSQL 可选）
docs/              规格设计、调研、每轮交付与验收报告（见文档索引）
scripts/           serve / demo / run_benchmark / ui_acceptance / verify_llm_provider
```
