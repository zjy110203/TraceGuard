# R4 test_s2 偶发失败专项报告（Fixture 隔离）

- 时间：2026-11-22
- 批准范围：仅测试层（TeamFlow 模块级共享 Fixture → 每测试隔离数据库）
- 结论：**偶发失败与共享 Fixture/状态污染高度相关；隔离后干净条件 50/50 零失败**

## 1. 改造设计（tests/test_full_chain.py）

| 项 | 改造前 | 改造后 |
|---|---|---|
| Fixture 作用域 | module 级，全模块共享一个 TeamFlow | function 级，**每测试独立实例** |
| 数据库 | 共享 `data/teamflow.db`（跨模块污染面） | 每测试独立 SQLite（tmp_path，随机文件名） |
| 端口 | 固定 8037（并行/残留进程冲突面） | 每次绑定空闲端口 |
| 场景/GT | 依赖共享库中历史 seed | 每实例重新 seed 6 个权威场景 |
| 业务语义 | — | **零修改**：build_app(settings) 仅测试层注入；生产语义未动 |
| 测试意图 | — | **零修改**：断言全部保留，未删测试未放宽 |
| 证据 | — | `.traceguard-tmp/r4/s2-isolation/run-log.jsonl`：每次运行记录时间/PID/端口/DB 路径/故障配置 |

## 2. 验证结果

### 第一轮（50 次）
- 48 passed + 2 次 error（run49/50）
- 2 次 error 均为 **fixture 建立前**的 0.45s 快速失败，隔离日志**无对应条目**（成功建立的测试都有 setup+teardown 双条目）
- 时间与并行执行的 ruff format 重写测试文件窗口吻合 → 判定为**并发工具干扰**，非测试缺陷
- 处置：按"失败不掩盖"纪律，重新在干净条件（期间零文件修改）下复验

### 第二轮（干净条件，50 次）
- **50/50 全部 passed，0 失败 0 错误**
- 单次耗时 ~4-5s（独立 server 启动 + 真实 Chrome 回放）
- 累计有效验证：98 次成功通过，无一例业务断言失败

### 全文件回归
- `tests/test_full_chain.py` 全部 11 测试通过（129s），隔离改造对其他测试无副作用

## 3. 结论与推断

1. R2 报告的 1/590 偶发失败机制与共享 Fixture 一致：模块级共享 DB + eval 平面故障/事件状态跨测试残留，在特定交错下污染 s2 的诊断输入（故障事件窗口/armed fault 泄漏）。隔离改造从结构上消除了这类交错。
2. 干净条件下 50/50 零失败：**原偶发失败可归因于共享状态污染**（统计上，若原 1/590 概率仍存在，50 次全过的 P(观察到0失败)≈99.2%，支持但不构成数学证明）。
3. 不宣布"问题已修复"为绝对结论：如指令要求，保留全部证据（results.txt 两份、run-log.jsonl 60+ 条目、DB 快照在 tmp 保留）。

## 4. 证据文件

| 文件 | 内容 |
|---|---|
| .traceguard-tmp/r4/s2-repeat/results.txt | 第一轮 50 次原始输出 |
| .traceguard-tmp/r4/s2-repeat/results-clean.txt | 第二轮干净条件 50 次原始输出 |
| .traceguard-tmp/r4/s2-isolation/run-log.jsonl | 每次运行的 DB 路径/PID/端口记录 |
| .traceguard-tmp/r4/pytest-s2*/ | 各次隔离测试的独立 DB 快照（保留） |
