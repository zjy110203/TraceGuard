<script setup lang="ts">
import { reactive, ref } from 'vue'
import { api } from '../api/client'
import { messageOf, useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import PageHeader from '../components/PageHeader.vue'
import type { RepairProposal } from '../api/types'

const state = useAsync(() => api.repairs())
const busyId = ref('')
const actionError = ref('')
const notice = ref('')
const createBusy = ref(false)
const createError = ref('')
const createNotice = ref('')
const sourceCase = ref<Record<string, unknown> | null>(null)
const createForm = reactive({
  caseId: '',
  sourceRunId: '',
  hypothesisId: '',
  evidenceRefs: '',
  changeKind: 'locator_update',
  candidateJson: '',
  diffJson: '[{"kind":"locator_update","target":"step-id","before":"old selector","after":"new selector"}]',
})

// Per-proposal form state: multiple pending proposals must never share inputs.
const forms = reactive<Record<string, { approver: string; record: string; reason: string; operator: string; promotionReason: string }>>({})

function formOf(id: string) {
  if (!forms[id]) forms[id] = { approver: '', record: '', reason: '', operator: '', promotionReason: '' }
  return forms[id]
}

async function loadFormalCase(): Promise<void> {
  createError.value = ''
  try {
    const result = await api.repairFormalCase(createForm.caseId.trim())
    sourceCase.value = result.case
    createForm.candidateJson = JSON.stringify(result.case, null, 2)
    createNotice.value = `已加载正式资产 ${result.source_file}；修改候选 JSON 后提交。`
  } catch (err) {
    createError.value = messageOf(err)
  }
}

async function createProposal(): Promise<void> {
  createError.value = ''
  createNotice.value = ''
  if (!sourceCase.value) {
    createError.value = '请先加载正式用例。'
    return
  }
  createBusy.value = true
  try {
    const candidate = JSON.parse(createForm.candidateJson) as Record<string, unknown>
    const diff = JSON.parse(createForm.diffJson) as unknown
    if (!Array.isArray(diff)) throw new Error('Diff 必须是 JSON 数组。')
    const result = await api.createRepair({
      case_id: createForm.caseId.trim(),
      candidate,
      change_kinds: [createForm.changeKind],
      diff,
      source_run_id: createForm.sourceRunId.trim(),
      hypothesis_id: createForm.hypothesisId.trim(),
      evidence_refs: createForm.evidenceRefs.split(',').map(x => x.trim()).filter(Boolean),
    })
    createNotice.value = `已创建提案 ${result.proposal_id}；下一步需验证并提交人工审核。`
    await state.reload()
  } catch (err) {
    createError.value = err instanceof SyntaxError ? '候选用例或 Diff JSON 格式无效。' : messageOf(err)
  } finally {
    createBusy.value = false
  }
}

const FORBIDDEN_APPROVERS = ['agent', 'ai', 'system']

function validateApprove(id: string): string {
  const f = formOf(id)
  const approver = f.approver.trim()
  if (!approver) return '批准人必填（具名人类）。'
  if (FORBIDDEN_APPROVERS.includes(approver.toLowerCase())) {
    return `批准人不能是 ${approver}：平台不能批准自己的修复。`
  }
  if (!f.record.trim()) return '批准记录 / 工单号必填。'
  return ''
}

async function approve(id: string): Promise<void> {
  const invalid = validateApprove(id)
  if (invalid) {
    actionError.value = `${id}：${invalid}`
    return
  }
  busyId.value = id
  actionError.value = ''
  notice.value = ''
  try {
    const f = formOf(id)
    await api.approveRepair(id, {
      approver: f.approver.trim(),
      approval_record: f.record.trim(),
    })
    notice.value = `已批准 ${id}（批准不等于晋升，晋升仍走独立门禁步骤）`
    await state.reload()
  } catch (err) {
    actionError.value = messageOf(err)
  } finally {
    busyId.value = ''
  }
}

async function reject(id: string): Promise<void> {
  const f = formOf(id)
  if (!f.reason.trim()) {
    actionError.value = `${id}：驳回必须填写理由。`
    return
  }
  busyId.value = id
  actionError.value = ''
  notice.value = ''
  try {
    await api.rejectRepair(id, { reason: f.reason.trim(), reviewer: f.approver.trim() })
    notice.value = `已驳回 ${id}`
    await state.reload()
  } catch (err) {
    actionError.value = messageOf(err)
  } finally {
    busyId.value = ''
  }
}

async function validateProposal(id: string): Promise<void> {
  await proposalAction(id, () => api.validateRepair(id), '校验完成')
}

async function submitProposal(id: string): Promise<void> {
  await proposalAction(id, () => api.submitRepair(id), '已提交人工审核')
}

async function replayProposal(id: string): Promise<void> {
  await proposalAction(id, () => api.replayRepair(id), '真实浏览器 Sandbox Replay 已完成')
}

async function promoteProposal(id: string): Promise<void> {
  const f = formOf(id)
  if (!f.operator.trim() || !f.promotionReason.trim()) {
    actionError.value = `${id}：晋升操作者和理由均必填。`
    return
  }
  await proposalAction(id, () => api.promoteRepair(id, {
    operator: f.operator.trim(), reason: f.promotionReason.trim(),
  }), '正式版本已追加晋升')
}

async function proposalAction(
  id: string,
  action: () => Promise<unknown>,
  success: string,
): Promise<void> {
  busyId.value = id
  actionError.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = `${id}：${success}`
    await state.reload()
  } catch (err) {
    actionError.value = messageOf(err)
  } finally {
    busyId.value = ''
  }
}
</script>

