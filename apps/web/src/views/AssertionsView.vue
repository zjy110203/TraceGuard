<script setup lang="ts">
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'

const props = defineProps<{ runId: string }>()
const state = useAsync(() => api.report(props.runId))
</script>

<template>
  <section class="page">
    <PageHeader title="断言结果" :subtitle="props.runId">
      <button class="btn" type="button" @click="navigate(`/runs/${props.runId}`)">返回 Run</button>
    </PageHeader>

    <StateBlock
      :loading="state.loading.value"
      :error="state.error.value"
      :empty="!state.data.value?.assertion_results.length"
      empty-hint="该 Run 的报告尚未包含断言结果。运行结束后点击刷新重试。"
      :retry="state.reload"
    >
      <p class="small muted">
        总体 Verdict：<StatusPill :value="state.data.value?.test_verdict || 'NOT_EVALUATED'" />
      </p>
      <table>
        <thead>
          <tr>
            <th>assertion_id</th><th>必需</th><th>结果</th><th>Oracle 可用</th><th>冲突</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in state.data.value?.assertion_results ?? []" :key="a.assertion_id">
            <td class="mono">{{ a.assertion_id }}</td>
            <td>{{ a.required ? '是' : '否' }}</td>
            <td><StatusPill :value="a.verdict" /></td>
            <td>{{ a.oracle_available ? '是' : '否' }}</td>
            <td>
              <span v-if="a.oracle_conflict" class="conflict">多源冲突</span>
              <span v-else class="muted">—</span>
            </td>
            <td class="small">{{ a.reason }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="state.data.value?.verdict_notes.length" class="notes">
        <h2>判定说明</h2>
        <ul class="plain small muted">
          <li v-for="(n, i) in state.data.value?.verdict_notes ?? []" :key="i">{{ n }}</li>
        </ul>
      </div>
    </StateBlock>
  </section>
</template>

<style scoped>
.conflict {
  font-size: 12px;
  color: var(--tg-warn);
}
.notes {
  margin-top: 20px;
}
</style>
