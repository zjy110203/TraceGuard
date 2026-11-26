<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import { messageOf } from '../composables/useAsync'
import type { ExplorationSummary, ExplorationTrace, PolicyCatalogue } from '../api/types'
import PageHeader from '../components/PageHeader.vue'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import EmptyState from '../components/EmptyState.vue'
import AppIcon from '../components/AppIcon.vue'
import { navigate } from '../router'

const policies = ref<PolicyCatalogue | null>(null)
const list = ref<ExplorationSummary[]>([])
const listLoading = ref(true)
const listError = ref('')

const selected = ref<ExplorationTrace | null>(null)
const detailLoading = ref(false)
const detailError = ref('')

// ---- run form ----
const intentRef = ref('explore:teamflow-login')
const baseUrl = ref('http://127.0.0.1:8001')
const scenario = ref('teamflow-login')
const scenarioVersion = ref('')
const scenarioPath = ref('normal')
watch(scenario, () => { scenarioVersion.value = ''; scenarioPath.value = 'normal' })
const goal = ref('登录 TeamFlow 并进入项目列表')
const expectedOutcomes = ref('项目列表页面可见\n登录表单不再显示')
const target = ref('/projects')
const requiredAssertions = ref('')
const constraints = ref('')
const policy = ref('scripted')
const maxSteps = ref(8)
const running = ref(false)
const runError = ref('')
const runNotice = ref('')

const activePolicy = computed(() => policies.value?.policies.find((p) => p.verified) ?? null)
const llmPolicy = computed(() => policies.value?.policies.find((p) => p.is_llm) ?? null)
const supportedScenarios = computed(() => policies.value?.supported_scenarios ?? {})

function lines(value: string): string[] {
  return value
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
}

/** Goal verdict is a separate axis from execution status/stop reason. */
const goalStatus = computed(() => selected.value?.goal_status ?? '')
const goalVerified = computed(() => goalStatus.value === 'VERIFIED')

const goalStatusTone = computed(() => {
  switch (goalStatus.value) {
    case 'VERIFIED':
      return 'ok'
    case 'NOT_VERIFIED':
      return 'bad'
    case 'INCONCLUSIVE':
      return 'warn'
    default:
      return 'neutral'
  }
})

async function loadList(): Promise<void> {
  listLoading.value = true
  listError.value = ''
  try {
    list.value = await api.explorations()
    if (!selected.value && list.value.length) {
      await openTrace(list.value[0].exploration_id)
    }
  } catch (err) {
    listError.value = messageOf(err)
  } finally {
    listLoading.value = false
  }
}

async function loadPolicies(): Promise<void> {
  try {
    policies.value = await api.explorationPolicies()
  } catch {
    policies.value = null
  }
}

async function openTrace(id: string): Promise<void> {
  detailLoading.value = true
  detailError.value = ''
  try {
    selected.value = await api.exploration(id)
  } catch (err) {
    detailError.value = messageOf(err)
  } finally {
    detailLoading.value = false
  }
}

async function run(): Promise<void> {
  running.value = true
  runError.value = ''
  runNotice.value = ''
  try {
    const trace = await api.runExploration({
      intent_ref: intentRef.value.trim(),
      goal: goal.value.trim(),
      expected_outcomes: lines(expectedOutcomes.value),
      target: target.value.trim(),
      required_assertions: lines(requiredAssertions.value),
      constraints: lines(constraints.value),
      scenario: scenario.value,
      scenario_version: scenarioVersion.value || undefined,
      path: scenarioPath.value,
      base_url: baseUrl.value.trim(),
      policy: policy.value,
      max_steps: Number(maxSteps.value),
    })
    selected.value = trace
    runNotice.value =
      trace.status === 'COMPLETED'
        ? `探索执行完成 · 停止原因 ${trace.stop_reason || '—'} · 目标验证 ${trace.goal_status || '—'}`
        : `探索执行失败：${trace.error ?? '未知错误'}`
    await loadList()
  } catch (err) {
    runError.value = messageOf(err)
  } finally {
    running.value = false
  }
}

onMounted(() => {
  void loadPolicies()
  void loadList()
})

// First failed/rejected action makes the trace's failure legible at a glance.
const rejectedCount = computed(
  () => selected.value?.action_sequence.filter((a) => a.rejected).length ?? 0,
)

