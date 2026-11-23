<script setup lang="ts">
// Candidate review queue, backed by the real candidate store.
//
// This page is the human gate in the pipeline: a trace compiled by the compiler
// arrives here as PENDING_REVIEW, and only an explicit approval turns it into a
// formal, runnable TestCaseVersion.
//
// R5-4.6: the server REQUIRES every required review-checklist item to be
// confirmed and the uncertainty notes acknowledged — the UI mirrors that gate,
// but the enforcement is server-side (a crafted request without confirmations
// is a 422 no matter what this page rendered).
import { computed, ref } from 'vue'
import { api } from '../api/client'
import { messageOf, useAsync } from '../composables/useAsync'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'
import StatCard from '../components/StatCard.vue'
import AppIcon from '../components/AppIcon.vue'
import { navigate } from '../router'
import type { CandidateDetail, CandidateRow } from '../api/types'

const state = useAsync(() => api.candidates())
const keyword = ref('')
const statusFilter = ref<'ALL' | 'PENDING_REVIEW' | 'APPROVED' | 'REJECTED'>('ALL')

const reviewer = ref('')
const approvalRecord = ref('')
const rejectReason = ref('')
const openId = ref('')
const detail = ref<CandidateDetail | null>(null)
const detailError = ref('')
const busy = ref('')
const actionError = ref('')
const actionNote = ref('')

// per-candidate review confirmation state
const confirmed = ref<Set<string>>(new Set())
const uncertaintyAcknowledged = ref(false)

const rows = computed(() => state.data.value ?? [])
const counts = computed(() => ({
  pending: rows.value.filter((c) => c.status === 'PENDING_REVIEW').length,
  approved: rows.value.filter((c) => c.status === 'APPROVED').length,
  rejected: rows.value.filter((c) => c.status === 'REJECTED').length,
}))

const visible = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return rows.value.filter(
    (c) =>
      (statusFilter.value === 'ALL' || c.status === statusFilter.value) &&
      (!kw ||
        c.case_id.toLowerCase().includes(kw) ||
        c.title.toLowerCase().includes(kw) ||
        c.candidate_id.toLowerCase().includes(kw)),
  )
})

const requiredItems = computed(() =>
  (detail.value?.review_checklist ?? []).filter((c) => c.required),
)
const allRequiredConfirmed = computed(
  () => requiredItems.value.length > 0 && requiredItems.value.every((c) => confirmed.value.has(c.id)),
)
const hasUncertainties = computed(
  () => (detail.value?.uncertainty_notes ?? []).some((n) => n.trim()),
)
const canReview = computed(
  () => reviewer.value.trim().length > 0 && approvalRecord.value.trim().length > 0,
)
const canApprove = computed(
  () =>
    canReview.value &&
    allRequiredConfirmed.value &&
    (!hasUncertainties.value || uncertaintyAcknowledged.value),
)

function toggleItem(id: string, checked: boolean): void {
  const next = new Set(confirmed.value)
  if (checked) next.add(id)
  else next.delete(id)
  confirmed.value = next
}

function statusLabel(c: CandidateRow): string {
  if (c.status === 'APPROVED') return 'APPROVED'
  if (c.status === 'REJECTED') return 'REJECTED'
  return 'PENDING_REVIEW'
}

function statusTone(c: CandidateRow): 'ok' | 'warn' | 'danger' {
  if (c.status === 'APPROVED') return 'ok'
  if (c.status === 'REJECTED') return 'danger'
  return 'warn'
}

async function open(c: CandidateRow): Promise<void> {
  actionError.value = ''
  actionNote.value = ''
  detailError.value = ''
  openId.value = c.candidate_id
  detail.value = null
  confirmed.value = new Set()
  uncertaintyAcknowledged.value = false
  try {
    detail.value = await api.candidate(c.candidate_id)
    // start from nothing: every required item must be checked deliberately
    for (const item of requiredItems.value) confirmed.value.delete(item.id)
  } catch (err) {
    detailError.value = messageOf(err)
  }
}

function close(): void {
  openId.value = ''
  detail.value = null
  detailError.value = ''
  confirmed.value = new Set()
  uncertaintyAcknowledged.value = false
}

