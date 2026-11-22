# C01～C12 契约 Schema 目录

- 依据：项目资料/05-统一契约与模块接口设计.md
- 状态：**契约地图已形成；正式字段与 JSON Schema 待逐项冻结**
- 纪律：本目录中的文件是版本治理骨架与示例，不是已批准冻结的契约。任何字段不得被当作正式契约引用，除非经过评审并在 `registry.json` 中标记为 `frozen: true`。

## 契约清单（与 v1.1 一一对应）

| 编号 | 名称 | 核心对象 | 生产者 | 主要消费者 | 冻结状态 |
|---|---|---|---|---|---|
| C01 | TestIntent | TestIntent | Test Intent Manager | Explorer、Compiler | 待冻结 |
| C02 | TestCase | TestCase / TestCaseVersion | Compiler / TestCase Service | Execution | 待冻结 |
| C03 | Oracle | OracleSpec / Assertion Oracle Binding | Oracle Manager | Verifier、Execution | 待冻结 |
| C04 | Execution | Task / Run / Step | Orchestrator、Engine | Event、Evidence、UI | 待冻结 |
| C05 | Event | Event Envelope | Runner、Browser、Probe、服务 | Event Collector | 待冻结 |
| C06 | Evidence | Evidence Record | Evidence Manager | Correlation、Diagnosis | 待冻结 |
| C07 | Evidence Graph | Node / Edge | Correlation Service | Diagnosis | 待冻结 |
| C08 | Diagnosis | Hypothesis / Diagnosis | Diagnosis Workflow | Disposition | 待冻结 |
| C09 | Probe | Probe Definition / ProbeExecution | Probe Registry / Engine | Diagnosis | 待冻结 |
| C10 | Disposition | Disposition | Disposition Router | Defect、Repair、环境处理 | 待冻结 |
| C11 | Repair | RepairProposal / Approval Ref / Regression Ref | Repair Service | Approval、Regression、TestCase | 待冻结 |
| C12 | Benchmark | Dataset / GroundTruth / Experiment / EvaluationResult | Benchmark Service | 评测与报告 | 示例（阶段1起记录） |

## 版本治理规则

1. 每个契约一个 JSON Schema 文件，命名 `Cxx_<name>.schema.json`。
2. `registry.json` 记录每个契约的 `id / name / version / status / schema_file / owner_role`。
3. `status` 取值：`placeholder` | `draft` | `frozen`。只有评审通过才置 `frozen`。
4. 字段变更遵循项目资料/00 的版本规则（MAJOR/MINOR/PATCH），并在此登记。
5. 阶段 1 仅 C12 有示例结构，用于记录 Ground Truth 与场景元数据；其余仍为占位。
