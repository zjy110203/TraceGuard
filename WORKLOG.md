
## 2026-11-26 F6-A
Verified clean bb8d97f branch and origin; preserved all prior commits. Formal reads moved under shared single-process case locks, digest CAS and immutable target refusal; identical proposals cannot adopt files. Added real threaded races and idempotency assertions. Evidence: .traceguard-tmp/r5/f6-final/f6-a.log.

## 2026-11-26 F6-B / F6-C
Implemented durable INTENT -> COMMITTED -> DONE journal protocol, ownership-marked immutable formal files, fsync, fail-closed mismatch recovery, append-only torn-tail preservation and immutable rollback API/UI. Real Windows subprocess deaths cover intent/temp/replace/commit/state/recovery. Added HTTP races, audit/replay assertions and rollback write-failure retry. Final focused validation: 81 passed (f6-final-v2.log), plus 18 passed after stricter JSONL tail handling (f6-jsonl-final.log). Earlier failing logs retained. Supporting F7 work: real locator-failure evidence/diagnosis, controlled demo UI revisions, sandbox deployment binding and secret provider.

## 2026-11-26 F7 unified product loop
Real Chromium test passed in 85.62s. Single Explorer exp_b62a0741d78f -> cand_141e5b5154 -> TC-CAND-CE3F2DDE97 -> failed run run_20261126113958903500_830c7b -> supported real locator diagnosis -> rp_968164f1ec90 -> real replay PASSED -> v2 promotion -> API process restart -> regression PASSED -> UI calls official rollback API -> immutable v3 -> second API process restart -> expected FAILED execution of historical v1 selector. All lineage assertions read persisted stores. Evidence and screenshots: docs/r5/evidence/f6-f7-20261126. Full aggregate currently running; no aggregate/CI PASS claimed yet.

## 2026-11-26 final invariant review
Recheck proposal creation against current disk base under the shared case lock; idempotent promote returns its own version. Revalidate committed/DONE file ownership on restart, expose BLOCKED on missing/changed assets, and refuse further mutation. Add explicit mock actor aliases to the common human-review policy. Correct persisted race coverage to use different contents in its nonidentical scenario. Focused Repair/Candidate/API/Diagnosis regression: 138 passed, 1 warning, raw f6-hardening.log retained. A fresh full aggregate will validate this final code; the earlier aggregate remains a separate run.

## 2026-11-26 aggregate evidence isolation
Found the existing all-mode runner used the same pytest.log and pytest-tmp for full Python and focused Security, allowing later gates to overwrite evidence. Preserved the original full log before overwrite; assigned the Security gate its own name/log/basetemp without changing test scope or assertions. Verifier status/timeout regression: 19 passed (verifier-evidence.log). A fresh all-mode run will cover the final runner and product code together.

Verifier test import spacing failed Ruff in run 20261126-195712-all-47748. Stopped only that superseded run and preserved its lint log/aborted.json. Fixed imports with Ruff, verified full Ruff/Format, and start a fresh full aggregate. No failing run is counted as PASS.

## 2026-11-26 final acceptance
Code baseline ef49bc7: full all-mode aggregate PASS in .traceguard-tmp\r5\verify\20261126-195754-all-41256; Python 852 passed / 1 skipped (Windows symlink WinError 1314), Security 101, Product 24, Browser 150. All 10 stages ran in this one aggregate. Code CI run 45815263410 success, 5/5 jobs. Final same-lineage Chromium E2E data/screenshot/hash archive at docs/r5/evidence/f6-f7-final-20261126. Historical records retained; D8 BLOCKED, M4 NOT_RUN. Final documentation-only push CI will be verified separately.
