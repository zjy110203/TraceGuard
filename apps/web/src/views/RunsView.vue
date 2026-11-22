<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { api, subscribeEventsSafe } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import Pagination from '../components/Pagination.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate, routeQuery } from '../router'
import type { RunRow } from '../api/types'

const tasks = useAsync(() => api.tasks())
const taskFilter = ref(routeQuery.value.get('task') ?? '')
const runs = ref<RunRow[]>([])
const loading = ref(true)
const error = ref('')
const live = ref(false)
const sseOpen = ref(false)

const page = ref(1)
const pageSize = 12

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const list = await api.tasks()
    const ids = taskFilter.value ? [taskFilter.value] : list.map((t) => t.task_id)
    // Parallel, not serial: per-task history requests are independent.
    const results = await Promise.all(
      ids.map((id) =>
        api.taskRuns(id).catch(() => [] as RunRow[]),
      ),
    )
    runs.value = results
      .flat()
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}
void load()

const pageRows = computed(() =>
  runs.value.slice((page.value - 1) * pageSize, page.value * pageSize),
)

let unsubscribe: (() => void) | null = null
function toggleLive(): void {
  if (unsubscribe) {
    unsubscribe()
    unsubscribe = null
    live.value = false
    return
  }
  live.value = true
  unsubscribe = subscribeEventsSafe(
    (event) => {
      if (event.event_type?.startsWith('run.')) void load()
    },
    (open) => {
      sseOpen.value = open
    },
  )
}
onUnmounted(() => unsubscribe?.())
</script>

<template>
  <section class="page">
    <PageHeader title="Run 列表" subtitle="Task → Run 两级结构；每次提交都产生新 Run，历史不可覆盖。">
      <label class="inline-label">
        <span class="sr-only">按 Task 筛选</span>
        <select v-model="taskFilter" @change="load()">
          <option value="">全部 Task</option>
          <option v-for="t in tasks.data.value ?? []" :key="t.task_id" :value="t.task_id">
            {{ t.task_id }}
          </option>
        </select>
      </label>
      <button class="btn" :class="{ on: live }" type="button" @click="toggleLive">
        {{ live ? (sseOpen ? '停止实时' : '实时（重连中）') : '实时刷新' }}
      </button>
      <button class="btn" type="button" @click="load()">刷新</button>
    </PageHeader>

    <StateBlock
      :loading="loading"
      :error="error"
      :empty="!runs.length"
      empty-hint="尚无 Run。到 Task 队列创建任务并提交一次执行。"
      :retry="load"
    >
      <table>
        <thead>
          <tr>
            <th>run_id</th><th>task</th><th>尝试</th><th>状态</th><th>Verdict</th><th>诊断</th><th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in pageRows" :key="row.run_id">
            <td class="mono">
              <a href="#" @click.prevent="navigate(`/runs/${row.run_id}`)">{{ row.run_id }}</a>
            </td>
            <td class="mono small">{{ row.task_id }}</td>
            <td>#{{ row.attempt }}</td>
            <td><StatusPill :value="row.status" /></td>
            <td><StatusPill :value="row.test_verdict || 'NOT_EVALUATED'" /></td>
            <td><StatusPill :value="row.diagnosis_status || 'NOT_STARTED'" /></td>
            <td class="muted small">{{ row.created_at?.replace('T', ' ').slice(0, 19) }}</td>
          </tr>
        </tbody>
      </table>
      <Pagination v-model:page="page" :page-size="pageSize" :total="runs.length" />
    </StateBlock>
  </section>
</template>

<style scoped>
.inline-label select {
  min-width: 160px;
}
.btn.on {
  border-color: var(--tg-accent);
  color: var(--tg-accent);
}
</style>