<template>
  <section class="page">
    <PageHeader
      title="修复审核"
      subtitle="Agent 修复提案的人工门禁：批准（APPROVED）≠ 晋升（PROMOTED）；正式 Oracle 受保护，不可被修复修改。"
    >
      <button class="btn" type="button" @click="state.reload()">刷新</button>
    </PageHeader>

    <p v-if="actionError" class="error-text">{{ actionError }}</p>
    <p v-if="notice" class="notice-text">{{ notice }}</p>

    <div class="card create-card">
      <h2>创建 Repair Proposal</h2>
      <p class="small muted">必须关联真实 Run 的受支持诊断假设和 supporting evidence。Agent 不能批准或晋升。</p>
      <div class="create-grid">
        <label class="field">正式用例 ID
          <input v-model="createForm.caseId" placeholder="TC-CREATE-001" />
        </label>
        <button class="btn" type="button" :disabled="!createForm.caseId || createBusy" @click="loadFormalCase">加载正式资产</button>
        <label class="field">来源 Run ID<input v-model="createForm.sourceRunId" placeholder="run_..." /></label>
        <label class="field">诊断假设 ID<input v-model="createForm.hypothesisId" placeholder="hyp_..." /></label>
        <label class="field">Supporting Evidence Ref（逗号分隔）<input v-model="createForm.evidenceRefs" placeholder="ev_..." /></label>
        <label class="field">变更类型
          <select v-model="createForm.changeKind">
            <option value="locator_update">locator_update</option>
            <option value="wait_condition">wait_condition</option>
            <option value="test_step_fix">test_step_fix</option>
            <option value="data_ref_fix">data_ref_fix</option>
            <option value="automation_config">automation_config</option>
          </select>
        </label>
      </div>
      <label class="field">候选 TestCase JSON<textarea v-model="createForm.candidateJson" rows="10" spellcheck="false" /></label>
      <label class="field">变更 Diff JSON<textarea v-model="createForm.diffJson" rows="3" spellcheck="false" /></label>
      <p v-if="createError" class="error-text">{{ createError }}</p>
      <p v-if="createNotice" class="notice-text">{{ createNotice }}</p>
      <button class="btn primary" type="button" :disabled="createBusy || !sourceCase" @click="createProposal">创建提案</button>
    </div>

    <StateBlock
      :loading="state.loading.value"
      :error="state.error.value"
      :empty="!state.data.value?.length"
      empty-hint="尚无修复提案。诊断确认测试资产问题后，Agent 可生成候选修复提案。"
      :retry="state.reload"
    >
      <div v-for="p in (state.data.value ?? []) as RepairProposal[]" :key="p.proposal_id" class="card">
        <div class="row">
          <span class="mono">{{ p.proposal_id }}</span>
          <StatusPill :value="p.state" />
          <span class="small muted">
            {{ p.case_id }} · v{{ p.base_testcase_version }} → v{{ p.candidate_testcase_version }}
            <template v-if="p.promotion_target_version">
              · 晋升目标 v{{ p.promotion_target_version }}
            </template>
          </span>
        </div>
        <div class="row small">
          <span class="muted">变更类型</span>
          <span class="mono">{{ p.change_kinds.join(', ') }}</span>
        </div>
        <p v-if="p.source_run_id" class="small muted">
          诊断来源：{{ p.source_run_id }} · {{ p.source_hypothesis_id }} · Evidence {{ p.source_evidence_refs.join(', ') }}
        </p>

        <div v-if="p.diff.length" class="diff">
          <h3 class="small muted">变更 Diff（before → after）</h3>
          <div v-for="(d, i) in p.diff" :key="i" class="diff-entry">
            <p class="mono small">
              <span class="badge-kind">{{ d.kind }}</span>
              <span class="mono">{{ d.target }}</span>
            </p>
            <div class="diff-cols">
              <pre class="before">- {{ d.before }}</pre>
              <pre class="after">+ {{ d.after }}</pre>
            </div>
          </div>
        </div>

        <ul v-if="p.validation_notes.length" class="plain small muted">
          <li v-for="(n, i) in p.validation_notes" :key="i">{{ n }}</li>
        </ul>
        <p v-if="p.failure_reason" class="error-text small">{{ p.failure_reason }}</p>
        <p v-if="p.sandbox_replay" class="small" :class="p.sandbox_replay.status === 'PASSED' ? 'notice-text' : 'error-text'">
          Sandbox Replay：{{ p.sandbox_replay.status }} · {{ p.sandbox_replay.verdict }} · evidence {{ p.sandbox_replay.evidence_sha256 }}
        </p>

        <div v-if="p.state === 'DRAFT'" class="actions">
          <button class="btn primary" type="button" :disabled="busyId === p.proposal_id" @click="validateProposal(p.proposal_id)">验证提案</button>
        </div>
        <div v-else-if="p.state === 'VALIDATED'" class="actions">
          <button class="btn primary" type="button" :disabled="busyId === p.proposal_id" @click="submitProposal(p.proposal_id)">提交人工审核</button>
        </div>

        <div v-if="p.state === 'PENDING_APPROVAL'" class="approval">
          <div class="field">
            <label :for="`approver-${p.proposal_id}`">批准人（具名人类）</label>
            <input
              :id="`approver-${p.proposal_id}`"
              v-model="formOf(p.proposal_id).approver"
              placeholder="如 zhang.san"
            />
          </div>
          <div class="field">
            <label :for="`record-${p.proposal_id}`">批准记录 / 工单号</label>
            <input :id="`record-${p.proposal_id}`" v-model="formOf(p.proposal_id).record" placeholder="工单号或记录链接" />
          </div>
          <div class="field">
            <label :for="`reason-${p.proposal_id}`">驳回理由（驳回时必填）</label>
            <input :id="`reason-${p.proposal_id}`" v-model="formOf(p.proposal_id).reason" placeholder="驳回时填写" />
          </div>
          <div class="actions">
            <button
              class="btn primary"
              type="button"
              :disabled="busyId === p.proposal_id"
              @click="approve(p.proposal_id)"
            >
              批准
            </button>
            <button
              class="btn danger"
              type="button"
              :disabled="busyId === p.proposal_id || !formOf(p.proposal_id).reason"
              @click="reject(p.proposal_id)"
            >
              驳回
            </button>
          </div>
        </div>
        <div v-if="p.state === 'APPROVED'" class="approval">
          <p class="small muted">提案已由 {{ p.approved_by }} 批准；先执行真实 Sandbox Replay，再由具名人员晋升。</p>
          <div class="field"><label :for="`operator-${p.proposal_id}`">晋升操作者</label>
            <input :id="`operator-${p.proposal_id}`" v-model="formOf(p.proposal_id).operator" placeholder="具名人员" />
          </div>
          <div class="field"><label :for="`promotion-reason-${p.proposal_id}`">晋升理由</label>
            <input :id="`promotion-reason-${p.proposal_id}`" v-model="formOf(p.proposal_id).promotionReason" placeholder="审核记录或变更理由" />
          </div>
          <div class="actions">
            <button class="btn" type="button" :disabled="busyId === p.proposal_id" @click="replayProposal(p.proposal_id)">Sandbox Replay</button>
            <button class="btn primary" type="button" :disabled="busyId === p.proposal_id || p.sandbox_replay?.status !== 'PASSED'" @click="promoteProposal(p.proposal_id)">人工晋升</button>
          </div>
        </div>
        <p v-else-if="p.approved_by" class="small muted">
          批准人：{{ p.approved_by }} · {{ p.approval_record }}
        </p>
        <p v-if="p.rejected_by" class="small muted">拒绝操作者：{{ p.rejected_by }}</p>
      </div>
    </StateBlock>
  </section>
</template>

<style scoped>
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.create-card { margin-bottom: 16px; }
.create-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 8px; align-items: end; margin-bottom: 10px; }
.field textarea { width: 100%; font: 12px ui-monospace, monospace; }
.diff {
  margin: 10px 0;
}
.diff-entry {
  margin-bottom: 10px;
}
.badge-kind {
  background: var(--tg-panel-2);
  border: 1px solid var(--tg-border);
  border-radius: 4px;
  padding: 0 6px;
  margin-right: 8px;
}
.diff-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.diff-cols pre {
  margin: 0;
  font-size: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.before {
  background: var(--tg-panel-2);
  color: var(--tg-danger);
}
.after {
  background: var(--tg-panel-2);
  color: var(--tg-success);
}
.approval {
  border-top: 1px dashed var(--tg-border);
  padding-top: 10px;
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px;
  align-items: end;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field label {
  font-size: 12px;
  color: var(--tg-muted);
}
.field input {
  width: 100%;
}
.actions {
  display: flex;
  gap: 8px;
}
</style>
