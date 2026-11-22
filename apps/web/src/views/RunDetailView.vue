<script setup lang="ts">
import { computed, ref } from 'vue'
import { api, ApiError } from '../api/client'
import { messageOf, useAsync } from '../composables/useAsync'
import type { RunReport } from '../api/types'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { navigate, isActive } from '../router'

const props = defineProps<{ runId: string }>()
const state = useAsync(() => api.run(props.runId))
// Report keeps three states distinct: data / genuinely-not-generated (404) /
// request-failed (anything else). A swallowed error used to fake "no report".
const report = ref<RunReport | null>(null)
const reportStatus = ref<'loading' | 'ready' | 'missing' | 'failed'>('loading')
const reportError = ref('')

function loadReport(): void {
  reportStatus.value = 'loading'
  reportError.value = ''
  api
    .report(props.runId)
    .then((data) => {
      report.value = data
      reportStatus.value = 'ready'
    })
    .catch((err) => {
      if (err instanceof ApiError && err.status === 404) {
        reportStatus.value = 'missing'
      } else {
        reportStatus.value = 'failed'
        reportError.value = messageOf(err)
      }
    })
}
void loadReport()

const terminal = computed(() =>
  ['COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED'].includes(state.data.value?.status ?? ''),
)

const confirmCancel = ref(false)
const cancelError = ref('')
const cancelBusy = ref(false)

async function doCancel(): Promise<void> {
  confirmCancel.value = false
  cancelBusy.value = true
  cancelError.value = ''
  try {
    await api.cancelRun(props.runId)
    await state.reload()
  } catch (err) {
    cancelError.value = messageOf(err)
  } finally {
    cancelBusy.value = false
  }
}

const tabs = computed(() => [
  { label: '步骤执行', path: `/runs/${props.runId}/steps` },
  { label: '断言结果', path: `/runs/${props.runId}/assertions` },
  { label: '诊断', path: `/runs/${props.runId}/diagnosis` },
])
</script>

<template>
  <section class="page">
    <header class="head">
      <div>
        <h1 class="mono">{{ props.runId }}</h1>
        <p class="sub muted small" v-if="state.data.value">
          {{ state.data.value.case_id }} · 第 {{ state.data.value.attempt }} 次尝试
        </p>
      </div>
      <div class="actions-row">
        <button class="btn" type="button" @click="state.reload()">刷新</button>
        <button class="btn danger" type="button" :disabled="terminal || cancelBusy" @click="confirmCancel = true">
          取消运行
        </button>
      </div>
    </header>

    <p v-if="cancelError" class="error-text">取消失败：{{ cancelError }}</p>

    <StateBlock :loading="state.loading.value" :error="state.error.value" :retry="state.reload">
      <div class="meta">
        <div><span class="k">状态</span><StatusPill :value="state.data.value?.status ?? ''" /></div>
        <div><span class="k">执行</span><StatusPill :value="state.data.value?.execution_status ?? ''" /></div>
        <div><span class="k">Verdict</span><StatusPill :value="state.data.value?.test_verdict || 'NOT_EVALUATED'" /></div>
        <div><span class="k">诊断</span><StatusPill :value="state.data.value?.diagnosis_status || 'NOT_STARTED'" /></div>
        <div><span class="k">开始</span><span class="small">{{ state.data.value?.started_at || '—' }}</span></div>
        <div><span class="k">结束</span><span class="small">{{ state.data.value?.finished_at || '—' }}</span></div>
        <div><span class="k">心跳</span><span class="small">{{ state.data.value?.heartbeat_at || '—' }}</span></div>
        <div><span class="k">中止原因</span><span class="small">{{ state.data.value?.abort_reason || '—' }}</span></div>
      </div>

      <nav class="tabs" aria-label="Run 详情标签页">
        <a
          v-for="t in tabs"
          :key="t.path"
          href="#"
          :class="{ current: isActive(t.path) }"
          @click.prevent="navigate(t.path)"
        >
          {{ t.label }}
        </a>
        <span class="muted small tab-note">Event 与 Evidence 请在证据中心按 run_id 检索</span>
      </nav>

      <h2>执行报告</h2>
      <div v-if="reportStatus === 'failed'" class="report-state error-text">
        <span>报告加载失败：{{ reportError }}</span>
        <button class="btn" type="button" @click="loadReport">重试</button>
      </div>
      <div v-else-if="reportStatus === 'loading'" class="report-state muted small">报告加载中…</div>
      <p v-else-if="reportStatus === 'missing'" class="muted small">
        该 Run 尚未生成报告（可能仍在执行或已中止）。
      </p>
      <div v-else>
        <p class="small muted">
          证据 {{ report?.evidence.length ?? 0 }} 条 · 事件 {{ report?.events.length ?? 0 }} 条 ·
          观测 {{ Object.keys(report?.observations ?? {}).length }} 项
        </p>
        <table>
          <thead><tr><th>step_id</th><th>action</th><th>结果</th><th>细节</th></tr></thead>
          <tbody>
            <tr v-for="s in report?.steps ?? []" :key="s.step_id">
              <td class="mono">{{ s.step_id }}</td>
              <td>{{ s.action }}</td>
              <td><StatusPill :value="s.ok ? 'PASSED' : 'FAILED'" /></td>
              <td class="small muted">{{ s.detail }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </StateBlock>

    <ConfirmDialog
      v-if="confirmCancel"
      title="取消运行"
      :body="`确定取消 ${props.runId}？取消是协作式中止：当前步骤完成后 Run 进入 CANCELLED。`"
      confirm-label="确认取消"
      danger
      @confirm="doCancel"
      @cancel="confirmCancel = false"
    />
  </section>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 16px;
}
.sub {
  margin: 2px 0 0;
}
.actions-row {
  display: flex;
  gap: 8px;
}
.meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px 24px;
  background: var(--tg-panel);
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 14px 16px;
  font-size: 13px;
}
.meta .k {
  display: inline-block;
  min-width: 72px;
  color: var(--tg-muted);
}
.tabs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin: 20px 0 8px;
  font-size: 13px;
  border-bottom: 1px solid var(--tg-border);
  padding-bottom: 0;
}
.tabs a {
  padding: 6px 12px;
  border-radius: 6px 6px 0 0;
  color: var(--tg-muted);
  border-bottom: 2px solid transparent;
}
.tabs a:hover {
  color: var(--tg-accent);
  text-decoration: none;
}
.tabs a.current {
  color: var(--tg-accent);
  font-weight: 500;
  border-bottom-color: var(--tg-accent);
}
.tab-note {
  margin-left: 12px;
}
.report-state {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
}
</style>
