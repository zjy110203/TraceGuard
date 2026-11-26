# Verify report — mode=all @ 20261126-211640

Output directory: `.traceguard-tmp\r5\verify\20261126-211640-all-27972`

| step | status | exit | duration | passed | skipped | timeout | log |
|------|--------|------|----------|--------|---------|---------|-----|
| ruff-check | PASS | 0 | 0.1s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-211640-all-27972\ruff-check.log` |
| ruff-format-check | PASS | 0 | 0.1s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-211640-all-27972\ruff-format-check.log` |
| mypy | PASS | 0 | 0.4s | - | 0 | - | `.traceguard-tmp\r5\verify\20261126-211640-all-27972\mypy.log` |
| pytest | FAIL | 1 | 871.0s | 897 | 1 | - | `.traceguard-tmp\r5\verify\20261126-211640-all-27972\pytest.log` |
| npm-ls | NOT_RUN | -1 | 0.0s | - | 0 | - | `` |
| vue-tsc | NOT_RUN | -1 | 0.0s | - | 0 | - | `` |
| vite-build | NOT_RUN | -1 | 0.0s | - | 0 | - | `` |
| pytest-security | NOT_RUN | -1 | 0.0s | - | 0 | - | `` |
| product-e2e | NOT_RUN | -1 | 0.0s | - | 0 | - | `` |
| browser | NOT_RUN | -1 | 0.0s | - | 0 | - | `` |

- **npm-ls**: NOT_RUN — not run: an earlier gate stopped the run
- **vue-tsc**: NOT_RUN — not run: an earlier gate stopped the run
- **vite-build**: NOT_RUN — not run: an earlier gate stopped the run
- **pytest-security**: NOT_RUN — not run: an earlier gate stopped the run
- **product-e2e**: NOT_RUN — not run: an earlier gate stopped the run
- **browser**: NOT_RUN — not run: an earlier gate stopped the run

**Result: FAIL — failed: pytest, npm-ls, vue-tsc, vite-build, pytest-security, product-e2e, browser**
