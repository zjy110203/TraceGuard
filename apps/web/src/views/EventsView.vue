<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { api, subscribeEventsSafe } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import Pagination from '../components/Pagination.vue'
import PageHeader from '../components/PageHeader.vue'
import type { EventRow } from '../api/types'

const source = ref<'orchestration' | 'evidence'>('orchestration')
const live = ref(false)
const sseOpen = ref(false)
const liveRows = ref<EventRow[]>([])

const orchestration = useAsync(() => api.events())
const evidence = useAsync(() => api.evidenceEvents({ limit: 200 }))

// Each source owns its own pagination cursor; sharing one caused confusing
// page jumps when switching sources.
const orchPage = ref(1)
const evPage = ref(1)
const pageSize = 20
const page = computed({
  get: () => (source.value === 'orchestration' ? orchPage.value : evPage.value),
  set: (v: number) => {
    if (source.value === 'orchestration') orchPage.value = v
    else evPage.value = v
  },
})

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
      liveRows.value = [event, ...liveRows.value].slice(0, 50)
    },
    (open) => {
      sseOpen.value = open
    },
  )
}
onUnmounted(() => unsubscribe?.())

async function switchSource(next: 'orchestration' | 'evidence'): Promise<void> {
  source.value = next
  if (next === 'orchestration') {
    await orchestration.reload()
  } else {
    await evidence.reload()
  }
}

function refresh(): void {
  if (source.value === 'orchestration') void orchestration.reload()
  else void evidence.reload()
}
</script>

<template>
  <section class="page">
    <PageHeader
      title="Event"
      subtitle="编排事件来自内存总线（重启即清空）；证据事件来自持久化 Event Store。SSE 仅作实时提示，权威状态以表格数据为准。"
    >
      <label class="inline-label">
        <span class="sr-only">数据源</span>
        <select :value="source" @change="switchSource(($event.target as HTMLSelectElement).value as 'orchestration' | 'evidence')">
          <option value="orchestration">编排事件（内存总线）</option>
          <option value="evidence">证据事件（持久化 Event Store）</option>
        </select>
      </label>
      <button
        class="btn"
        :class="{ on: live }"
        type="button"
        :disabled="source === 'evidence'"
        :title="source === 'evidence' ? '实时流只覆盖编排事件' : undefined"
        @click="toggleLive"
      >
        {{ live ? (sseOpen ? '停止实时' : '实时（重连中）') : '实时订阅' }}
      </button>
      <button class="btn" type="button" @click="refresh">刷新</button>
    </PageHeader>

    <div v-if="live" class="live">
      <h2>
        实时帧（{{ liveRows.length }}）
        <span class="small" :class="sseOpen ? 'notice-text' : 'error-text'">
          {{ sseOpen ? '· 已连接' : '· 连接已断开，自动重连中' }}
        </span>
      </h2>
      <p v-if="!liveRows.length" class="muted small">等待新事件…</p>
      <ul v-else class="plain mono small">
        <li v-for="(e, i) in liveRows" :key="`${e.event_id}-${i}`">
          {{ e.event_type }} · {{ e.run_id || '—' }} · seq {{ e.sequence ?? '—' }}
        </li>
      </ul>
    </div>

    <template v-if="source === 'orchestration'">
      <StateBlock
        :loading="orchestration.loading.value"
        :error="orchestration.error.value"
        :empty="!orchestration.data.value?.length"
        empty-hint="尚无编排事件。到 Task 队列提交一次 Run 后这里会出现事件。"
        :retry="orchestration.reload"
      >
        <div class="table-wrap"><table>
          <thead><tr><th>event_type</th><th>run_id</th><th>seq</th><th>发生时间</th></tr></thead>
          <tbody>
            <tr v-for="e in (orchestration.data.value ?? []).slice((orchPage - 1) * pageSize, orchPage * pageSize)" :key="e.event_id">
              <td class="mono">{{ e.event_type }}</td>
              <td class="mono small">{{ e.run_id || '—' }}</td>
              <td class="muted">{{ e.sequence ?? '—' }}</td>
              <td class="muted small">{{ e.occurred_at || '—' }}</td>
            </tr>
          </tbody>
        </table></div>
        <Pagination v-model:page="orchPage" :page-size="pageSize" :total="orchestration.data.value?.length ?? 0" />
      </StateBlock>
    </template>

    <template v-else>
      <StateBlock
        :loading="evidence.loading.value"
        :error="evidence.error.value"
        :empty="!evidence.data.value?.length"
        empty-hint="Event Store 尚无持久化事件。执行 Run 并产生证据后会有记录。"
        :retry="evidence.reload"
      >
        <div class="table-wrap"><table>
          <thead><tr><th>event_type</th><th>run_id</th><th>seq</th><th>producer</th><th>幂等键</th></tr></thead>
          <tbody>
            <tr
              v-for="e in (evidence.data.value ?? []).slice((evPage - 1) * pageSize, evPage * pageSize)"
              :key="`${e.producer_id}-${e.sequence}`"
            >
              <td class="mono">{{ e.event_type }}</td>
              <td class="mono small">{{ e.run_id }}</td>
              <td class="muted">{{ e.sequence }}</td>
              <td class="small">{{ e.producer_id }}</td>
              <td class="mono small muted">{{ e.idempotency_key }}</td>
            </tr>
          </tbody>
        </table></div>
        <Pagination v-model:page="evPage" :page-size="pageSize" :total="evidence.data.value?.length ?? 0" />
      </StateBlock>
    </template>
  </section>
</template>

<style scoped>
.inline-label select {
  min-width: 210px;
}
.btn.on {
  border-color: var(--tg-accent);
  color: var(--tg-accent);
}
.live {
  background: var(--tg-panel);
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 12px 16px;
  margin-bottom: 16px;
}
.live h2 {
  margin-top: 0;
}
.plain {
  margin: 0;
  padding-left: 16px;
  max-height: 180px;
  overflow: auto;
}
</style>
