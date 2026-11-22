<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'
import type { CaseRow } from '../api/types'

const state = useAsync(() => api.cases())
const keyword = ref('')

// Candidate = a case compiled by the machine (Explorer + Compiler), as opposed
// to a human-authored one. Approval state comes straight from the case record.
const candidates = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return (state.data.value ?? []).filter(
    (c: CaseRow) =>
      (c.compiled_from ?? '') !== 'manual' &&
      (!kw || c.case_id.toLowerCase().includes(kw) || c.title.toLowerCase().includes(kw)),
  )
})

function approvedLabel(c: CaseRow): string {
  if (c.approved_by === 'human') return 'APPROVED'
  if (c.approved_by) return `APPROVED (${c.approved_by})`
  return 'PENDING_REVIEW'
}
</script>

<template>
  <section class="page">
    <PageHeader
      title="Candidate TestCase 审核"
      subtitle="由探索 + 编译产生的机器候选用例。人工用例不在此列；候选晋升为正式资产需通过审核门禁。"
    >
      <label class="inline-label">
        <span class="sr-only">按 ID 或标题搜索</span>
        <input v-model="keyword" placeholder="搜索 case_id / 标题" />
      </label>
      <button class="btn" type="button" @click="state.reload()">刷新</button>
    </PageHeader>

    <StateBlock
      :loading="state.loading.value"
      :error="state.error.value"
      :empty="!candidates.length"
      :empty-hint="
        keyword
          ? '没有匹配的候选用例。'
          : '尚无机器候选用例。Explorer 探索 + Compiler 编译后，候选会出现在这里。'
      "
      :retry="state.reload"
    >
      <table>
        <thead>
          <tr><th>case_id</th><th>标题</th><th>来源</th><th>步骤</th><th>断言</th><th>审核状态</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="c in candidates" :key="c.case_id">
            <td class="mono">{{ c.case_id }}</td>
            <td>{{ c.title }}</td>
            <td class="mono small">{{ c.compiled_from }}</td>
            <td>{{ c.steps }}</td>
            <td>{{ c.assertions }}</td>
            <td><span class="pill-like small" :class="c.approved_by === 'human' ? 'ok' : 'warn'">{{ approvedLabel(c) }}</span></td>
            <td>
              <a href="#" @click.prevent="navigate(`/cases/${c.case_id}`)">详情</a>
            </td>
          </tr>
        </tbody>
      </table>
    </StateBlock>
  </section>
</template>

<style scoped>
.inline-label input {
  width: 220px;
}
.pill-like {
  display: inline-block;
  font-size: 11px;
  line-height: 18px;
  padding: 0 7px;
  border-radius: 9px;
  border: 1px solid transparent;
  white-space: nowrap;
}
.pill-like.ok {
  color: var(--tg-success);
  border-color: var(--tg-success);
}
.pill-like.warn {
  color: var(--tg-warn);
  border-color: var(--tg-warn);
}
</style>
