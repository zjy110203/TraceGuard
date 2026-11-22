# R4 UI-FaultBench 结果报告

- 时间：2026-11-22 22:34（运行 20261122T143446，另有首轮 143218 留档）
- 执行：`scripts/run_benchmark.py`，隔离 TeamFlow（独立 SQLite + 端口 8041）+ 真实 Chrome + DeepSeek 真实调用
- 持久化：`benchmark/data/`（gt / datasets / experiments，append-only + SHA-256 完整性）
- 前端：Benchmark 页消费同一数据源（/api/v1/benchmark/*）

## 1. 数据集与 GT 状态

- GT 版本：`gt-v4-r4-unresolved-20261122T143446`（6 场景，expected_verdict 来自 seed 权威定义）
- **primary_cause / fault_class = UNRESOLVED**（映射提案见 `R4-GT-Mapping-Proposal.md`，等待人工批准）
- 数据集：`d-trusted-r4`（TRUSTED_TEST_SET，6 场景，版本含 GT 版本哈希）
- **准确率类指标（primary_cause_accuracy / selective_accuracy / macro_f1）全部输出 None(BLOCKED)**——引擎强制，未批准映射前不产生任何准确率数字。

## 2. Track B：诊断（6 场景 × 4 方法，全部真实执行）

| 方法 | 说明 | false_pass_rate | coverage | 机制回答 |
|---|---|---|---|---|
| A 原始报告 | 仅 verdict | 0.0 | 0.0（无机制输出） | 无 |
| C 规则诊断（无 eval 证据） | DiagnosisService 不带 fault_events | 0.0 | 0.0 | 全部 UNKNOWN（诚实保守） |
| D 规则诊断（带 eval 证据/证据图） | 带 fault_events | 0.0 | **0.5** | API✓ NET✓ PERSISTENCE✓（3/3 命中候选映射）；UI/TIME/BASE→UNKNOWN |
| B 单次 LLM（DeepSeek 真实调用） | 979 tokens / 6 次调用 | 0.0 | **1.0** | UI→UI_RENDERING✓；API/DB→NONE（识别失败，如实记录） |

真实 verdict vs GT：BASE=PASSED✓ API=FAILED✓ NET=PASSED✓ DB=FAILED✓ **UI=FAILED（GT=PASSED）** TIME=PASSED✓ —— UI 场景的"误报"是真实测量：TC-CREATE-002 断言成功横幅，而 GT 语义为业务成功（提示缺失）。这正是 benchmark 要暴露的用例口径差异，记录为人工决策项。

要点：
1. **false_pass_rate = 0.0**（全部方法）：没有 truth=FAILED 的场景被判 PASSED——INCONCLUSIVE 未计入 PASSED ✓。
2. 证据图输入（D）把机制回答率从 0 提升到 0.5，且 3 个回答全部与候选映射一致——**为批准映射提供了正面证据，但不构成批准本身**。
3. 单次 LLM（B）verdict 判断 6/6 与真实执行一致，但机制识别弱于带证据的规则诊断——真实模型实验与 Scripted Policy 的区别在此体现（B 是真实推理，成本 979 tokens）。

## 3. Track A：测试生成（真实回放）

- 已审核人工用例（TC-CREATE-002、TC-LIST-001）×2 次真实 Chrome 回放：
  - **replay_stability = 1.0**（两次 verdict 一致）
  - **locator_validity = 1.0**（15/15 steps 全过）
  - **assertion_correctness = 1.0**
- Scripted Explorer + Compiler 链：NOT_RUN（本轮预算边界，如实标注，未用 scripted 冒充）

## 4. Track C：自愈（真实沙箱）

| 方法 | 状态 | assertion_preservation | repair_validation_success | acceptance |
|---|---|---|---|---|
| 受保护修复（R3 RepairSandbox，真 Chrome） | COMPLETED | **1.0** | **1.0** | 0.0（人工门禁，未晋升）✓ |
| 人工修复基线 | NOT_RUN | — | — | 无人类操作者，不伪造 |
| 无保护 LLM 修复 | NOT_RUN | — | — | 仅限隔离环境，本轮预算外 |

沙箱证据：verdict=VALIDATED，evidence_delivery_error=''（回传成功），cleanup_error=''（清理成功）。

## 5. 消融（隔离预测变体，不触碰正式晋升链路）

| 轴 | 基线 | 消融结果 |
|---|---|---|
| evidence_graph | diagD（coverage 0.5） | 移除后 coverage 0.5→0（等价 C）：**证据图贡献 3/6 的机制回答** |
| active_diagnosis | diagD | 同上变体；probe_count=0（本轮规则诊断无主动探测预算） |

消融仅在预测函数层隔离运行，Oracle Protection/Assertion Preservation 未被移除作用于任何正式链路。

## 6. 指标口径（全部定义于 benchmark/engine.py METRICS）

每个指标含统计单位/分子/分母/排除条件/缺失值处理/多根因处理/波动说明。关键纪律：
- INCONCLUSIVE 不计入 PASSED；
- Selective Accuracy 与 Coverage 成对报告；
- 结果可回溯：experiment JSON → dataset 版本 → GT 版本 → seed 权威定义。

## 7. 阻塞与人工决策

1. **准确率评测 BLOCKED**：等待 GT 映射批准（提案已备好，批准后重跑 runner 自动生成 GT v2 并解除）。
2. UI 场景用例口径（TC-CREATE-002 断言横幅 vs GT 业务成功）需要人工裁决：改用例还是改 GT 语义。
3. Scripted Explorer 链、人工修复基线、无保护 LLM 修复：NOT_RUN（预算边界）。

阶段11 维持 READY_FOR_REVIEW——退出条件"GT 可隔离✓ 数据集可追溯✓ 实验可复现✓ 指标有口径✓ 结果可回溯✓"已满足技术项，但准确率评测仍在门禁后，不置 ACCEPTED。
