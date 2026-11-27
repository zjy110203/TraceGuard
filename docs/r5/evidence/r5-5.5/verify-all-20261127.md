# R5-5.5 Full Verification — 2026-11-27

Result: **PASS** (`scripts/verify_all.py --mode all --build-web`).

Executed against an isolated clean R5-only worktree so the pre-existing R6 changes in the user workspace were excluded. The run used a short explicit Windows temp/output path. Four tests skipped: three require the absent formal database and one requires unavailable symlink privilege; the full pytest run reported one Starlette/httpx deprecation warning.

| Gate | Result | Duration | Passed | Skipped |
|---|---:|---:|---:|---:|
| ruff-check | PASS | 1.8s | — | 0 |
| ruff-format-check | PASS | 0.1s | — | 0 |
| mypy | PASS | 0.6s | — | 0 |
| pytest | PASS | 1062.9s | 973 | 4 |
| npm-ls | PASS | 2.9s | — | 0 |
| vue-tsc | PASS | 2.3s | — | 0 |
| vite-build | PASS | 4.2s | — | 0 |
| pytest-security | PASS | 248.8s | 216 | 0 |
| product-e2e | PASS | 13.4s | 24 | 0 |
| browser | PASS | 65.6s | 150 | 0 |

Full pytest: **973 passed, 4 skipped**. Security pytest: **216 passed**. Product flow: **24 passed**. Browser acceptance: **150 passed**.

Sanitized gate logs are in `verify-all-20261127/logs/`; the machine-readable summary is `verify-all-20261127.json`.
