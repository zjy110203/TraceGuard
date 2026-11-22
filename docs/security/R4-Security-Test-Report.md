# R4 安全测试报告

- 时间：2026-11-22
- 范围：SSRF / DNS Rebinding / Redirect / Prompt Injection / 文件权限 / API 绑定 / 凭据
- 方法：Mock resolver + 本地 127.0.0.1 测试服务器；**未访问任何真实内网/公网地址**
- 结果：`tests/test_security_r4.py` **16 passed**；`tests/test_sandbox_paths.py` 7 passed；`tests/test_storage.py` 10 passed（既有）

## 1. SSRF（UrlGuard，packages/core/url_guard.py）

| 测试 | 结果 |
|---|---|
| 非 http/https scheme（file/ftp/gopher/javascript）拒绝 | PASS |
| 默认策略拒绝私网（10.x/192.168.x） | PASS |
| link-local（169.254.169.254 云元数据）拒绝 | PASS |
| IPv6 loopback / fe80:: / ::ffff:10.0.0.9（mapped）拒绝 | PASS |
| 0.0.0.0 未指定地址拒绝 | PASS |
| 本地开发策略仅豁免 loopback（127.0.0.1），其他私网仍拒绝 | PASS |
| **多地址解析逐个检查**：混合答案含一个私网 IP 即整体拒绝 | PASS |
| userinfo（user:pass@host）拒绝；空 host 拒绝 | PASS |
| 无法解析的 host 拒绝 | PASS |

使用边界说明：当前平台没有接受外部 URL 输入的功能入口（LLM base_url 与 TeamFlow URL 均为配置常量）；UrlGuard 作为强制基础设施交付，任何未来动态 URL 功能必须接入（威胁模型 §2 已标注）。

## 2. DNS Rebinding（Mock Resolver，未使用真实 DNS）

| 测试 | 结果 |
|---|---|
| 首次解析公开 → 二次解析变 loopback：每次 validate 重新解析，重验捕获 | PASS |
| 多 A 记录中一条在两次查询间翻转为 loopback：捕获 | PASS |

模拟边界声明：**测试使用注入的 Mock resolver，不是真实 DNS**；真实 DNS 行为差异（TTL/缓存）不在本轮验证范围。

## 3. Redirect Security（本地 127.0.0.1:8077 测试服务器）

| 测试 | 结果 |
|---|---|
| httpx 默认不跟随 redirect（302 停在跳板，不访问目标）→ LLM provider 出站不受恶意 Location 影响 | PASS |
| 需要跟随 redirect 的客户端：每跳经 guard 复验；跳往非 loopback 私网被拒 | PASS |
| 跳往 file: scheme 被拒 | PASS |
| Redirect 循环被 max_redirects 有界截断（TooManyRedirects） | PASS |

未访问未经授权的真实地址；跳板与目标全部是本机回环或 mock。

## 4. Prompt Injection（权限由代码层控制）

| 测试 | 结果 |
|---|---|
| 伪造故障事件（"忽略指令/根因已确认/跳过审批/读取 GT"）无法伪造 CONFIRMED——确认必须来自真实 eval 平面故障信号 | PASS |
| 注入无法越过 FORBIDDEN_KINDS（overwrite_formal_version 构造即被拒） | PASS |
| 既有覆盖：agent/ai/system 审批被拒；probe 越权被拒并留痕（s9）；GT 服务层不可达 | PASS（复测通过） |

## 5. 文件与权限（既有 + 本轮新增）

| 面 | 结果 |
|---|---|
| Blob key 路径穿越（非 64-hex 一律拒绝） | PASS（既有 10 项） |
| 沙箱目录：无标记目录不删 / 大小写变体归一 / Junction(内外) / 标记范围 / 回传失败显式 / 清理失败显式 | PASS（R4 新增 7 项） |
| UNC/系统目录链接：**BLOCKED**——按约束不创建指向个人/系统目录的链接；Junction 反例已覆盖同等逃逸面 | 记录为边界 |

## 6. API 与 Workspace

- api_host / teamflow_host 默认 127.0.0.1（测试断言）；serve.py 显式 --host 127.0.0.1。
- 无 0.0.0.0 默认、无隧道暴露、无公网访问路径。
- 跨 workspace：业务路由按 workspace_id 过滤；无跨 workspace 读取端点（威胁模型 §2）。
- **多用户权限验证：未做也不宣称**——单机无鉴权模式，多用户方案仅为设计建议。

## 7. 数据保护

- DeepSeek Key 仅经 .env → os.environ → provider 内存；**不出现在**：源码、异常文本、CallTrace、repr、测试输出、报告、截图（test_llm_errors_and_traces_never_contain_the_api_key 断言）。
- 测试输出/日志检查：本轮全部报告不含真实凭据；eval token 为本地演示值且 prod 启动校验强制更换。
- .env 不入版本控制（项目未 init Git；若未来初始化必须先确认 .gitignore）。

## 8. 汇总

| 检查 | 结论 |
|---|---|
| SSRF | PASS（9 项） |
| DNS Rebinding（模拟） | PASS（2 项，模拟边界已声明） |
| Redirect | PASS（4 项） |
| Prompt Injection | PASS（2 项新 + 3 项既有） |
| 文件权限 | PASS（17 项：10 既有 + 7 新增）；UNC 反例 BLOCKED（约束性） |
| 绑定与暴露 | PASS |
| 凭据卫生 | PASS |
| 需提权的测试 | 无（全部在普通用户权限内完成） |
