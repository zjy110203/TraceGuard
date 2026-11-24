<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { api, subscribeEventStream, type StreamNotice, type StreamState } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import Pagination from '../components/Pagination.vue'
import PageHeader from '../components/PageHeader.vue'
import type { EventRow } from '../api/types'

const source = ref<'orchestration' | 'evidence'>('orchestration')
const live = ref(false)
const sseOpen = ref(false)
const streamState = ref<StreamState>('closed')
const reconnects = ref(0)
const notices = ref<StreamNotice[]>([])
const liveRows = ref<EventRow[]>([])

const STATE_TEXT: Record<StreamState, string> = {
  connecting: '连接中…',
  live: '已连接（实时）',
  reconnecting: '连接已断开，自动重连中',
  closed: '已关闭',
}

function noticeText(notice: StreamNotice): string {
  switch (notice.code) {
    case 'cursor-unknown':
      return `游标无法识别（${notice.detail ?? '服务端不认识这个 Last-Event-ID'}）；为避免重复投递，未回放历史`
    case 'cursor-expired':
      return `游标已过期（保留水位 ${notice.pruned_through ?? '—'}）；已回放仍保留的历史`
    case 'gap':
      return `历史缺口 ${notice.from ?? '?'}→${notice.to ?? '?'}（缺 ${notice.missing ?? '?'} 条，队列溢出）`
    case 'persist-degraded':
      return `事件写入失败，当前流不可回溯：${notice.last_error ?? notice.detail ?? '未知原因'}`
    case 'persist-recovered': {
      // R5-5.1-R R4: the server exposes DISJOINT per-outage intervals. The old
      // first→last pair is an aggregate that may span successful writes; never
      // render it as one continuous non-replayable hole when intervals exist.
      const ivs = notice.undurable_intervals as
        | Array<{ first: number; last: number; count: number; recovered_at: number | null }>
        | undefined
      const rangeText =
        ivs && ivs.length
          ? ivs.map((iv) => `${iv.first}→${iv.last}`).join('、')
          : `${notice.undurable_first_delivery ?? '?'}→${notice.undurable_last_delivery ?? '?'}`
      return `事件写入已恢复（失败 ${notice.failures ?? '?'} 次，未持久化区间 ${rangeText}）；这些区间不可回放，区间之间的事件已正常持久化`
    }
    default:
      return notice.code + (notice.detail ? `：${notice.detail}` : '')
  }
}

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
  notices.value = []
  unsubscribe = subscribeEventStream({
    onEvent: (event) => {
      liveRows.value = [event, ...liveRows.value].slice(0, 50)
    },
    onState: (state, info) => {
      streamState.value = state
      reconnects.value = info.reconnects
      sseOpen.value = state === 'live'
    },
    onNotice: (notice) => {
      notices.value = [notice, ...notices.value].slice(0, 10)
    },
  })
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
      subtitle="编排事件已持久化（R5-5.1：重启后可回放，Last-Event-ID 可续接）；证据事件来自 Event Store。SSE 仅作实时提示，权威状态以表格数据为准。"
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
        {{ live ? '停止实时' : '实时订阅' }}
      </button>
      <button class="btn" type="button" @click="refresh">刷新</button>
    </PageHeader>

    <div v-if="live" class="live">
      <h2>
        实时帧（{{ liveRows.length }}）
        <span class="small" :class="sseOpen ? 'notice-text' : 'error-text'">
          · {{ STATE_TEXT[streamState] }}
          <template v-if="reconnects">（已重连 {{ reconnects }} 次）</template>
        </span>
      </h2>
      <ul v-if="notices.length" class="plain small notices">
        <li v-for="(n, i) in notices" :key="`${n.code}-${i}`" class="notice-text">
          {{ noticeText(n) }}
        </li>
      </ul>
      <p v-if="!liveRows.length" class="muted small">等待新事件…</p>
      <ul v-else class="plain mono small">
        <li v-for="(e, i) in liveRows" :key="`${e.event_id}-${i}`">
          {{ e.event_type }} · {{ e.run_id || '—' }} · seq {{ e.sequence ?? '—' }}
          <span v-if="e.durable === false" class="error-text">· 未持久化（不可回放）</span>
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
.notices {
  margin-bottom: 10px;
  padding-left: 18px;
}
</style>
