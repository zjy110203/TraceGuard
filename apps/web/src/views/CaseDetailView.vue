<script setup lang="ts">
import { computed } from 'vue'
import { api } from '../api/client'
import type { OracleBinding } from '../api/types'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'

const props = defineProps<{ caseId: string }>()
const state = useAsync(() => api.caseDetail(props.caseId))

const bindingByAssertion = computed(() => {
  const map = new Map<string, OracleBinding>()
  for (const b of state.data.value?.oracle_bindings ?? []) {
    map.set(b.assertion_id, b)
  }
  return map
})
</script>

<template>
  <section class="page">
    <PageHeader
      :title="props.caseId"
      :subtitle="state.data.value ? `${state.data.value.title} · v${state.data.value.version}` : undefined"
    >
      <button class="btn" type="button" @click="navigate(`/cases/${props.caseId}/versions`)">版本历史</button>
      <button class="btn" type="button" @click="state.reload()">刷新</button>
    </PageHeader>

    <StateBlock :loading="state.loading.value" :error="state.error.value" :retry="state.reload">
      <div class="meta">
        <div><span class="k">意图</span><span class="v">{{ state.data.value?.intent.description }}</span></div>
        <div><span class="k">intent_id</span><span class="v mono">{{ state.data.value?.intent.intent_id }}</span></div>
        <div><span class="k">编译来源</span><span class="v">{{ state.data.value?.compiled_from }}</span></div>
        <div><span class="k">批准人</span><span class="v">{{ state.data.value?.approved_by }}</span></div>
        <div><span class="k">清理策略</span><span class="v">{{ state.data.value?.cleanup.strategy }}</span></div>
        <div><span class="k">源文件</span><span class="v mono">{{ state.data.value?.source_file }}</span></div>
      </div>

      <h2>步骤 ({{ state.data.value?.steps.length ?? 0 }})</h2>
      <table>
        <thead><tr><th>step_id</th><th>action</th><th>target</th><th>value</th><th>expected</th></tr></thead>
        <tbody>
          <tr v-for="s in state.data.value?.steps ?? []" :key="s.step_id">
            <td class="mono">{{ s.step_id }}</td>
            <td>{{ s.action }}</td>
            <td class="mono">{{ s.target }}</td>
            <td class="mono">{{ s.value || '—' }}</td>
            <td class="mono">{{ s.expected || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h2>断言与 Oracle 绑定 ({{ state.data.value?.assertions.length ?? 0 }})</h2>
      <table>
        <thead><tr><th>assertion</th><th>要求</th><th>主判据</th><th>期望</th><th>佐证数</th><th>裁决规则</th></tr></thead>
        <tbody>
          <tr v-for="a in state.data.value?.assertions ?? []" :key="a.assertion_id">
            <td class="mono">{{ a.assertion_id }}</td>
            <td>{{ a.required ? '必需' : '可选' }}</td>
            <td class="mono">{{ bindingByAssertion.get(a.assertion_id)?.primary.source_ref ?? '未绑定' }}</td>
            <td class="mono">{{ bindingByAssertion.get(a.assertion_id)?.primary.expected ?? '—' }}</td>
            <td>{{ bindingByAssertion.get(a.assertion_id)?.supporting.length ?? 0 }}</td>
            <td class="mono small">{{ bindingByAssertion.get(a.assertion_id)?.adjudication_rule ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <div class="split" v-if="state.data.value?.preconditions.length || state.data.value?.postconditions.length">
        <div>
          <h2>前置条件</h2>
          <ul class="plain">
            <li v-for="(p, i) in state.data.value?.preconditions ?? []" :key="i">{{ p.description }}</li>
          </ul>
        </div>
        <div>
          <h2>后置条件</h2>
          <ul class="plain">
            <li v-for="(p, i) in state.data.value?.postconditions ?? []" :key="i">{{ p.description }}</li>
          </ul>
        </div>
      </div>
    </StateBlock>
  </section>
</template>

<style scoped>
.meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 8px 24px;
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 14px 16px;
  margin-bottom: 20px;
  font-size: 13px;
}
.meta .k {
  display: inline-block;
  width: 78px;
  color: var(--tg-muted);
}
</style>
