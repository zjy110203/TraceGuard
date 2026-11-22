<script setup lang="ts">
import { computed } from 'vue'
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import StatusPill from '../components/StatusPill.vue'
import { navigate } from '../router'
import type { RunBrief } from '../api/types'

const state = useAsync(() => api.overview())

const verdictRows = computed(() =>
  Object.entries(state.data.value?.runs_by_verdict ?? {}).sort((a, b) => b[1] - a[1]),
)
const statusRows = computed(() =>
  Object.entries(state.data.value?.runs_by_status ?? {}).sort((a, b) => b[1] - a[1]),
)
const recentRuns = computed<RunBrief[]>(() => state.data.value?.recent_runs ?? [])

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  return iso.replace('T', ' ').slice(0, 19)
}
</script>

<template>
  <section class="page">
    <PageHeader title="项目工作台" subtitle="Agentic Web UI Testing & Quality Intelligence 平台总览">
      <button class="btn" type="button" @click="state.reload()">刷新</button>
    </PageHeader>

    <StateBlock
      :loading="state.loading.value"
      :error="state.error.value"
      :retry="state.reload"
    >
      <div class="cards">
        <MetricCard label="Task" :value="state.data.value?.tasks ?? 0" @open="navigate('/tasks')" />
        <MetricCard label="Run 总数" :value="state.data.value?.runs ?? 0" @open="navigate('/runs')" />
        <MetricCard label="TestCase" :value="state.data.value?.cases ?? 0" @open="navigate('/cases')" />
        <MetricCard label="Evidence" :value="state.data.value?.evidence ?? 0" @open="navigate('/evidence')" />
        <MetricCard
          label="待审批修复"
          :value="state.data.value?.repairs_pending ?? 0"
          :tone="(state.data.value?.repairs_pending ?? 0) > 0 ? 'warn' : 'default'"
          @open="navigate('/repairs')"
        />
      </div>

      <div class="split">
        <div class="panel">
          <h2>最近 Run</h2>
          <p v-if="!recentRuns.length" class="muted small">
            尚无执行记录。在 Task 队列创建任务并提交 Run 后，这里会显示最近执行。
          </p>
          <div v-else class="table-wrap">
            <table>
            <thead>
              <tr><th>Run</th><th>Task</th><th>状态</th><th>Verdict</th><th>时间</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in recentRuns" :key="r.run_id">
                <td>
                  <a :href="`#/runs/${r.run_id}`" @click.prevent="navigate(`/runs/${r.run_id}`)">
                    <span class="mono">{{ r.run_id.slice(0, 22) }}…</span>
                  </a>
                </td>
                <td class="mono small">{{ r.task_id }}</td>
                <td><StatusPill :value="r.status" /></td>
                <td><StatusPill :value="r.test_verdict || 'NOT_EVALUATED'" /></td>
                <td class="small muted">{{ fmtTime(r.created_at) }}</td>
              </tr>
            </tbody>
          </table>
          </div>
        </div>

        <div class="panel">
          <h2>Verdict 分布</h2>
          <p v-if="!verdictRows.length" class="muted small">尚无判定结果。</p>
          <ul v-else class="kv">
            <li v-for="[key, n] in verdictRows" :key="key">
              <span><StatusPill :value="key" /></span><b>{{ n }}</b>
            </li>
          </ul>
          <h2>Run 状态分布</h2>
          <p v-if="!statusRows.length" class="muted small">尚无 Run 记录。</p>
          <ul v-else class="kv">
            <li v-for="[key, n] in statusRows" :key="key">
              <span>{{ key }}</span><b>{{ n }}</b>
            </li>
          </ul>
        </div>
      </div>

      <p class="hint metric-note">
        统计口径：页面仅展示后端 /overview 返回的真实计数；成功率等需要明确分母口径的指标未在此展示。
      </p>
    </StateBlock>
  </section>
</template>

<style scoped>
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.panel {
  background: var(--tg-panel);
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 14px 16px;
}
.panel h2 {
  margin-top: 0;
}
.metric-note {
  margin-top: 20px;
}
</style>
