<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api/client'
import { navigate, routeQuery } from '../router'
import type { Diagnosis } from '../api/types'
import { messageOf } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import PageHeader from '../components/PageHeader.vue'

const props = defineProps<{ runId?: string }>()
// Two entry points exist: `/runs/{id}/diagnosis` (path segment) and
// `/diagnosis?run={id}` (link from a case). Both must prefill the same field,
// otherwise the second one lands on an empty page.
const runId = ref(props.runId || routeQuery.value.get('run') || '')
const data = ref<Diagnosis | null>(null)
const loading = ref(false)
const error = ref('')

async function load(): Promise<void> {
  if (!runId.value.trim()) {
    data.value = null
    error.value = ''
    return
  }
  loading.value = true
  error.value = ''
  try {
    data.value = await api.diagnosis(runId.value.trim())
  } catch (err) {
    error.value = messageOf(err)
    data.value = null
  } finally {
    loading.value = false
  }
}

if (runId.value) {
  void load()
}

watch(
  () => routeQuery.value.get('run'),
  (value) => {
    if (value && value !== runId.value) {
      runId.value = value
      void load()
    }
  },
)
</script>

<template>
  <section class="page">
    <PageHeader title="诊断" subtitle="Run 失败诊断" />
    <p class="hint">
      CONFIRMED 只由确定性规则 + 权威证据触发，LLM 不参与定论；证据不足时保持 INCONCLUSIVE。
    </p>

    <div class="search">
      <label class="sr-only" for="diag-run">run_id</label>
      <input id="diag-run" v-model="runId" placeholder="run_id" @keyup.enter="load" />
      <button class="btn" type="button" :disabled="loading" @click="load">
        {{ loading ? '诊断中…' : '运行诊断' }}
      </button>
    </div>

    <!-- Round trip: run -> diagnosis -> evidence -> run. Without these the
         diagnosis page is a dead end reached only by retyping a run_id. -->
    <p v-if="data" class="xlinks">
      <a href="#" @click.prevent="navigate(`/runs/${data.run_id}`)">← 返回运行</a>
      <a href="#" @click.prevent="navigate(`/evidence?run=${data.run_id}`)">查看该 Run 证据</a>
    </p>

    <StateBlock
      :loading="loading"
      :error="error"
      :empty="!data"
      empty-hint="输入 run_id 后查询。可从 Run 列表复制 run_id。"
    >
      <div class="meta">
        <div><span class="k">状态</span><StatusPill :value="data?.diagnosis_status ?? ''" /></div>
        <div><span class="k">路由</span><StatusPill :value="data?.disposition ?? ''" /></div>
        <div><span class="k">失败域</span><span class="v mono">{{ data?.failure_domain }}</span></div>
        <div><span class="k">机制</span><span class="v mono">{{ data?.failure_mechanism }}</span></div>
        <div><span class="k">波动特征</span><span class="v mono">{{ data?.flaky_characteristic }}</span></div>
        <div><span class="k">run_id</span><span class="v mono small">{{ data?.run_id }}</span></div>
      </div>

      <p class="symptom"><b>症状</b> {{ data?.symptom }}</p>

      <div class="split">
        <div>
          <h2>根因</h2>
          <ul v-if="data?.root_causes.length" class="plain">
            <li v-for="(c, i) in data.root_causes" :key="i">{{ c }}</li>
          </ul>
          <p v-else class="muted small">无已确认根因（未达 CONFIRMED）。</p>
        </div>
        <div>
          <h2>促成因素</h2>
          <ul v-if="data?.contributing_factors.length" class="plain">
            <li v-for="(c, i) in data.contributing_factors" :key="i">{{ c }}</li>
          </ul>
          <p v-else class="muted small">—</p>
        </div>
      </div>

      <h2>假设（{{ data?.hypotheses.length ?? 0 }}）</h2>
      <div v-for="h in data?.hypotheses ?? []" :key="h.hypothesis_id" class="hyp">
        <div class="hyp-head">
          <span class="mono small">{{ h.hypothesis_id }}</span>
          <StatusPill :value="h.status" />
          <span class="mono small muted">{{ h.failure_domain }} / {{ h.failure_mechanism }}</span>
        </div>
        <p class="small">{{ h.statement }}</p>
        <p v-if="h.confirmation_rule" class="small mono muted">规则：{{ h.confirmation_rule }}</p>
        <div class="split">
          <div>
            <p class="lbl">支持证据（{{ h.supporting.length }}）</p>
            <ul class="plain small">
              <li v-for="r in h.supporting" :key="r.ref_id">{{ r.ref_id }} · {{ r.detail }}</li>
            </ul>
          </div>
          <div>
            <p class="lbl">反向证据（{{ h.contradicting.length }}）</p>
            <ul class="plain small">
              <li v-for="r in h.contradicting" :key="r.ref_id">{{ r.ref_id }} · {{ r.detail }}</li>
            </ul>
          </div>
        </div>
      </div>

      <h2>诊断备注</h2>
      <ul class="plain small muted">
        <li v-for="(n, i) in data?.notes ?? []" :key="i">{{ n }}</li>
      </ul>
    </StateBlock>
  </section>
</template>

<style scoped>
.search {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
.search input {
  width: 320px;
}
.xlinks {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 13px;
  margin: 0 0 12px;
}
.meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px 24px;
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 14px 16px;
  font-size: 13px;
}
.meta .k {
  display: inline-block;
  width: 76px;
  color: var(--tg-muted);
}
.symptom {
  font-size: 13px;
  margin: 16px 0;
}
.hyp {
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 12px 16px;
  margin-bottom: 12px;
}
.hyp-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.lbl {
  font-size: 12px;
  color: var(--tg-muted);
  margin: 6px 0 2px;
}
</style>