async function approve(c: CandidateRow): Promise<void> {
  actionError.value = ''
  actionNote.value = ''
  if (!canApprove.value) {
    actionError.value = '批准前必须：填写评审人与审批记录、逐项确认全部必需审核项、并确认所有不确定项。'
    return
  }
  busy.value = c.candidate_id
  try {
    const updated = await api.approveCandidate(c.candidate_id, {
      reviewer: reviewer.value.trim(),
      approval_record: approvalRecord.value.trim(),
      review_confirmed: [...confirmed.value],
      uncertainty_acknowledged: uncertaintyAcknowledged.value || !hasUncertainties.value,
    })
    actionNote.value = `${updated.case_id} 已晋升为正式用例（文件 ${updated.case_file}）。可在「测试用例」页打开并执行全链。`
    close()
    await state.reload()
  } catch (err) {
    actionError.value = messageOf(err)
  } finally {
    busy.value = ''
  }
}

async function reject(c: CandidateRow): Promise<void> {
  actionError.value = ''
  actionNote.value = ''
  if (!reviewer.value.trim()) {
    actionError.value = '退回候选需要填写评审人。'
    return
  }
  if (!rejectReason.value.trim()) {
    actionError.value = '退回必须填写理由：该轨迹将被永久关闭，决定必须可追溯。'
    return
  }
  busy.value = c.candidate_id
  try {
    await api.rejectCandidate(c.candidate_id, {
      reviewer: reviewer.value.trim(),
      reason: rejectReason.value.trim(),
    })
    actionNote.value = `${c.case_id} 已退回；该轨迹不可再次编译，需要重新探索。`
    close()
    await state.reload()
  } catch (err) {
    actionError.value = messageOf(err)
  } finally {
    busy.value = ''
  }
}

function goalTone(status?: string): string {
  switch (status) {
    case 'VERIFIED':
      return 'ok'
    case 'NOT_VERIFIED':
      return 'bad'
    case 'INCONCLUSIVE':
      return 'warn'
    default:
      return 'neutral'
  }
}

function oracleValue(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  return String(v)
}
</script>

