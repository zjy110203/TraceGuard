<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import type { ChainSummary, OracleBinding } from '../api/types'
import { messageOf, useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'

const props = defineProps<{ caseId: string }>()
const state = useAsync(() => api.caseDetail(props.caseId))

// Full chain: Run -> Oracle -> Evidence -> Diagnosis. The run is real (Chrome
// against the local target) and takes tens of seconds, so the button states
// that plainly instead of looking instantaneous.
const chainBusy = ref(false)
const chainError = ref('')
const chainResult = ref<ChainSummary | null>(null)

const approved = computed(() => (state.data.value?.approved_by ?? '').trim().length > 0)

async function runChain(): Promise<void> {
  chainBusy.value = true
  chainError.value = ''
  chainResult.value = null
  try {
    chainResult.value = await api.runChain({ case_id: props.caseId })
  } catch (err) {
    chainError.value = messageOf(err)
  } finally {
    chainBusy.value = false
  }
}

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
      <button
        class="btn primary"
        type="button"
        :disabled="!approved || chainBusy"
        :title="approved ? '执行真实浏览器全链（约数十秒）' : '未通过人工批准的用例不可执行'"
        @click="runChain"
      >
        {{ chainBusy ? '全链执行中…' : '执行全链' }}
      </button>
      <button class="btn" type="button" @click="navigate(`/cases/${props.caseId}/versions`)">版本历史</button>
      <button class="btn" type="button" @click="state.reload()">刷新</button>
    </PageHeader>

    <p v-if="chainError" class="alert error" role="alert">{{ chainError }}</p>
    <div v-if="chainResult" class="chain-result card">
      <div class="card-head">
        <h2>全链结果</h2>
        <span class="pill" :class="chainResult.test_verdict === 'PASSED' ? 'ok' : 'warn'">
          {{ chainResult.test_verdict ?? '未判定' }}
        </span>
      </div>
      <div class="card-body">
        <div class="kv-grid">
          <div class="kv"><span>chain</span><b class="mono">{{ chainResult.chain_id }}</b></div>
          <div class="kv"><span>run</span><b class="mono">{{ chainResult.run_id ?? '—' }}</b></div>
          <div class="kv"><span>证据 / 事件</span><b>{{ chainResult.evidence_count }} / {{ chainResult.event_count }}</b></div>
          <div class="kv"><span>诊断</span><b>{{ chainResult.diagnosis_status ?? '—' }} · {{ chainResult.disposition ?? '—' }}</b></div>
          <div class="kv"><span>耗时</span><b>{{ chainResult.duration_s ?? '—' }} s</b></div>
          <div class="kv"><span>落库</span><b>证据 {{ chainResult.persisted?.evidence?.written ?? 0 }} · 事件 {{ chainResult.persisted?.events?.written ?? 0 }}</b></div>
        </div>
        <div class="links">
          <a v-if="chainResult.run_id" href="#" @click.prevent="navigate(`/runs/${chainResult.run_id}`)">查看运行详情</a>
          <a href="#" @click.prevent="navigate('/evidence')">证据台账</a>
          <a href="#" @click.prevent="navigate('/graph')">证据图谱</a>
          <a v-if="chainResult.run_id" href="#" @click.prevent="navigate(`/runs/${chainResult.run_id}/diagnosis`)">诊断</a>
        </div>
      </div>
    </div>

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
      <div class="table-wrap"><table>
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
      </table></div>

      <h2>断言与 Oracle 绑定 ({{ state.data.value?.assertions.length ?? 0 }})</h2>
      <div class="table-wrap"><table>
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
      </table></div>

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
.chain-result {
  margin-bottom: 16px;
}
.chain-result .links {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 13px;
}
.kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px 18px;
  margin-bottom: 12px;
  font-size: 13px;
}
.kv-grid .kv span {
  display: block;
  color: var(--tg-muted);
  font-size: 11.5px;
}
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