// ---- compile to candidate (Trace -> Compiler -> review queue) ----
const compiling = ref(false)
const compileError = ref('')
const compiled = ref<{ candidate_id: string; case_id: string; status: string } | null>(null)

const canCompile = computed(
  () =>
    !!selected.value &&
    selected.value.status === 'COMPLETED' &&
    goalVerified.value &&
    !compiling.value,
)

async function compile(): Promise<void> {
  if (!selected.value) return
  compiling.value = true
  compileError.value = ''
  compiled.value = null
  try {
    const cand = await api.compileExploration(selected.value.exploration_id, {})
    compiled.value = cand
  } catch (err) {
    compileError.value = messageOf(err)
  } finally {
    compiling.value = false
  }
}

function actionSummary(action: { tool?: string; args?: Record<string, unknown> }): string {
  const args = action.args ?? {}
  if (action.tool === 'navigate') return String(args.url ?? '')
  if (action.tool === 'fill') return `${args.selector} = ${args.value}`
  if (action.tool === 'click') return String(args.selector ?? '')
  if (action.tool === 'wait') return `${args.ms ?? 0} ms`
  return ''
}
</script>

<template>
  <section class="page">
    <PageHeader
      title="探索器"
      subtitle="受控 Agent 探索闭环：页面观察 → 策略决策 → 安全校验 → 确定性执行 → 轨迹留存。探索成功不等于测试通过。"
    >
      <button class="btn" type="button" :disabled="listLoading" @click="loadList">
        <AppIcon name="refresh" :size="14" /> 刷新
      </button>
    </PageHeader>

    <!-- policy honesty banner -->
    <div class="policy-bar">
      <div class="policy-main">
        <span class="pill" :class="activePolicy ? 'info' : 'neutral'">
          {{ activePolicy?.label ?? '策略未知' }}
        </span>
        <span class="small muted">
          {{ activePolicy?.note ?? '正在读取策略目录…' }}
        </span>
      </div>
      <div v-if="llmPolicy" class="policy-llm">
        <span class="pill warn">LLM · 未启用</span>
        <span class="small muted">{{ policies?.llm_blocked_reason }}</span>
      </div>
    </div>

    <div class="explorer-grid">
      <!-- run panel -->
      <div class="card run-card">
        <h2 class="card-title"><AppIcon name="play" :size="14" /> 发起探索任务</h2>
        <p class="hint">
          目标仅允许本机地址（127.0.0.1 / localhost）。动作白名单：navigate / fill / click / wait /
          snapshot；步数与时长由安全网关硬性约束。
        </p>

        <div class="field">
          <label for="exp-intent">测试意图引用</label>
          <input id="exp-intent" v-model="intentRef" data-testid="exp-intent-ref" placeholder="explore:..." />
        </div>
        <div class="field">
          <label for="exp-scenario">业务场景</label>
          <select id="exp-scenario" v-model="scenario" data-testid="exp-scenario">
            <option v-for="(note, id) in supportedScenarios" :key="id" :value="id">
              {{ id }}
            </option>
          </select>
          <label for="exp-version">场景版本</label>
          <select id="exp-version" v-model="scenarioVersion">
            <option value="">保持现有默认版本</option>
            <option v-for="version in policies?.scenario_versions?.[scenario] ?? []" :key="version" :value="version">{{ version }}{{ version === '2.1.0' ? '（新版路径目标，显式选择）' : '' }}</option>
          </select>
          <label for="exp-path">场景路径</label>
          <select id="exp-path" v-model="scenarioPath">
            <option value="normal">Normal</option><option value="exception">Exception</option><option value="boundary">Boundary</option><option value="recovery">Recovery</option>
          </select>
          <button v-if="scenarioVersion === '2.1.0'" class="btn" type="button" @click="goal = ''; target = ''; expectedOutcomes = ''; requiredAssertions = ''; maxSteps = 30">使用所选路径的内置目标</button>
          <p class="micro muted">{{ supportedScenarios[scenario] || '未注册场景将被拒绝（UNSUPPORTED_SCENARIO）' }}</p>
        </div>
        <div class="field">
          <label for="exp-goal">业务目标（goal）</label>
          <input id="exp-goal" v-model="goal" data-testid="exp-goal" placeholder="用户要达成什么业务结果" />
        </div>
        <div class="field">
          <label for="exp-outcomes">预期结果（每行一条）</label>
          <textarea id="exp-outcomes" v-model="expectedOutcomes" rows="2" data-testid="exp-outcomes"></textarea>
        </div>
        <div class="field">
          <label for="exp-target-path">目标页面 / 路由（target）</label>
          <input id="exp-target-path" v-model="target" class="mono" data-testid="exp-target-path" placeholder="/projects" />
        </div>
        <div class="field">
          <label for="exp-assertions">必须满足的断言（每行一条，state.key=json）</label>
          <textarea id="exp-assertions" v-model="requiredAssertions" rows="2" class="mono" data-testid="exp-required-assertions" placeholder="可选"></textarea>
        </div>
        <div class="field">
          <label for="exp-constraints">环境与测试约束（每行一条）</label>
          <textarea id="exp-constraints" v-model="constraints" rows="2" data-testid="exp-constraints" placeholder="可选"></textarea>
        </div>
        <div class="field">
          <label for="exp-policy">决策策略</label>
          <select id="exp-policy" v-model="policy" data-testid="exp-policy">
            <option v-for="p in policies?.policies.filter((p) => p.verified) ?? []" :key="p.id" :value="p.id">
              {{ p.label }}
            </option>
          </select>
        </div>
        <div class="field">
          <label for="exp-target">目标地址</label>
          <input id="exp-target" v-model="baseUrl" class="mono" data-testid="exp-base-url" />
        </div>
        <div class="field">
          <label for="exp-steps">最大步数</label>
          <select id="exp-steps" v-model="maxSteps">
            <option :value="5">5</option>
            <option :value="8">8</option>
            <option :value="12">12</option>
            <option :value="20">20</option>
          </select>
        </div>

        <button class="btn primary block" type="button" :disabled="running" @click="run">
          <AppIcon :name="running ? 'clock' : 'play'" :size="14" />
          {{ running ? '探索执行中…' : '启动探索' }}
        </button>
        <p v-if="runError" class="error-text">{{ runError }}</p>
        <p v-if="runNotice" class="notice-text">{{ runNotice }}</p>

        <div class="invariants">
          <p class="micro">安全不变式</p>
          <ul class="inv-list">
            <li><AppIcon name="check" :size="12" /> 仅批准域名可达</li>
            <li><AppIcon name="check" :size="12" /> 页面文本永不授权</li>
            <li><AppIcon name="check" :size="12" /> 无 shell / 文件 / 网络越权</li>
            <li><AppIcon name="check" :size="12" /> 显式 stop_reason</li>
          </ul>
        </div>
      </div>

      <!-- detail -->
      <div class="card detail-card">
        <h2 class="card-title"><AppIcon name="layers" :size="14" /> 探索轨迹</h2>

        <StateBlock
          :loading="detailLoading"
          :error="detailError"
          :empty="!selected"
          empty-hint="选择左侧历史记录，或发起一次新的探索任务。"
        >
          <template v-if="selected">
            <div class="meta-row">
              <span class="mono">{{ selected.exploration_id }}</span>
              <StatusPill :value="selected.status" />
              <span class="tag">{{ selected.policy_label }}</span>
              <span v-if="!selected.is_real_model" class="pill plain neutral">非真实模型调用</span>
              <span v-if="rejectedCount" class="pill bad">{{ rejectedCount }} 次拒绝</span>
              <span v-if="selected.redacted_fields" class="pill plain warn" :title="'凭据类参数已在服务端脱敏'">
                {{ selected.redacted_fields }} 个敏感字段已脱敏
              </span>
            </div>

            <ul class="kv">
              <li><span>执行状态</span><StatusPill :value="selected.status" /></li>
              <li>
                <span>停止原因</span>
                <!-- PLAN_EXHAUSTED / STEP_BUDGET ≠ 目标达成：三个状态轴分开展示 -->
                <span class="mono" :data-testid="'stop-reason'">{{ selected.stop_reason || '—' }}</span>
              </li>
              <li>
                <span>目标验证</span>
                <span
                  class="pill"
                  :class="goalStatusTone"
                  data-testid="goal-status"
                  :title="selected.goal_reason || ''"
                >
                  {{ selected.goal_status || 'NOT_EVALUATED' }}
                </span>
              </li>
              <li>
                <span>测试用例裁决</span>
                <span class="pill" :class="selected.test_case_verdict === 'PASSED' ? 'ok' : selected.test_case_verdict === 'FAILED' ? 'bad' : 'warn'" data-testid="test-case-verdict">
                  {{ selected.test_case_verdict || 'INCONCLUSIVE' }}
                </span>
              </li>
              <li><span>意图引用</span><span class="mono">{{ selected.test_intent_ref || '—' }}</span></li>
              <li>
                <span>业务目标</span>
                <span>{{ selected.test_intent?.goal || '—' }}</span>
              </li>
              <li>
                <span>预期结果</span>
                <span>{{ selected.test_intent?.expected_outcomes?.length
                  ? selected.test_intent.expected_outcomes.join('；') : '—' }}</span>
              </li>
              <li>
                <span>目标页面</span>
                <span class="mono">{{ selected.test_intent?.target || selected.target || '—' }}</span>
              </li>
              <li><span>Agent 版本</span><span class="mono">{{ selected.agent_version }}</span></li>
              <li><span>浏览器</span><span class="mono">{{ selected.browser_version }}</span></li>
              <li>
                <span>轨迹哈希</span>
                <!-- No .truncate here: its white-space:nowrap would stop the
                     long sha256 from wrapping and push the page wider at 390px. -->
                <span class="mono hash" :title="selected.source_trace_hash">
                  {{ selected.source_trace_hash || '—' }}
                </span>
              </li>
            </ul>

            <!-- goal checks: evidence per condition, deferred ones marked -->
            <ul v-if="selected.goal_checks?.length" class="goal-checks" data-testid="goal-checks">
              <li v-for="(c, i) in selected.goal_checks" :key="i">
                <AppIcon :name="c.deferred ? 'clock' : c.ok ? 'check' : 'x'" :size="12" />
                <span class="mono small">{{ c.name }}</span>
                <span class="small">预期 {{ c.expected || '（未声明）' }}</span>
                <span class="small muted">观察 {{ c.observed ? c.observed.slice(0, 60) : '—' }}</span>
                <span v-if="c.deferred" class="pill plain neutral">运行时 Oracle 验证</span>
              </li>
            </ul>

            <p v-if="selected.error" class="error-text run-err">{{ selected.error }}</p>

            <!-- Trace -> Compiler handoff. Compilation never writes the case
                 library; it only queues a candidate for human review. -->
            <div class="compile-bar">
              <button class="btn sm" type="button" :disabled="!canCompile" data-testid="compile-candidate" @click="compile">
                <AppIcon name="layers" :size="13" />
                {{ compiling ? '编译中…' : '编译为候选' }}
              </button>
              <span class="small muted">
                {{
                  selected.status !== 'COMPLETED'
                    ? '仅执行状态 COMPLETED 的轨迹可编译。'
                    : !goalVerified
                      ? '仅业务目标 VERIFIED 可编译。负向路径即使符合预期（PASSED），也不能据此生成正式业务断言。'
                      : '编译器为确定性规则（非 LLM），产物需人工评审后才会成为正式用例。'
                }}
              </span>
            </div>
            <p v-if="compileError" class="error-text" role="alert">{{ compileError }}</p>
            <p v-if="compiled" class="notice-text" role="status">
              已生成候选 <span class="mono">{{ compiled.candidate_id }}</span>（case_id
              <span class="mono">{{ compiled.case_id }}</span>，状态 {{ compiled.status }}）。
              <a href="#" @click.prevent="navigate('/candidates')">前往审核队列</a>
            </p>

            <p class="micro section-label">动作序列 · {{ selected.action_sequence.length }} 步</p>
            <EmptyState
              v-if="!selected.action_sequence.length"
              icon="explorer"
              title="轨迹为空"
              hint="策略未产生任何动作，或执行在第一步前即失败。"
              compact
            />
            <ol v-else class="timeline">
              <li
                v-for="(a, i) in selected.action_sequence"
                :key="i"
                :class="a.rejected ? 'bad' : 'ok'"
              >
                <div class="act-head">
                  <span class="act-tool mono">{{ a.tool ?? '—' }}</span>
                  <span v-if="a.rejected" class="pill bad plain">REJECTED</span>
                </div>
                <div v-if="actionSummary(a)" class="act-arg mono">{{ actionSummary(a) }}</div>
                <div v-if="a.detail" class="act-detail small muted truncate" :title="String(a.detail)">
                  → {{ a.detail }}
                </div>
                <div v-if="a.reason" class="error-text">{{ a.reason }}</div>
              </li>
            </ol>
          </template>
        </StateBlock>
      </div>
    </div>

    <h2>探索历史</h2>
    <StateBlock
      :loading="listLoading"
      :error="listError"
      :empty="!list.length"
      empty-hint="还没有探索记录。发起一次任务后，这里会出现真实轨迹。"
      :retry="loadList"
    >
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Exploration</th>
              <th>意图</th>
              <th>策略</th>
              <th>执行状态</th>
              <th>停止原因</th>
              <th>目标验证</th>
              <th>测试裁决</th>
              <th class="num">步数</th>
              <th>开始时间</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in list"
              :key="row.exploration_id"
              class="clickable"
              @click="openTrace(row.exploration_id)"
            >
              <td class="mono">{{ row.exploration_id }}</td>
              <td class="truncate" :title="row.test_intent_ref">{{ row.test_intent_ref || '—' }}</td>
              <td><span class="tag">{{ row.policy_label || row.policy }}</span></td>
              <td><StatusPill :value="row.status" /></td>
              <td class="mono">{{ row.stop_reason || '—' }}</td>
              <td>
                <span
                  class="pill"
                  :class="{
                    ok: row.goal_status === 'VERIFIED',
                    bad: row.goal_status === 'NOT_VERIFIED',
                    warn: row.goal_status === 'INCONCLUSIVE',
                    neutral: !row.goal_status || row.goal_status === 'NOT_EVALUATED',
                  }"
                >
                  {{ row.goal_status || 'NOT_EVALUATED' }}
                </span>
              </td>
              <td>
                <StatusPill :value="row.test_case_verdict || 'INCONCLUSIVE'" />
              </td>
              <td class="num">{{ row.step_count }}</td>
              <td class="mono small">{{ row.started_at.replace('T', ' ').slice(0, 19) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </StateBlock>
  </section>
</template>

<style scoped>
.policy-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px 20px;
  padding: 10px 14px;
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  background: var(--tg-panel);
  margin-bottom: 16px;
}

.policy-main,
.policy-llm {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  min-width: 0;
}

.explorer-grid {
  display: grid;
  grid-template-columns: minmax(280px, 360px) 1fr;
  gap: 16px;
  align-items: start;
}

.run-card .field {
  margin-bottom: 12px;
}

.run-card .btn {
  margin-top: 4px;
}

.invariants {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--tg-border);
}

