# R4 Skill / Plugin 调研报告

- 时间：2026-11-22（R4 开工前）
- 方法：实际检索 Workbuddy 本机已安装插件与 Skill 市场，未猜测名称。

## 1. 检索结果

### 1.1 已安装 Connector

`list_installed_plugins` 结果：**无已安装 Connector**。

### 1.2 Connector 市场检索

关键词：browser / frontend / UI / design。返回 6 个候选：

| 名称 | 功能 | 是否适用 |
|---|---|---|
| Aholo Lux3D | 3D/图片生成 | 否 |
| RestoSuite 商户助手 | 餐饮运营平台 | 否 |
| 闪剪AI | 素材/笔记创作 | 否 |
| RabbitVis | 海报/UI 视觉稿生成 | 否（生成设计稿，不产出 Vue 代码，与工作台重构无关） |
| 腾讯云可观测 TCOP | 云监控排障 | 否 |
| 百度地图 | 位置服务 | 否 |

结论：**无适用 Connector，不安装。** 安装来源不明或权限过宽的插件违反本轮安全约束。

### 1.3 Skill 市场检索

关键词：browser automation / 前端设计 / UI 设计 / visual regression / Vue。
结果：**无匹配 Skill。**

### 1.4 本机已具备能力（实际验证）

| 能力 | 来源 | 权限边界 | 结论 |
|---|---|---|---|
| Playwright ≥1.49 | `requirements.txt`，已在项目 `.venv` 内 | 仅项目 venv；测试均指向 127.0.0.1 | ✅ 用于真实浏览器验收与截图 |
| Chrome | 本机已装（R3 沙箱回放已验证） | — | ✅ |
| Vite / vue-tsc | `apps/web` 本地 `node_modules` | 无全局安装 | ✅ |
| 文件读写 / 命令执行 | Workbuddy 内建工具 | 限于项目目录 | ✅ |

## 2. 安全评估

- 未安装任何新插件 → 无新增文件/网络/Shell/凭据权限面。
- 浏览器验收使用项目内 Playwright + 本机 Chrome，目标 URL 全部为 `127.0.0.1`。
- 无外部凭据需求；无遥测引入。

## 3. 决策

1. 不安装任何新 Connector / Skill。
2. 前端设计遵循自研设计系统（`TraceGuard-Design-System.md`），参考 Linear/Sentry/Datadog/GitHub Actions 的设计思想，不复制界面。
3. 真实浏览器验收用 pytest + Playwright（同步 API）+ Chrome，产物（截图）存 `docs/ui/screenshots/`。
