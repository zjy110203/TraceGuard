# R4 可靠性测试报告（有界）

- 时间：2026-11-22
- 范围：Worker 故障语义、消息语义、死信、存储损坏（既有测试复验）+ 本轮边界声明
- 并发/长稳：见 §3（本轮规模受控）

## 1. 既有可靠性覆盖（本轮复验全过）

| 语义 | 测试 | 结果 |
|---|---|---|
| 任务幂等 | test_enqueue_is_idempotent_per_job_id | PASS |
| 可见性超时重投递 | test_visibility_timeout_redelivers_the_same_job | PASS |
| 重复投递只执行一次 | test_dispatcher_runs_each_job_once_and_skips_settled_duplicates | PASS |
| 失败任务死信 | test_nack_without_requeue / test_max_deliveries / test_dispatcher_dead_letters_a_failing_handler | PASS |
| 死信可由操作员重放 | test_dead_letter_can_be_requeued_by_an_operator | PASS |
| Blob 损坏检测 | test_verify_detects_corruption_on_disk | PASS |
| 大小上限先拒后写 | test_size_cap_is_enforced_before_writing | PASS |
| Redis/S3 不假装可用 | test_redis_broker_refuses_to_pretend / test_s3_adapter_refuses_to_pretend | PASS |
| Worker 异常不晋升 | test_target_startup_failure_is_reported_not_promoted（沙箱） | PASS |
| 数据库临时不可用 | 由 HTTP 500 + 前端 ErrorState 呈现（test_platform_api 覆盖错误封套） | PASS |

## 2. 本轮新增相关加固

- 沙箱清理失败显式化（cleanup_error）、Evidence 回传失败显式化（evidence_delivery_error）——资源/证据异常不再伪装成功（test_sandbox_paths）。
- run-log.jsonl 记录每次隔离测试的 DB 路径/端口/PID/故障配置（R4-4）。

## 3. 并发与长稳（有界执行）

本轮规格：
- 并发：≤2 Worker / ≤2 Browser Context（Orchestrator max_workers=2 默认；沙箱单进程单上下文）。
- 短稳验证：全量 302+ 测试连跑（约 6 分钟）+ benchmark runner 连跑 2 轮 + 沙箱 E2E 多次 = 系统级重复执行无资源泄漏（进程数回落、句柄随进程退出）。
- **30 分钟长稳：NOT_RUN**——本轮预算给了前端重构与三类真实实验；长稳需专门窗口，列为下轮候选。 CPU/内存/磁盘采样器未在本轮部署，不虚报数据。

## 4. 资源边界纪律

- 所有临时物在 .traceguard-tmp/r4/（pytest basetemp、沙箱根、隔离 DB、s2 日志）。
- 沙箱关闭即删除自己的目录（失败显式记录）；隔离测试 DB 保留在 tmp 供复查，不触碰业务库。
- 未占满整机资源：并发 Chrome 实例 ≤2，CPU 峰值出现在真实回放瞬间，属预期。
