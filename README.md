# TraceGuard 工程骨架 (M0 + 阶段1 TeamFlow + 阶段2 执行内核)

# 本目录是 v1.1 实施路线「阶段 0 工程初始化」+「阶段 1 TeamFlow 最小被测应用」
# +「阶段 2 专业 Web UI 自动化测试框架（最小闭环）」的可运行落地。
# 设计依据：实施线路/TraceGuard-完整项目实施路线图-v1.1.md。
# 状态：工程默认值已本地验证可运行；P01/P03 等仍为待确认项，见 docs/。

## 快速启动

```bash
# 1. 创建虚拟环境（本机 python 为 3.13；请用 python -m pip 避免 pip 指向旧版本）
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -r requirements-dev.txt

# 2. 复制环境变量样例（不含真实密钥，密钥不入库）
cp .env.example .env

# 3. 初始化数据库（默认 SQLite，零外部依赖；可切换 PostgreSQL）
python -m scripts.init_db

# 4. 启动平台主服务
uvicorn apps.api.main:app --app-dir . --reload --port 8000

# 5. 启动 TeamFlow 被测应用（另一个终端）
uvicorn apps.teamflow.main:app --app-dir . --reload --port 8001
```

启动后：
- 平台服务健康检查：http://127.0.0.1:8000/health
- TeamFlow 被测应用：http://127.0.0.1:8001/
- TeamFlow 独立业务状态查询：http://127.0.0.1:8001/api/v1/projects（返回真实持久化状态）
- Eval 控制面（Ground Truth / 故障注入）：http://127.0.0.1:8001/__eval__/docs
  （需 `X-Eval-Token` 头，Agent 不得持有，见 docs/架构说明.md）

## 目录结构（对齐 v1.1 阶段 0 实施任务 1）

```
apps/api          平台主服务：配置、日志、错误处理、健康检查骨架
apps/teamflow     被测应用：项目创建/列表/详情 + 故障注入 + 独立状态查询
apps/web          Vue3 + TS 前端骨架（阶段 0 占位）
apps/workers      异步 Worker 骨架（阶段 3 启用，本阶段占位）
packages/contracts C01～C12 契约 Schema 目录与版本治理
packages/core     共享配置、结构化日志、统一错误、数据库基座
packages/execution 确定性执行内核（阶段 2：Verdict/模型/Oracle/Runner）
packages/agents    Agent 编排（阶段 5 填充，本阶段占位）
packages/evidence  Event/Evidence（阶段 4 填充，本阶段占位）
benchmark/        场景元数据与 Ground Truth 定义（阶段 1 起积累）
tests/            单元与集成测试
infra/            Docker Compose（PostgreSQL 可选）
docs/             启动、架构、版本矩阵、待确认事项
scripts/          初始化与开发工具脚本
```

## 边界纪律（贯穿全部代码）

- Agent 输出默认是候选信息，不自动成为可信事实。
- 业务状态以数据库为权威；独立状态查询 API 直接读库，不依赖 UI 文案。
- Ground Truth 与故障注入配置只存在于 Eval 控制面，业务面与 Agent 不可读。
- Verdict 聚合规则已冻结并实现（`packages/execution/verdict.py`）；Oracle 多来源优先级（P03）仍待确认，分歧一律 INCONCLUSIVE，不由 Primary 覆盖。
- 阶段 2 不建 Celery 分布式调度、不引入 Browser Use、不让 Agent 决定测试结果。
- 诊断（阶段 7）、Explorer/Compiler（阶段 5/6）、异步平台化（阶段 3）尚未实现。

## 看最小闭环跑起来（一条命令，推荐）

无需手动起服务，脚本会自己启动 TeamFlow、用真 Chrome 跑 3 个场景，并在屏幕上打印闭环结果：

```powershell
# PowerShell（Windows 用户）：在 traceguard/ 目录下
.\.venv\Scripts\python.exe -m scripts.demo
```

```bash
# Git Bash / Linux / macOS
.venv/Scripts/python.exe -m scripts.demo      # Windows venv
# 或  python -m scripts.demo                  # 已激活 venv 时
```

> 不需要 `PYTHONPATH=.` —— `python -m` 会自动把当前目录加入模块搜索路径。

你会看到：正常路径 → `PASSED`；UI 撒谎（TF-UI-001）→ `PASSED` 但 UI 断言失败被记录；DB 故障（TF-DB-001）→ `FAILED`。每场景还打印独立的真值、证据数、结论依据和报告路径。报告 JSON 落在 `data/runs/`。

## 阶段 2：手动执行一条用例（最小闭环）

TeamFlow 启动后（见快速启动），无需 Agent 即可用真 Playwright 驱动系统 Chrome 跑一条用例：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_case benchmark/cases/TC_E2E_001.json --base-url http://127.0.0.1:8001 --reset
```

运行产物是每个 Run 独立的 JSON 报告（`data/runs/`，含 execution_status / test_verdict 分离、断言结果、证据引用、事件序列）。
详见 `docs/架构说明与阶段2退出证据.md`。
