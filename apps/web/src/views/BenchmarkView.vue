<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StatusPill from '../components/StatusPill.vue'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'
import type { DatasetRow, ExperimentRow } from '../api/types'

const datasets = useAsync(() => api.benchmarkDatasets())
const experiments = useAsync(() => api.benchmarkExperiments())

function reloadAll(): void {
  void datasets.reload()
  void experiments.reload()
}

const openExperiment = ref('')

function toggleOpen(id: string): void {
  openExperiment.value = openExperiment.value === id ? '' : id
}

// Metric semantics: null means "not measurable this run" — display as the
// explicit status, never as 0 or a fabricated number.
function metricState(v: number | null): string {
  return v === null ? 'NOT_RUN' : ''
}

const datasetById = computed(() => {
  const map = new Map<string, DatasetRow>()
  for (const d of datasets.data.value ?? []) map.set(d.dataset_id, d)
  return map
})
</script>

<template>
  <section class="page">
    <PageHeader
      title="UI-FaultBench"
      subtitle="数据集 / Ground Truth / 实验评测。结果可回溯到数据集版本与实验环境；未运行的实验标记为 NOT_RUN，不伪造数字。"
    >
      <button class="btn" type="button" @click="reloadAll">刷新</button>
    </PageHeader>

    <div class="card flush">
      <div class="card-head">
        <h2>数据集</h2>
        <span class="small muted">由 Ground Truth 记录驱动创建</span>
      </div>
      <div v-if="datasets.loading.value" class="card-body">
        <p class="muted small">加载中…</p>
      </div>
      <div v-else-if="datasets.error.value" class="card-body">
        <p class="error-text">{{ datasets.error.value }}</p>
        <button class="btn sm" type="button" @click="datasets.reload">重试</button>
      </div>
      <EmptyState
        v-else-if="!datasets.data.value?.length"
        icon="layers"
        title="尚无数据集"
        hint="数据集由 Ground Truth 记录驱动创建。GT 的 5 个 fault_id → primary_cause 映射仍为 UNRESOLVED，在获批并完成导入前，此处为空是预期状态。"
      />
      <div v-else class="table-wrap bare">
        <table>
          <thead><tr><th>dataset_id</th><th>分区</th><th>版本</th><th>场景数</th><th>隔离说明</th></tr></thead>
          <tbody>
            <tr v-for="d in (datasets.data.value ?? []) as DatasetRow[]" :key="d.dataset_id">
              <td class="mono">{{ d.dataset_id }}</td>
              <td><StatusPill :value="d.partition" /></td>
              <td class="mono small">{{ d.version }}</td>
              <td>{{ d.scenario_ids.length }}</td>
              <td class="small muted">
                {{ d.partition.includes('TRUSTED') ? 'Trusted Test Set（人工审核来源）' : 'Agent Generated Test Set（与 Trusted 隔离）' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card flush">
      <div class="card-head">
        <h2>实验</h2>
        <span class="small muted">三类评测：测试生成 / 诊断 / 自愈</span>
      </div>
      <div v-if="experiments.loading.value" class="card-body">
        <p class="muted small">加载中…</p>
      </div>
      <div v-else-if="experiments.error.value" class="card-body">
        <p class="error-text">{{ experiments.error.value }}</p>
        <button class="btn sm" type="button" @click="experiments.reload">重试</button>
      </div>
      <EmptyState
        v-else-if="!experiments.data.value?.length"
        icon="benchmark"
        title="尚无实验记录"
        hint="评测运行后会出现在这里。真实 LLM 未接入的实验标记为 NOT_RUN；被门禁阻塞的指标标记为 BLOCKED —— 两种情况都不会显示为数字。"
      />
      <div v-else class="exp-list">
        <div v-for="e in (experiments.data.value ?? []) as ExperimentRow[]" :key="e.experiment_id" class="exp-item">
          <button class="exp-head" type="button" @click="toggleOpen(e.experiment_id)">
            <span class="mono">{{ e.experiment_id }}</span>
            <StatusPill :value="e.status" />
            <span class="small muted">{{ e.track }} · {{ e.method }} · {{ e.scenario_count }} 场景</span>
            <span class="muted small toggle-mark">{{ openExperiment === e.experiment_id ? '收起 ▲' : '展开 ▼' }}</span>
          </button>
          <p v-if="e.note" class="hint">{{ e.note }}</p>

          <div v-if="openExperiment === e.experiment_id" class="exp-body">
            <p class="small muted">
              数据集：
              <span class="mono">{{ e.dataset_id }}</span>
              <template v-if="datasetById.get(e.dataset_id)">
                · 版本 <span class="mono">{{ datasetById.get(e.dataset_id)?.version }}</span>
              </template>
            </p>
            <h3 class="small muted">指标</h3>
            <div class="table-wrap">
              <table>
                <thead><tr><th>指标</th><th>值</th><th>说明</th></tr></thead>
                <tbody>
                  <tr v-for="(v, k) in e.metrics" :key="k">
                    <td class="mono">{{ k }}</td>
                    <td>
                      <StatusPill v-if="metricState(v)" :value="metricState(v)" />
                      <b v-else>{{ v }}</b>
                    </td>
                    <td class="small muted">
                      {{ v === null ? '本实验未产生该指标（未运行或被门禁阻塞）' : '' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.exp-head {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0;
  text-align: left;
  color: var(--tg-text);
  font: inherit;
  flex-wrap: wrap;
}
.exp-head:focus-visible {
  outline: 2px solid var(--tg-accent);
}
.toggle-mark {
  margin-left: auto;
}
.exp-item + .exp-item {
  border-top: 1px solid var(--tg-border);
}
.exp-item {
  padding: 12px var(--tg-space-5);
}
.exp-body {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--tg-border);
}
@media (max-width: 720px) {
  .exp-item {
    padding: 12px var(--tg-space-3);
  }
}
</style>
