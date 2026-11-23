# R5-0 Git 差异报告

- 时间：2026-11-23（GMT+8）
- 约束：**只读审计**。仅执行 `status` / `branch` / `log` / `diff` / `remote` / `ls-files` / `check-ignore` / `git grep`。
  未执行 reset、clean、checkout --、stash、commit、push、fetch，未修改任何分支或文件。
- 状态：**DONE（待负责人确认）**

## 1. 仓库定位

| 项 | 值 |
|---|---|
| 工程根 | `D:/桌面/TraceGuard/traceguard`（外层 `D:/桌面/TraceGuard` 不是仓库） |
| 当前分支 | `main`，跟踪 `origin/main` |
| HEAD | `86ebae75245b03762466a4f90bdd3411fd02d334` |
| HEAD 提交者/时间 | zhoujy，2026-11-23 11:58:31 +0800 |
| HEAD 主题 | `fix: declare jsonschema runtime dependency (CI collection failed without it)` |
| remote.origin | `https://github.com/zjy110203/TraceGuard.git`（Public，fetch/push 同址） |
| 与 origin/main 差距 | `git rev-list --left-right --count origin/main...HEAD` → `0 0`（齐平） |
| 跟踪文件数 | 322 |

## 2. 工作区状态

`git status --short --branch` 输出仅 `## main...origin/main`；
`git status --porcelain=v1 --untracked-files=all` 为空。

结论：**无未提交修改、无已暂存修改、无未跟踪文件**。不存在"用户已有修改可能被覆盖"的风险。

## 3. 与 R4 基线 `bfdfc54` 的差异

指令给出的参考提交为 `bfdfc544f0aa6639cb5cca050ae9e13671dc201b`。
`git log bfdfc54..HEAD` 显示本机领先 8 个提交：

| 提交 | 主题 | 性质 |
|---|---|---|
| `d0ece75` | docs: update README to reflect R4 final state | 文档 |
| `085595d` | ci: exclude archived research docs from ruff; fix CI gates and ignore coverage temp | CI / 配置 |
| `fb24b9e` | ci: add collection diagnostics to locate ubuntu pytest failure | CI |
| `ce621a4` | ci: drop --cov from CI test step (pytest-cov incompatible on ubuntu runner) | CI |
| `65f98d8` | ci: upload pytest output artifact on failure for diagnosis | CI |
| `9152736` | ci: add pipefail so pytest exit code is not masked by tee | CI |
| `0cd676a` | ci: surface pytest errors as check annotations for diagnosis | CI |
| `86ebae7` | fix: declare jsonschema runtime dependency (CI collection failed without it) | 依赖修复 |

`git diff --stat bfdfc54..HEAD`：

```
 .github/workflows/ci.yml | 13 ++++-
 .gitignore               |  1 +
 README.md                | 133 ++++++++++++++++++++++++-----------------------
 pyproject.toml           |   4 +-
 requirements.txt         |   1 +
 5 files changed, 84 insertions(+), 68 deletions(-)
```

**关键判断**：基线之后的全部差异都是 CI、文档、依赖声明与忽略规则，
**没有任何业务源码或测试断言变更**。因此 R5 可以从当前 HEAD 起步，
无需（也不得）为了"回到 bfdfc54"而做批量回退。

## 4. CI 相关差异的实质

这些提交修复的是三个真实问题，不是降低标准：

1. `ruff format` 会重排 `docs/research/` 内逐字归档的第三方 Markdown Python 代码块 →
   通过 `pyproject.toml` 的 `extend-exclude = ["alembic/versions", "docs/research"]` 排除归档资料。
   范围有限且理由明确；R5-1 **不得**删除调研文档，也**不得**继续扩大排除规则。
2. `pytest -q | tee` 缺少 `pipefail` 时退出码被吞成 0，出现"假绿" → CI 已加 `set -o pipefail`，
   失败时上传 `pytest-output.txt` 并把错误写成 check annotation。
3. 干净 CI 环境缺 `jsonschema`（生产代码 `packages/contracts/loader.py`、
   `packages/agents/llm.py` 及测试 import）→ 已写入 `requirements.txt`。

## 5. 密钥与敏感文件审计（只读，未输出值）

| 检查 | 命令 | 结果 |
|---|---|---|
| `.env` 是否被跟踪 | `git ls-files --error-unmatch .env` | **未被跟踪** |
| `.env` 是否被忽略 | `git check-ignore -v .env` | `.gitignore:2:.env` → 已忽略 |
| `.env` 是否曾进入历史 | `git log --all --oneline -- .env` | 空 → 从未入库 |
| 跟踪文件中疑似密钥 | `git grep -l -I -E 'sk-[A-Za-z0-9_-]{16,}\|DEEPSEEK_API_KEY=...' HEAD` | 命中 2 个**测试文件** |
| 命中内容核实（脱敏） | 仅看脱敏上下文 | `tests/test_security_r4.py:261` 为 `sk-test***` 测试夹具；
`tests/test_verdict.py:122` 的 `optional-pass-cannot-mask-required` 中 `sk-requ...` 为误匹配 |

结论：**未发现真实密钥进入跟踪文件或 Git 历史**。不需要密钥轮换。
本轮未打印 `.env` 内容、未读取任何密钥值。

## 6. 风险与纪律提醒

- 仓库是 **Public**。任何未经批准的 push 都会公开源码与文档；R5 全程默认不 push。
- `origin/main` 的本地跟踪引用与 HEAD 齐平，但这**不等于**远端服务器当前状态
  （本轮未联网 fetch）。如需核验远端，须先获得网络访问批准。
- 若后续需要改动 CI 配置（属于源码/配置变更），必须先通过 R5-1 准入，且保留可审查 diff。

## 7. 结论

本机 Git 基线为 `86ebae7`，干净、与本地跟踪的远端齐平；
R4 描述中的 `bfdfc54` 已过期 8 个提交。差异内容仅为 CI/文档/依赖，
不存在需要回退的用户修改，也不存在密钥泄露。
