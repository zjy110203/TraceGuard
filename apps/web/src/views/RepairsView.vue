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

// Per-proposal form state: multiple pending proposals must never share inputs.
const forms = reactive<Record<string, { approver: string; record: string; reason: string }>>({})

function formOf(id: string) {
  if (!forms[id]) forms[id] = { approver: '', record: '', reason: '' }
  return forms[id]
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
    await api.rejectRepair(id, { reason: f.reason.trim(), reviewer: f.approver.trim() || 'human' })
    notice.value = `已驳回 ${id}`
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
        <p v-else-if="p.approved_by" class="small muted">
          批准人：{{ p.approved_by }} · {{ p.approval_record }}
        </p>
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