<template>
  <section class="page">
    <PageHeader
      title="Candidate 审核"
      subtitle="由探索轨迹编译出的机器候选用例。编译不会写入正式用例库；只有带评审人与审批记录、逐项完成审核清单并确认不确定项的人工批准才会晋升为可执行资产。"
    >
      <label class="inline-label">
        <span class="sr-only">按 ID 或标题搜索</span>
        <input v-model="keyword" placeholder="搜索候选 / 用例 / 标题" />
      </label>
      <button class="btn" type="button" @click="state.reload()">刷新</button>
    </PageHeader>

    <div class="kpis">
      <StatCard label="待审核" :value="counts.pending" icon="clock" hint="等待人工评审" />
      <StatCard label="已批准" :value="counts.approved" icon="check" tone="success" hint="已晋升为正式用例" />
      <StatCard label="已退回" :value="counts.rejected" icon="x" tone="danger" hint="轨迹需重新探索" />
    </div>

    <div class="card">
      <div class="card-head">
        <h2>评审依据</h2>
        <span class="small muted">候选的 Oracle 在批准前保持未批准状态；reviewer 为本地审计字段，非身份认证</span>
      </div>
      <div class="card-body">
        <div class="gate">
          <label class="inline-label">
            <span>评审人</span>
            <input v-model="reviewer" data-testid="reviewer" placeholder="例如 alice" />
          </label>
          <label class="inline-label">
            <span>审批记录</span>
            <input v-model="approvalRecord" data-testid="approval-record" placeholder="例如 REV-2026-0142" />
          </label>
          <label class="inline-label">
            <span>退回理由</span>
            <input v-model="rejectReason" data-testid="reject-reason" placeholder="退回时必填" />
          </label>
        </div>
        <p class="small muted gate-hint">
          批准 = 以评审人身份批准该候选的 UI Oracle，并将其写为正式用例文件。编译器
          <code class="mono">model_version</code> 为确定性规则（非 LLM）。服务端会强制校验审核清单确认与不确定项确认。
        </p>
        <p v-if="actionError" class="alert error" role="alert">{{ actionError }}</p>
        <p v-if="actionNote" class="alert ok" role="status">{{ actionNote }}</p>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>候选队列</h2>
        <div class="tabs">
          <button
            v-for="f in (['ALL', 'PENDING_REVIEW', 'APPROVED', 'REJECTED'] as const)"
            :key="f"
            type="button"
            class="tab"
            :class="{ active: statusFilter === f }"
            @click="statusFilter = f"
          >
            {{ { ALL: '全部', PENDING_REVIEW: '待审核', APPROVED: '已批准', REJECTED: '已退回' }[f] }}
          </button>
        </div>
      </div>

      <div v-if="state.loading.value" class="card-body">
        <div class="skeleton" style="height: 68px" />
        <div class="skeleton" style="height: 68px" />
      </div>
      <div v-else-if="state.error.value" class="card-body">
        <p class="alert error" role="alert">{{ state.error.value }}</p>
        <button class="btn" type="button" @click="state.reload()">重试</button>
      </div>
      <div v-else-if="!visible.length" class="card-body">
        <EmptyState
          icon="layers"
          :title="keyword || statusFilter !== 'ALL' ? '没有匹配的候选' : '暂无候选用例'"
          :hint="
            keyword || statusFilter !== 'ALL'
              ? '调整筛选条件或清空搜索。'
              : '在「探索器」完成一次目标已验证（VERIFIED）的探索后点击「编译为候选」，候选会进入这里等待评审。'
          "
        >
          <button class="btn" type="button" @click="navigate('/explorer')">前往探索器</button>
        </EmptyState>
      </div>
      <div v-else class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>candidate_id</th>
              <th>case_id</th>
              <th>标题</th>
              <th>步骤</th>
              <th>断言</th>
              <th>目标验证</th>
              <th>状态</th>
              <th>来源轨迹</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in visible" :key="c.candidate_id">
              <td class="mono">{{ c.candidate_id }}</td>
              <td class="mono">{{ c.case_id }}</td>
              <td>
                {{ c.title }}
                <div v-if="c.uncertainty_notes.length" class="small muted notes">
                  {{ c.uncertainty_notes.length }} 条不确定项标注
                </div>
              </td>
              <td>{{ c.step_count }}</td>
              <td>{{ c.assertion_count }}</td>
              <td>
                <span class="pill" :class="goalTone(c.goal_status)">
                  {{ c.goal_status || '—' }}
                </span>
              </td>
              <td>
                <span class="pill" :class="statusTone(c)">{{ statusLabel(c) }}</span>
                <div v-if="c.reviewer" class="small muted">{{ c.reviewer }}</div>
              </td>
              <td class="mono small">{{ c.source_exploration_id }}</td>
              <td class="actions">
                <button class="btn ghost sm" type="button" data-testid="open-candidate" @click="open(c)">查看</button>
                <button
                  v-if="c.status === 'PENDING_REVIEW'"
                  class="btn primary sm"
                  type="button"
                  :disabled="!canApprove || busy === c.candidate_id"
                  :title="
                    canApprove
                      ? '以评审人身份批准并写入正式用例库'
                      : '需填写评审人与审批记录，并完成全部审核项确认'
                  "
                  @click="approve(c)"
                >
                  批准
                </button>
                <button
                  v-if="c.status === 'PENDING_REVIEW'"
                  class="btn danger sm"
                  type="button"
                  :disabled="!reviewer.trim() || !rejectReason.trim() || busy === c.candidate_id"
                  @click="reject(c)"
                >
                  退回
                </button>
                <button
                  v-if="c.status === 'APPROVED'"
                  class="btn ghost sm"
                  type="button"
                  @click="navigate(`/cases/${c.case_id}`)"
                >
                  正式用例
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="openId" class="card">
      <div class="card-head">
        <h2>候选详情 · {{ openId }}</h2>
        <button class="btn ghost sm" type="button" @click="close">收起</button>
      </div>
      <div class="card-body">
        <p v-if="detailError" class="alert error" role="alert">{{ detailError }}</p>
        <div v-else-if="!detail" class="skeleton" style="height: 120px" />
        <template v-else>
          <div class="kv-grid">
            <div class="kv"><span>case_id</span><b class="mono">{{ detail.case_id }}</b></div>
            <div class="kv"><span>编译器</span><b class="mono">{{ detail.compiler_version }}</b></div>
            <div class="kv"><span>定位策略</span><b class="mono">{{ detail.locator_strategy_version }}</b></div>
            <div class="kv"><span>模型</span><b class="mono">{{ detail.model_version }}</b></div>
            <div class="kv"><span>来源轨迹</span><b class="mono">{{ detail.source_exploration_id }}</b></div>
            <div class="kv"><span>轨迹哈希</span><b class="mono hash">{{ detail.source_trace_hash }}</b></div>
            <div class="kv">
              <span>目标验证</span>
              <b><span class="pill" :class="goalTone(detail.goal_status)">{{ detail.goal_status || '—' }}</span></b>
            </div>
          </div>

          <h3>原始 TestIntent</h3>
          <ul class="kv" data-testid="candidate-intent">
            <li><span>goal</span><span>{{ detail.intent.goal || detail.intent.description || '—' }}</span></li>
            <li>
              <span>expected_outcomes</span>
              <span>{{ detail.intent.expected_outcomes?.length ? detail.intent.expected_outcomes.join('；') : '—' }}</span>
            </li>
            <li><span>target</span><span class="mono">{{ detail.intent.target || detail.target || '—' }}</span></li>
            <li>
              <span>required_assertions</span>
              <span class="mono">{{ detail.intent.required_assertions?.length ? detail.intent.required_assertions.join('；') : '—' }}</span>
            </li>
            <li>
              <span>constraints</span>
              <span>{{ detail.intent.constraints?.length ? detail.intent.constraints.join('；') : '—' }}</span>
            </li>
          </ul>

          <h3>目标验证依据</h3>
          <ul v-if="detail.goal_checks?.length" class="goal-list" data-testid="candidate-goal-checks">
            <li v-for="(c, i) in detail.goal_checks" :key="i">
              <AppIcon :name="c.deferred ? 'clock' : c.ok ? 'check' : 'x'" :size="13" />
              <span class="mono small">{{ c.name }}</span>
              <span class="small">预期 <b class="mono">{{ c.expected || '（未声明）' }}</b></span>
              <span class="small muted">观察 {{ c.observed ? c.observed.slice(0, 80) : '—' }}</span>
              <span v-if="c.deferred" class="pill plain neutral">正式运行时由独立 Oracle 验证</span>
            </li>
          </ul>
          <p v-else class="small muted">编译时未记录目标验证检查项。</p>

          <h3>正式执行步骤</h3>
          <div class="table-wrap">
            <table>
              <thead><tr><th>#</th><th>动作</th><th>目标</th><th>值</th><th>value_ref</th></tr></thead>
              <tbody>
                <tr v-for="s in detail.steps" :key="s.step_id" data-testid="candidate-step">
                  <td class="mono">{{ s.step_id }}</td>
                  <td>{{ s.action }}</td>
                  <td class="mono">{{ s.target || '—' }}</td>
                  <td class="mono">{{ s.value || '—' }}</td>
                  <td class="mono">{{ s.value_ref || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <h3>TestDataRef 对应关系</h3>
          <div v-if="detail.data_refs?.length" class="table-wrap">
            <table>
              <thead><tr><th>ref_id</th><th>locator</th><th>value</th></tr></thead>
              <tbody>
                <tr v-for="d in detail.data_refs" :key="d.ref_id">
                  <td class="mono">{{ d.ref_id }}</td>
                  <td class="mono">{{ d.locator }}</td>
                  <td class="mono">{{ d.value || '（部署级 TestDataProvider 运行时解析）' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="small muted">该候选不引用敏感测试数据。</p>

          <h3>探索观察结果（最后 4 条）</h3>
          <ul v-if="detail.observations?.length" class="obs-list">
            <li v-for="(o, i) in detail.observations" :key="i">
              <span class="mono small">{{ (o as Record<string, unknown>).url_path || '—' }}</span>
              <span class="small muted">{{ String((o as Record<string, unknown>).state_summary || '') }}</span>
            </li>
          </ul>
          <p v-else class="small muted">无观察记录。</p>

          <h3>断言与 Oracle</h3>
          <ul class="oracles" data-testid="candidate-oracles">
            <li v-for="b in detail.oracle_bindings" :key="b.assertion_id">
              <AppIcon :name="b.primary.approved ? 'check' : 'alert'" :size="14" />
              <span class="mono">{{ b.assertion_id }}</span>
              <span class="mono small">{{ b.primary.source_ref }}</span>
              <span class="small">期望值 <b class="mono">{{ oracleValue(b.primary.expected_value) }}</b></span>
              <span class="pill" :class="b.primary.approved ? 'ok' : 'warn'">
                {{ b.primary.approved ? `approved by ${b.primary.approved_by}` : '未批准' }}
              </span>
              <span class="small muted basis">{{ b.primary.source }}</span>
            </li>
          </ul>

          <h3>不确定项</h3>
          <ul v-if="detail.uncertainty_notes.length" class="notes-list">
            <li v-for="(n, i) in detail.uncertainty_notes" :key="i">{{ n }}</li>
          </ul>
          <p v-else class="small muted">编译器未标注不确定项。</p>

          <h3>审核清单（逐项确认）</h3>
          <div class="checklist" data-testid="review-checklist">
            <label
              v-for="item in detail.review_checklist"
              :key="item.id"
              class="check-item"
            >
              <input
                type="checkbox"
                :checked="confirmed.has(item.id)"
                :data-testid="`checklist-${item.id}`"
                @change="toggleItem(item.id, ($event.target as HTMLInputElement).checked)"
              />
              <span>{{ item.text }}</span>
              <span v-if="item.required" class="pill plain warn">必选</span>
            </label>
          </div>

          <label v-if="hasUncertainties" class="check-item uncertainty" data-testid="uncertainty-ack">
            <input type="checkbox" v-model="uncertaintyAcknowledged" />
            <span>
              我已阅读以上 {{ detail.uncertainty_notes.length }} 条不确定项，并确认其不影响本次批准决定。
            </span>
            <span class="pill plain warn">必选</span>
          </label>

          <div class="approve-bar">
            <button
              v-if="detail.status === 'PENDING_REVIEW'"
              class="btn primary"
              type="button"
              data-testid="approve-candidate"
              :disabled="!canApprove || busy === detail.candidate_id"
              @click="approve(detail)"
            >
              批准并晋升为正式用例
            </button>
            <button
              v-if="detail.status === 'PENDING_REVIEW'"
              class="btn danger"
              type="button"
              data-testid="reject-candidate"
              :disabled="!reviewer.trim() || !rejectReason.trim() || busy === detail.candidate_id"
              @click="reject(detail)"
            >
              退回
            </button>
            <span v-if="detail.status !== 'PENDING_REVIEW'" class="small muted">
              该候选已 {{ statusLabel(detail) }}，不可再次审核。
            </span>
            <span v-else-if="!canApprove" class="small muted">
              尚未满足批准条件（{{ confirmed.size }}/{{ requiredItems.length }} 项已确认{{ hasUncertainties && !uncertaintyAcknowledged ? '，且未确认不确定项' : '' }}）。
            </span>
          </div>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.kpis {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.inline-label {
  display: flex;
  align-items: center;
  gap: 8px;
}
.inline-label input {
  width: 200px;
}
.inline-label span {
  font-size: 12px;
  color: var(--tg-muted);
  white-space: nowrap;
}
.gate {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
}
.gate-hint {
  margin: 10px 0 0;
}
.notes {
  margin-top: 2px;
}
.actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px 18px;
  margin-bottom: 18px;
}
.hash {
  word-break: break-all;
}
.oracles,
.notes-list,
.goal-list,
.obs-list {
  list-style: none;
  margin: 0 0 4px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.oracles li {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13px;
}
.oracles .basis {
  width: 100%;
  padding-left: 22px;
}
.goal-list li,
.obs-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13px;
  padding: 6px 10px;
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius-sm);
  background: var(--tg-panel-2);
}
.notes-list li {
  font-size: 13px;
  color: var(--tg-muted);
  padding-left: 12px;
  position: relative;
}
.notes-list li::before {
  content: '·';
  position: absolute;
  left: 2px;
}
.checklist {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.check-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  cursor: pointer;
}
.check-item input {
  margin-top: 2px;
}
.check-item.uncertainty {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius-sm);
  background: var(--tg-panel-2);
}
.approve-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--tg-border);
}
@media (max-width: 900px) {
  .kpis {
    grid-template-columns: minmax(0, 1fr);
  }
  .inline-label input {
    width: 100%;
  }
  .gate {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
