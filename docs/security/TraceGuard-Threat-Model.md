# TraceGuard 威胁模型（本地单用户模式）

- 时间：2026-11-22（R4-3）
- 边界：本机运行，默认绑定 127.0.0.1，单用户，无身份鉴权（如实记录，不宣称多用户安全）。

## 1. 信任边界

```
浏览器(用户) ──> Platform API (FastAPI, 127.0.0.1:8000)
                    │  REST + SSE
                    ├──> Orchestrator(进程内 worker 池) ──> LocalRunner(Playwright/Chrome)
                    │        │                                    │
                    │        │                                    ▼
                    │        │                              TeamFlow(SUT, 独立进程/端口)
                    │        │                                    │ eval 平面(eval token 隔离)
                    │        ▼                                    │
                    ├──> Evidence/Event/Graph(SQLite) <──────────┘ (报告回传)
                    ├──> RepairService(人工门禁+CAS) ──> RepairSandbox(独立子进程+独立DB+独立端口)
                    └──> Benchmark(GT/Dataset/Experiment, GT 仅 Evaluator 可达)
LLM Provider (api.deepseek.com) <── 仅 LLM 出口, Key 仅经 .env/env
```

信任边界 5 条：
1. 用户浏览器 ↔ API（本机回环，无鉴权 → 任何本机进程都可调用 API，威胁模型接受）。
2. Agent/业务平面 ↔ eval 平面（X-Eval-Token；eval token 不下发给 Agent 工具）。
3. 平台 ↔ LLM Provider（出站唯一互联网调用；URL 固定常量，非用户输入）。
4. 沙箱子进程 ↔ 项目（唯一 ID + 归属标记 + 收敛校验，删除/终止只作用于自己的资源）。
5. Evaluator ↔ Ground Truth（服务层隔离：GT 不在 app.state、HTTP 面只挂无 GT 引用的 Facade）。

## 2. 组件威胁矩阵

| 组件 | 资产 | 攻击面 | 主要风险 | 现有措施 | 缺失措施 | 测试 |
|---|---|---|---|---|---|---|
| API | 业务数据、执行控制 | 本机任意进程可调 REST | 无鉴权下的越权操作 | 127.0.0.1 默认绑定；eval token 与业务分离 | 未来多用户需真实鉴权（设计建议见 §4） | test_platform_binds_loopback_by_default |
| Agent | 判定可信性 | 页面内容进入诊断/探索上下文 | Prompt Injection | 权限由代码层控制：确认需真实故障证据；FORBIDDEN_KINDS 硬编码；agent/ai/system 审批被拒 | LLM 输出消毒器（未来） | test_prompt_injection_* |
| LLM Provider | API Key、出站通道 | 恶意 prompt 让平台访问任意 URL | SSRF（间接） | base_url 为固定常量；httpx 不跟随 redirect；trust_env=False | UrlGuard 已交付，任何动态 URL 入口必须接入（本次未发现动态 URL 入口） | test_llm_errors...、guard 全套 |
| Tool Gateway(Probe) | 探测范围 | 页面要求越权探测 | 越权工具调用 | Probe scope 审计（s9 测试：越权被拒+留痕） | — | test_full_chain::s9 |
| Browser | 页面执行 | SUT 页面恶意脚本 | 本机文件读取（浏览器内） | 沙箱回放用一次性 DB + 独立端口；Chrome 非提权 | 浏览器 profile 隔离（Chrome 默认临时 profile） | 沙箱 E2E |
| Worker | 任务一致性 | 异常退出/重复消息 | 重复执行、死信丢失 | 幂等 job_id、可见性超时、重投递、死信队列+重放 | 进程级崩溃恢复演练（本轮有界测试：dispatcher 幂等+死信） | test_broker 全套 |
| Workspace | 数据隔离 | 跨 workspace 读写 | 数据串扰 | workspace_id 贯穿；跨 workspace 访问无路由 | 无正式多租户声明 | — |
| Database | 业务/证据数据 | 直接文件访问 | 本机任意进程可读 SQLite | 单用户模式接受；prod 校验拒绝演示口令 | 加密静态存储（未来） | — |
| Evidence Storage | 证据完整性 | 路径穿越、篡改 | 假证据 | Blob key 强制 64 hex（穿越面为零）；SHA-256 verify | — | test_storage 全套 |
| Ground Truth | 评测公正性 | Agent/业务读取 GT | 评测作弊 | 服务层隔离（app.state 无 GT、Facade 无 gt 属性）；append-only 版本 | 进程级 ACL（单机单用户不适用） | test_gt_not_reachable_via_app_state |
| Repair | 正式资产 | 自动晋升、Oracle 篡改 | 未经批准修改正式版本 | FORBIDDEN_KINDS、Assertion/Oracle 完整性校验、CAS、人工审批、沙箱不晋升 | — | test_repair 全套 |
| Benchmark | 结果可信 | 结果覆盖、GT 反推 | 假实验数据 | append-only 存储+SHA-256、NOT_RUN 语义、INCONCLUSIVE≠PASSED | — | test_benchmark_persistence 全套 |

## 3. 本轮安全测试结论（详见 R4-Security-Test-Report.md）

- SSRF：UrlGuard（scheme 白名单/私网/link-local/IPv6/IPv4-mapped/userinfo/多地址全检）16 项测试全过。
- DNS Rebinding：Mock resolver 模拟首查公开/重查私网、多记录不一致 → 重验捕获。
- Redirect：httpx 默认不跟随（已验证）；跟随场景每跳经 guard 复验；循环受 max_redirects 限制。
- Prompt Injection：伪造故障事件无法伪造 CONFIRMED；注入指令无法越过 FORBIDDEN_KINDS/审批门禁。
- 文件与权限：Blob key hex 白名单（既有）+ 沙箱路径反例 7 项（junction/symlink面/大小写/无标记目录）。
- 凭据：Key 仅在 provider 内存；异常文本/CallTrace/repr 均无泄漏（测试断言）。
- 绑定：api_host/teamflow_host 默认 127.0.0.1；prod 拒绝演示口令。

## 4. 未来鉴权设计建议（不在本轮实施）

1. 单用户 → 多用户：API 层引入 session/token 中间件，按 workspace 授权；eval 平面 token 升级为独立鉴权主体。
2. Service 层加 capability 检查（GT/晋升/审批路由要求显式 role）。
3. 对外暴露前：反代 + TLS + 审计日志不可篡改（append-only 存储）。
4. 明确不做：为单机本地模式伪装"已完成多用户权限验证"。
