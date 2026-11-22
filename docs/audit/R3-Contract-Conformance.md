# R3 契约符合性审计（2026-11-22）

## 方法

- Schema 与代码一致性：`tests/test_contracts.py::test_on_disk_schemas_match_current_code` 对 12 份 Schema 逐一重生成比对（PASS）
- 规则可追溯性：`test_rule_index_matches_implementation` 用正则从 `semantic.py` 源码提取全部 `C##_CODE` 与 `SEMANTIC_RULES` 索引比对（PASS）
- 样例覆盖：19 合法 / 19 非法 / 12 语义反例，目录非空断言（PASS）

## 逐项结论

| 项 | 结论 | 证据 |
|---|---|---|
| 12 Schema 对应正式设计 | PASS（结构一致；语义以 semantic.py 为准） | 漂移测试 |
| dataclass 漏字段 | 未发现；沙箱 round-trip（序列化→load_case→身份一致）再次验证 | `test_repair_sandbox.py` |
| 101 条规则可追溯业务语义 | PASS，每条规则码有中文说明并有用例断言 | `SEMANTIC_RULES` + 用例 |
| 每条规则有对应测试 | 部分：规则码级断言覆盖核心规则（8 条具名 + 12 语义反例）；其余经由样例验证 | 见 gap |
| 正例/反例边界条件 | PASS（缺列/错型/越界/多字段/禁改类型等变异） | samples |
| C10 是否仍为 routing 视图 | 是（无新增字段，字段全部来自 Diagnosis） | C10_SCHEMA 注释 |
| C03 多源 Oracle 优先级 | 仍待确认（BLOCKED），未擅自定级 | decisions |

## Gap（如实记录）

1. 规则级具名测试仅覆盖代表性规则；101 条未逐一具名测试（由样例批量覆盖）。改进方向：为每条规则生成针对性反例。
2. C10 若未来需要独立 Disposition 记录（路由人/理由/时间），需人工确认字段后再扩展。
3. 契约全部 `draft`，未冻结——冻结必须人工批准。
