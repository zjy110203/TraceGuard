<script setup lang="ts">
// Candidate review queue, backed by the real candidate store.
//
// This page is the human gate in the pipeline: a trace compiled by the compiler
// arrives here as PENDING_REVIEW, and only an explicit approval (reviewer +
// approval record) turns it into a formal, runnable TestCaseVersion. Nothing on
// this page can promote a candidate without those two fields.
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

const canReview = computed(
  () => reviewer.value.trim().length > 0 && approvalRecord.value.trim().length > 0,
)

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
  try {
    detail.value = await api.candidate(c.candidate_id)
  } catch (err) {
    detailError.value = messageOf(err)
  }
}

function close(): void {
  openId.value = ''
  detail.value = null
  detailError.value = ''
}

async function approve(c: CandidateRow): Promise<void> {
  actionError.value = ''
  actionNote.value = ''
  if (!canReview.value) {
    actionError.value = '评审人与审批记录均为必填：候选晋升必须可追溯到具体责任人。'
    return
  }
  busy.value = c.candidate_id
  try {
    const updated = await api.approveCandidate(c.candidate_id, {
      reviewer: reviewer.value.trim(),
      approval_record: approvalRecord.value.trim(),
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
</script>

<template>
  <section class="page">
    <PageHeader
      title="Candidate 审核"
      subtitle="由探索轨迹编译出的机器候选用例。编译不会写入正式用例库；只有带评审人与审批记录的人工批准才会晋升为可执行资产。"
    >
      <label class="inline-label">
        <span class="sr-only">按 ID 或标题搜索</span>
        <input v-model="keyword" placeholder="搜索 candidate / case_id / 标题" />
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
        <span class="small muted">候选的 Oracle 在批准前保持未批准状态</span>
      </div>
      <div class="card-body">
        <div class="gate">
          <label class="inline-label">
            <span>评审人</span>
            <input v-model="reviewer" placeholder="例如 alice" />
          </label>
          <label class="inline-label">
            <span>审批记录</span>
            <input v-model="approvalRecord" placeholder="例如 REV-2026-0142" />
          </label>
          <label class="inline-label">
            <span>退回理由</span>
            <input v-model="rejectReason" placeholder="仅在退回时需要" />
          </label>
        </div>
        <p class="small muted gate-hint">
          批准 = 以评审人身份批准该候选的 UI Oracle，并将其写为正式用例文件。编译器
          <code class="mono">model_version</code> 为确定性规则（非 LLM）。
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
              : '在「探索器」完成一次探索后点击「编译为候选」，候选会进入这里等待评审。'
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
                <span class="pill" :class="statusTone(c)">{{ statusLabel(c) }}</span>
                <div v-if="c.reviewer" class="small muted">{{ c.reviewer }}</div>
              </td>
              <td class="mono small">{{ c.source_exploration_id }}</td>
              <td class="actions">
                <button class="btn ghost sm" type="button" @click="open(c)">查看</button>
                <button
                  v-if="c.status === 'PENDING_REVIEW'"
                  class="btn primary sm"
                  type="button"
                  :disabled="!canReview || busy === c.candidate_id"
                  :title="
                    canReview ? '以评审人身份批准并写入正式用例库' : '请先填写评审人与审批记录'
                  "
                  @click="approve(c)"
                >
                  批准
                </button>
                <button
                  v-if="c.status === 'PENDING_REVIEW'"
                  class="btn danger sm"
                  type="button"
                  :disabled="!reviewer.trim() || busy === c.candidate_id"
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
          </div>

          <h3>步骤</h3>
          <div class="table-wrap">
            <table>
              <thead><tr><th>#</th><th>动作</th><th>目标</th><th>值</th></tr></thead>
              <tbody>
                <tr v-for="s in detail.steps" :key="s.step_id">
                  <td class="mono">{{ s.step_id }}</td>
                  <td>{{ s.action }}</td>
                  <td class="mono">{{ s.target || '—' }}</td>
                  <td class="mono">{{ s.value || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <h3>Oracle 绑定</h3>
          <ul class="oracles">
            <li v-for="b in detail.oracle_bindings" :key="b.assertion_id">
              <AppIcon :name="b.primary.approved ? 'check' : 'alert'" :size="14" />
              <span class="mono">{{ b.assertion_id }}</span>
              <span class="mono small">{{ b.primary.source_ref }}</span>
              <span class="pill" :class="b.primary.approved ? 'ok' : 'warn'">
                {{ b.primary.approved ? `approved by ${b.primary.approved_by}` : '未批准' }}
              </span>
            </li>
          </ul>

          <h3>不确定项</h3>
          <ul v-if="detail.uncertainty_notes.length" class="notes-list">
            <li v-for="(n, i) in detail.uncertainty_notes" :key="i">{{ n }}</li>
          </ul>
          <p v-else class="small muted">编译器未标注不确定项。</p>
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
.notes-list {
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
