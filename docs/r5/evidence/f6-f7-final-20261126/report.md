# Verify report — mode=all @ 20261126-195754

Output directory: `.traceguard-tmp\r5\verify\20261126-195754-all-41256`

| step | status | exit | duration | passed | skipped | timeout | log |
|------|--------|------|----------|--------|---------|---------|-----|
| ruff-check | PASS | 0 | 0.1s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\ruff-check.log` |
| ruff-format-check | PASS | 0 | 0.1s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\ruff-format-check.log` |
| mypy | PASS | 0 | 0.4s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\mypy.log` |
| pytest | PASS | 0 | 807.6s | 852 | 1 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\pytest.log` |
| npm-ls | PASS | 0 | 0.9s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\npm-ls.log` |
| vue-tsc | PASS | 0 | 2.3s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\vue-tsc.log` |
| vite-build | PASS | 0 | 4.0s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\vite-build.log` |
| pytest-security | PASS | 0 | 187.1s | 101 | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\pytest-security.log` |
| product-e2e | PASS | 0 | 12.9s | 24 | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\product-e2e.log` |
| browser | PASS | 0 | 65.8s | 150 | 0 | - | `.traceguard-tmp\r5\verify\20261126-195754-all-41256\browser.log` |

## product-e2e — 24 recorded assertions

| check | verdict |
|-------|---------|
| product/isolation-layout | PASS work=.traceguard-tmp\r5\verify\20261126-195754-all-41256\product cases_before=0 outside_data_dir=0 platform_db=.traceguard-tmp\r5\verify\20261126-195754-all-41256\product\platform.db |
| product/teamflow-isolated-up | PASS url=http://127.0.0.1:56692 db=.traceguard-tmp\r5\verify\20261126-195754-all-41256\product\teamflow.db exists=True |
| product/platform-up | PASS url=http://127.0.0.1:56697 db=.traceguard-tmp\r5\verify\20261126-195754-all-41256\product\platform.db store_dirs=6 cases_dir=.traceguard-tmp\r5\verify\20261126-195754-all-41256\product\cases |
| product/frontend-built | PASS dist/index.html exists=True bytes=711 has_app_mount=True |
| product/frontend-served | PASS GET / -> 200 bytes=727 references_assets=True |
| product/workbench-ui-loads | PASS mount_children=1 text_len=589 console_errors=[] |
| product/explore-real-browser | PASS http=201 id=exp_6018be486862 status=COMPLETED goal=VERIFIED stop=GOAL_VERIFIED steps=5 policy=scripted is_llm=False inference=NOT_REAL_INFERENCE |
| product/trace-structured | PASS http=200 observations=5 actions=5 hash_hex_len=64 integrity=True no_plaintext_secret=True |
| product/compile-candidate | PASS http=201 candidate=cand_3de007387b status=PENDING_REVIEW steps=7 assertions=5 oracle_sources=['ui.url_path', 'ui.s5.passed', 'ui.s6.passed', 'ui.s7.passed', 'state.project_count'] checklist=3 |
| product/no-case-before-review | PASS cases_in_library=[] POST /chains -> 404 blocked=NOT_FOUND |
| product/human-approval | PASS http=200 status=APPROVED case_file=TC-E2E-20261126121437.json reviewer=e2e-reviewer |
| product/formal-case-written | PASS http=200 version=1 approved_by=e2e-reviewer:R5-4-5-REV-20261126121437 compiled_from=compiler:1.2.0 sources=['ui.url_path', 'ui.s5.passed', 'ui.s6.passed', 'ui.s7.passed', 'state.project_count'] independent_api_oracle=True data_refs=1 value_ref_bound=True no_plaintext=True |
| product/chain-executed | PASS http=201 chain=chain_20261126121444022756_TC-E2E-20261126121437 run=run_20261126121444000128_db0b28 run_status=COMPLETED verdict=PASSED exec=COMPLETED stages=['testcase', 'cleanup_pre', 'execution', 'verdict', 'graph', 'diagnosis', 'repair', 'cleanup', 'event_persistence', 'evidence_persistence', 'event_persistence', 'evidence_persistence'] persistence=OK graph_errors=[] |
| product/oracle-business-verdict | PASS verdict=PASSED state_oracle=a5:PASSED url_oracle=PASSED evaluated=5/5 observed_path='/projects' |
| product/events-persisted | PASS http=200 rows=42 own_run=42 written=27 duplicates=0 status=OK |
| product/evidence-persisted | PASS http=200 records=15 types=['dom_snapshot', 'network_response', 'screenshot'] written=15 duplicates=0 blobs=7 status=OK |
| product/blob-integrity | PASS blobs_checked=7 full_sha256=True problems=[] |
| product/graph-query | PASS nodes=10 subgraph_nodes=9 subgraph_edges=8 run_node_present=True |
| product/diagnosis-query | PASS http=200 diagnosis_status=INCONCLUSIVE disposition=NEEDS_MORE_EVIDENCE |
| product/run-details | PASS http=200 status=COMPLETED steps=7 verdict=PASSED report_stages=['testcase', 'cleanup_pre', 'execution', 'verdict', 'graph', 'diagnosis', 'repair', 'cleanup', 'event_persistence', 'evidence_persistence', 'event_persistence', 'evidence_persistence'] |
| product/negative-control | PASS http=201 verdict=FAILED run=run_20261126121446601464_d61679 failed_assertions=['a5'] actions_ok=True action_steps=4 |
| product/cancel-and-recover | PASS observed_state=RUNNING cancelled_run=run_20261126121448360899_9648f1 run_status=CANCELLED verdict=None cleanup_stage=True \| retry_run=run_20261126121448484520_ded25e retry_verdict=PASSED evidence_written=15 new_run_id=True |
| product/no-plaintext-secret | PASS files_scanned=12 leaks=[] |
| product/product-data-untouched | PASS path=data\traceguard.db identical=True |

## browser — 150 recorded assertions

fixture: seeded from .traceguard-tmp\r5\verify\20261126-195754-all-41256\product (copied to browser-20261126-201450-150595.db)

| check | verdict |
|-------|---------|
| pre/platform /health reachable | PASS |
| /#/overview/not blank | PASS |
| /#/overview/no console error | PASS |
| /#/overview/no page error | PASS |
| /#/overview/no API 4xx/5xx | PASS |
| /#/intents/not blank | PASS |
| /#/intents/no console error | PASS |
| /#/intents/no page error | PASS |
| /#/intents/no API 4xx/5xx | PASS |
| /#/candidates/not blank | PASS |
| /#/candidates/no console error | PASS |
| /#/candidates/no page error | PASS |
| /#/candidates/no API 4xx/5xx | PASS |
| /#/cases/not blank | PASS |
| /#/cases/no console error | PASS |
| /#/cases/no page error | PASS |
| /#/cases/no API 4xx/5xx | PASS |
| /#/explorer/not blank | PASS |
| /#/explorer/no console error | PASS |
| /#/explorer/no page error | PASS |
| /#/explorer/no API 4xx/5xx | PASS |
| /#/tasks/not blank | PASS |
| /#/tasks/no console error | PASS |
| /#/tasks/no page error | PASS |
| /#/tasks/no API 4xx/5xx | PASS |
| /#/runs/not blank | PASS |
| /#/runs/no console error | PASS |
| /#/runs/no page error | PASS |
| /#/runs/no API 4xx/5xx | PASS |
| /#/events/not blank | PASS |
| /#/events/no console error | PASS |
| /#/events/no page error | PASS |
| /#/events/no API 4xx/5xx | PASS |
| /#/evidence/not blank | PASS |
| /#/evidence/no console error | PASS |
| /#/evidence/no page error | PASS |
| /#/evidence/no API 4xx/5xx | PASS |
| /#/graph/not blank | PASS |
| /#/graph/no console error | PASS |
| /#/graph/no page error | PASS |
| /#/graph/no API 4xx/5xx | PASS |
| /#/diagnosis/not blank | PASS |
| /#/diagnosis/no console error | PASS |
| /#/diagnosis/no page error | PASS |
| /#/diagnosis/no API 4xx/5xx | PASS |
| /#/probes/not blank | PASS |
| /#/probes/no console error | PASS |
| /#/probes/no page error | PASS |
| /#/probes/no API 4xx/5xx | PASS |
| /#/repairs/not blank | PASS |
| /#/repairs/no console error | PASS |
| /#/repairs/no page error | PASS |
| /#/repairs/no API 4xx/5xx | PASS |
| /#/benchmark/not blank | PASS |
| /#/benchmark/no console error | PASS |
| /#/benchmark/no page error | PASS |
| /#/benchmark/no API 4xx/5xx | PASS |
| /#/system/not blank | PASS |
| /#/system/no console error | PASS |
| /#/system/no page error | PASS |
| /#/system/no API 4xx/5xx | PASS |
| overview/metric card navigates | PASS |
| breadcrumb/crumbs rendered | PASS |
| graph/node click shows detail panel | PASS |
| benchmark/empty/blocked state visible | PASS |
| repairs/page reachable | PASS |
| explorer/explorer page reachable | PASS |
| deep-link/run detail shows step rows | PASS |
| deep-link/run detail -> diagnosis tab | PASS |
| deep-link/diagnosis page renders verdict | PASS |
| deep-link/diagnosis -> evidence filtered | PASS |
| deep-link/evidence -> run detail | PASS |
| deep-link/diagnosis?run= prefills input | PASS |
| deep-link/diagnosis?run= auto-loads | PASS |
| 1920x1080 /#/overview/no horizontal overflow | PASS |
| 1920x1080 /#/intents/no horizontal overflow | PASS |
| 1920x1080 /#/candidates/no horizontal overflow | PASS |
| 1920x1080 /#/cases/no horizontal overflow | PASS |
| 1920x1080 /#/explorer/no horizontal overflow | PASS |
| 1920x1080 /#/tasks/no horizontal overflow | PASS |
| 1920x1080 /#/runs/no horizontal overflow | PASS |
| 1920x1080 /#/events/no horizontal overflow | PASS |
| 1920x1080 /#/evidence/no horizontal overflow | PASS |
| 1920x1080 /#/graph/no horizontal overflow | PASS |
| 1920x1080 /#/diagnosis/no horizontal overflow | PASS |
| 1920x1080 /#/probes/no horizontal overflow | PASS |
| 1920x1080 /#/repairs/no horizontal overflow | PASS |
| 1920x1080 /#/benchmark/no horizontal overflow | PASS |
| 1920x1080 /#/system/no horizontal overflow | PASS |
| 1440x900 /#/overview/no horizontal overflow | PASS |
| 1440x900 /#/intents/no horizontal overflow | PASS |
| 1440x900 /#/candidates/no horizontal overflow | PASS |
| 1440x900 /#/cases/no horizontal overflow | PASS |
| 1440x900 /#/explorer/no horizontal overflow | PASS |
| 1440x900 /#/tasks/no horizontal overflow | PASS |
| 1440x900 /#/runs/no horizontal overflow | PASS |
| 1440x900 /#/events/no horizontal overflow | PASS |
| 1440x900 /#/evidence/no horizontal overflow | PASS |
| 1440x900 /#/graph/no horizontal overflow | PASS |
| 1440x900 /#/diagnosis/no horizontal overflow | PASS |
| 1440x900 /#/probes/no horizontal overflow | PASS |
| 1440x900 /#/repairs/no horizontal overflow | PASS |
| 1440x900 /#/benchmark/no horizontal overflow | PASS |
| 1440x900 /#/system/no horizontal overflow | PASS |
| 1024x768 /#/overview/no horizontal overflow | PASS |
| 1024x768 /#/intents/no horizontal overflow | PASS |
| 1024x768 /#/candidates/no horizontal overflow | PASS |
| 1024x768 /#/cases/no horizontal overflow | PASS |
| 1024x768 /#/explorer/no horizontal overflow | PASS |
| 1024x768 /#/tasks/no horizontal overflow | PASS |
| 1024x768 /#/runs/no horizontal overflow | PASS |
| 1024x768 /#/events/no horizontal overflow | PASS |
| 1024x768 /#/evidence/no horizontal overflow | PASS |
| 1024x768 /#/graph/no horizontal overflow | PASS |
| 1024x768 /#/diagnosis/no horizontal overflow | PASS |
| 1024x768 /#/probes/no horizontal overflow | PASS |
| 1024x768 /#/repairs/no horizontal overflow | PASS |
| 1024x768 /#/benchmark/no horizontal overflow | PASS |
| 1024x768 /#/system/no horizontal overflow | PASS |
| 768x1024 /#/overview/no horizontal overflow | PASS |
| 768x1024 /#/intents/no horizontal overflow | PASS |
| 768x1024 /#/candidates/no horizontal overflow | PASS |
| 768x1024 /#/cases/no horizontal overflow | PASS |
| 768x1024 /#/explorer/no horizontal overflow | PASS |
| 768x1024 /#/tasks/no horizontal overflow | PASS |
| 768x1024 /#/runs/no horizontal overflow | PASS |
| 768x1024 /#/events/no horizontal overflow | PASS |
| 768x1024 /#/evidence/no horizontal overflow | PASS |
| 768x1024 /#/graph/no horizontal overflow | PASS |
| 768x1024 /#/diagnosis/no horizontal overflow | PASS |
| 768x1024 /#/probes/no horizontal overflow | PASS |
| 768x1024 /#/repairs/no horizontal overflow | PASS |
| 768x1024 /#/benchmark/no horizontal overflow | PASS |
| 768x1024 /#/system/no horizontal overflow | PASS |
| 390x844 /#/overview/no horizontal overflow | PASS |
| 390x844 /#/intents/no horizontal overflow | PASS |
| 390x844 /#/candidates/no horizontal overflow | PASS |
| 390x844 /#/cases/no horizontal overflow | PASS |
| 390x844 /#/explorer/no horizontal overflow | PASS |
| 390x844 /#/tasks/no horizontal overflow | PASS |
| 390x844 /#/runs/no horizontal overflow | PASS |
| 390x844 /#/events/no horizontal overflow | PASS |
| 390x844 /#/evidence/no horizontal overflow | PASS |
| 390x844 /#/graph/no horizontal overflow | PASS |
| 390x844 /#/diagnosis/no horizontal overflow | PASS |
| 390x844 /#/probes/no horizontal overflow | PASS |
| 390x844 /#/repairs/no horizontal overflow | PASS |
| 390x844 /#/benchmark/no horizontal overflow | PASS |
| 390x844 /#/system/no horizontal overflow | PASS |
| a11y/Tab moves focus into document | PASS |

**Result: PASS**