.inv-list {
  list-style: none;
  padding: 0;
  margin: 8px 0 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 12px;
  color: var(--tg-text-2);
}

.inv-list li {
  display: flex;
  align-items: center;
  gap: 7px;
}

.inv-list svg {
  color: var(--tg-success);
}

.detail-card {
  min-height: 320px;
}

.meta-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

/* A sha256 is a single 71-char token; break it rather than widen the page. */
.hash {
  word-break: break-all;
  overflow-wrap: anywhere;
  text-align: right;
}

.meta-row > .mono {
  font-size: 13px;
  font-weight: 600;
  color: var(--tg-text);
}

.section-label {
  margin: 18px 0 10px;
}

.act-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.act-tool {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--tg-text);
}

.act-arg {
  color: var(--tg-accent-text);
  font-size: 11.5px;
  margin-top: 1px;
  overflow-wrap: anywhere;
}

.act-detail {
  margin-top: 1px;
}

.run-err {
  margin: 10px 0 0;
  padding: 8px 10px;
  background: var(--tg-danger-bg);
  border: 1px solid var(--tg-danger-border);
  border-radius: var(--tg-radius-sm);
}

tr.clickable {
  cursor: pointer;
}

.compile-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: 12px 0 4px;
  padding: 10px 12px;
  background: var(--tg-panel-2);
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius-sm);
}

.goal-checks {
  list-style: none;
  margin: 10px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius-sm);
  background: var(--tg-panel-2);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.goal-checks li {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}

@media (max-width: 900px) {
  .explorer-grid {
    grid-template-columns: 1fr;
  }
}
</style>
