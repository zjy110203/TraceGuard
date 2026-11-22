# R4 GT 映射提案（R4-GT-Mapping-Proposal.md）

- 时间：2026-11-22
- 性质：**提案**——未经项目负责人批准前，任何未确认的 primary_cause 不进入正式准确率统计（沿用 R3 门禁）。
- 依据：全项目代码级证据链核查（文件:行号见下）。

## 1. 权威来源确认

| 数据 | 权威来源 | 证据 |
|---|---|---|
| fault_id 清单（5 个） | `apps/teamflow/faults.py:24-46`（FAULT_SPECS，v1.1 阶段1 规定） | API/NET/DB/UI/TIME |
| scenario 清单（6 个） | `scripts/seed_scenarios.py:15-52` → `apps/teamflow/service.py:183-209`（seed_scenario 落库） | TF-BASE-001 + 5 个 `-S1` |
| expected_verdict | `eval_ground_truth.expected_json`（seed_scenarios.py:20,26,32,…） | 语义与引擎一致（PASSED/FAILED） |
| expected_project_count | 同上（eval 平面内部消费） | 与 verdict 评测无冲突 |
| primary_cause | **代码中无任何权威定义** | R3 报告 :130 挂起项 |
| fault_class（fault_id→机制） | **代码中无权威映射**；唯一"证据"是契约样例生成器 build_contracts.py:385-391 的演示数据（S8，非真实场景） | UNRESOLVED |
| benchmark/scenarios/ | 空目录，无引用 | 未实现 |
| benchmark/cases/（13 用例） | 手工用例库，与 scenario 无 ID 映射代码 | 两套体系，无对应关系 |

## 2. 映射表

| fault_id / scenario | 原始场景 | 权威来源 | 期望配置（verdict） | 候选机制（**仅启发式对齐，未获批准**） | 证据 | 状态 |
|---|---|---|---|---|---|---|
| TF-BASE-001 | 基线：正常创建项目 | seed_scenarios.py:15-21 | PASSED（count=1） | NONE（无故障） | seed 定义 | verdict 可用；primary_cause=NONE 无争议 |
| TF-API-001-S1 | 创建接口 500 | seed_scenarios.py:23-28 | FAILED（count=0） | API（FAULT_MECHANISMS 枚举） | faults.py:24 label「API 异常」+ diagnosis.py:34 枚举 | verdict 可用；**primary_cause UNRESOLVED** |
| TF-NET-001-S1 | 请求延迟/超时 | seed_scenarios.py:30-34 | PASSED（count=1） | NETWORK | faults.py:25「网络超时」+ diagnosis.py:35 | verdict 可用；**primary_cause UNRESOLVED** |
| TF-DB-001-S1 | 持久化失败回滚 | seed_scenarios.py:36-40 | FAILED（count=0） | PERSISTENCE | faults.py:26「持久化失败」+ diagnosis.py:36 | verdict 可用；**primary_cause UNRESOLVED** |
| TF-UI-001-S1 | 成功提示缺失但业务成功 | seed_scenarios.py:42-46 | PASSED（count=1） | UI_RENDERING | faults.py:27「UI 表现异常」+ diagnosis.py:37；契约样例 fault_class="TF-UI-001" 仅为演示 | verdict 可用；**primary_cause UNRESOLVED** |
| TF-TIME-001-S1 | 列表刷新延迟 | seed_scenarios.py:48-52 | PASSED（count=1） | TIMING | faults.py:28「异步时序异常」+ diagnosis.py:38 | verdict 可用；**primary_cause UNRESOLVED** |

说明：候选机制列是"fault_id 中文 label ↔ diagnosis.py FAILURE_MECHANISMS 枚举"的启发式对齐，**代码中不存在该映射，不构成权威依据**。批准方式建议：项目负责人逐行确认或提供 fault_id→(primary_cause, fault_class) 官方对照表；批准后由脚本写入 GT 新版本（append-only），不覆盖旧版本。

## 3. 门禁执行（本轮已落实）

1. benchmark 引擎的 `GroundTruthRecord.primary_cause/fault_class` 在本轮所有落盘 GT 中取值 `UNRESOLVED`。
2. `ExperimentManager.run_diagnosis` 检测到 GT 无有效 primary_cause 时：primary_cause_accuracy / selective_accuracy / macro_f1 输出 **None（BLOCKED）**，不输出 0 假装跑过（engine 修改 + 测试）。
3. **可正常评测**（分母可靠、与 GT 映射无关）：false_pass_rate（verdict 对比）、coverage（verdict 回答率）、probe_count、replay_stability、locator_validity、assertion_preservation、repair_validation_success。
4. 待批准后：重跑 runner 自动生成 GT v2（含真实 primary_cause），准确率类指标解除 BLOCKED。

## 4. 需要人工决策的问题

| # | 决策项 | 影响 |
|---|---|---|
| 1 | 批准/驳回上表 5 行候选映射 | 解除准确率评测 BLOCKED |
| 2 | benchmark/scenarios/ 目录定位（废弃 or 实现场景文件） | 数据集导入路径 |
| 3 | benchmark/cases（13 用例）与 eval scenario 的关系（无映射代码） | Test Generation track 的场景覆盖口径 |
