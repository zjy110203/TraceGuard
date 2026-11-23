<script setup lang="ts">
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'

const health = useAsync(() => api.health())
const version = useAsync(() => api.version())
const dispatch = useAsync(() => api.dispatchStatus())
const audit = useAsync(() => api.evidenceAudit())
const probes = useAsync(() => api.probeAudit())

function reloadAll(): void {
  void health.reload()
  void version.reload()
  void dispatch.reload()
  void audit.reload()
  void probes.reload()
}

function num(v: unknown): number {
  if (typeof v === 'number') return v
  if (v && typeof v === 'object') {
    return Object.values(v as Record<string, unknown>).reduce<number>(
      (s, x) => s + (typeof x === 'number' ? x : 0),
      0,
    )
  }
  return 0
}

/**
 * Flatten one level of nesting so `db: {ok: true, error: null}` renders as
 * `db.ok` / `db.error` instead of the "[object Object]" that String() gives.
 */
function flatEntries(v: unknown, prefix = ''): [string, string][] {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return []
  const out: [string, string][] = []
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (val && typeof val === 'object' && !Array.isArray(val)) {
      out.push(...flatEntries(val, key))
    } else if (Array.isArray(val)) {
      out.push([key, `${val.length} 项`])
    } else if (val === null || val === undefined) {
      out.push([key, '—'])
    } else {
      out.push([key, String(val)])
    }
  }
  return out
}

const healthEntries = () => flatEntries(health.data.value)
const versionEntries = () => flatEntries(version.data.value)
const dispatcherEntries = () => flatEntries(dispatch.data.value?.dispatcher)

// Real LLM provider state: until a provider is wired into the runtime, this is
// NOT_CONNECTED — shown honestly rather than faked.
const llmState = 'NOT_CONNECTED'
</script>

<template>
  <section class="page">
    <PageHeader
      title="系统与审计"
      subtitle="服务状态、调度队列、模型提供者与审计记录。此处只展示真实可观测的数据。"
    >
      <button class="btn" type="button" @click="reloadAll">刷新</button>
    </PageHeader>

    <div class="grid-cards cards">
      <StatCard
        label="Platform API"
        :value="health.error.value ? 'DOWN' : health.data.value ? 'UP' : '—'"
        :tone="health.error.value ? 'danger' : 'success'"
        :hint="health.error.value ? '健康检查失败' : '健康检查通过'"
        icon="system"
        clickable
        @open="health.reload()"
      />
      <StatCard
        label="Worker 数"
        :value="dispatch.data.value?.workers ?? 0"
        hint="执行工作线程"
        icon="task"
        clickable
        @open="dispatch.reload()"
      />
      <StatCard
        label="队列深度"
        :value="num(dispatch.data.value?.broker)"
        :hint="Object.keys(dispatch.data.value?.broker ?? {}).join(' / ') || '队列为空'"
        icon="layers"
        clickable
        @open="dispatch.reload()"
      />
      <StatCard
        label="死信队列"
        :value="dispatch.data.value?.dead_letters.length ?? 0"
        :tone="(dispatch.data.value?.dead_letters.length ?? 0) > 0 ? 'danger' : 'default'"
        :hint="(dispatch.data.value?.dead_letters.length ?? 0) > 0 ? '需人工排查' : '无死信'"
        icon="alert"
        clickable
        @open="dispatch.reload()"
      />
    </div>

    <div class="split">
      <div class="card">
        <div class="card-head"><h2>服务状态</h2></div>
        <div class="card-body">
          <StateBlock :loading="health.loading.value" :error="health.error.value" :retry="health.reload">
            <ul class="kv">
              <li v-for="[k, v] in healthEntries()" :key="k">
                <span class="mono">{{ k }}</span><span class="mono small">{{ v }}</span>
              </li>
            </ul>
          </StateBlock>
          <div class="divider" />
          <StateBlock :loading="version.loading.value" :error="version.error.value" :retry="version.reload">
            <ul class="kv">
              <li v-for="[k, v] in versionEntries()" :key="k">
                <span class="mono">{{ k }}</span><span class="mono small">{{ v }}</span>
              </li>
            </ul>
          </StateBlock>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h2>模型提供者与调度</h2></div>
        <div class="card-body">
          <ul class="kv">
            <li>
              <span>LLM Provider</span>
              <StatusPill :value="llmState" />
            </li>
            <li>
              <span>说明</span>
              <span class="small muted">真实 LLM 通过 DeepSeek 接入（见 R4 记录）；本页展示运行时状态，未接入时如实标记。</span>
            </li>
          </ul>
          <div class="divider" />
          <StateBlock
            :loading="dispatch.loading.value"
            :error="dispatch.error.value"
            :retry="dispatch.reload"
          >
            <ul class="kv">
              <li v-for="[k, v] in dispatcherEntries()" :key="k">
                <span class="mono">{{ k }}</span><span class="mono small">{{ v }}</span>
              </li>
            </ul>
            <p v-if="dispatch.data.value?.dead_letters.length" class="error-text small dead-note">
              存在 {{ dispatch.data.value.dead_letters.length }} 条死信，需人工排查。
            </p>
            <p v-else class="muted small dead-note">无死信。</p>
          </StateBlock>
        </div>
      </div>
    </div>

    <div class="card flush">
      <div class="card-head">
        <h2>证据完整性审计</h2>
        <span class="small muted">不可变操作日志</span>
      </div>
      <div v-if="audit.loading.value" class="card-body"><p class="muted small">加载中…</p></div>
      <div v-else-if="audit.error.value" class="card-body">
        <p class="error-text">{{ audit.error.value }}</p>
        <button class="btn sm" type="button" @click="audit.reload">重试</button>
      </div>
      <EmptyState
        v-else-if="!audit.data.value?.length"
        icon="shield"
        title="尚无证据审计记录"
        hint="执行 Run 并产生证据操作后会出现（如校验、派生）。"
      />
      <div v-else class="table-wrap bare">
        <table>
          <thead><tr><th>action</th><th>detail</th><th>recorded_at</th></tr></thead>
          <tbody>
            <tr v-for="(row, i) in audit.data.value ?? []" :key="i">
              <td class="mono small">{{ row.action }}</td>
              <td class="small muted">{{ row.detail }}</td>
              <td class="small">{{ row.recorded_at }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card flush">
      <div class="card-head">
        <h2>Probe 审计</h2>
        <span class="small muted">网关决策记录</span>
      </div>
      <div v-if="probes.loading.value" class="card-body"><p class="muted small">加载中…</p></div>
      <div v-else-if="probes.error.value" class="card-body">
        <p class="error-text">{{ probes.error.value }}</p>
        <button class="btn sm" type="button" @click="probes.reload">重试</button>
      </div>
      <EmptyState
        v-else-if="!probes.data.value?.length"
        icon="probes"
        title="尚无 Probe 执行审计记录"
        hint="诊断触发 Probe 后，网关会记录每次调用的决策与校验结果。"
      />
      <template v-else>
        <div class="table-wrap bare">
          <table>
            <thead><tr><th>probe_id</th><th>caller</th><th>decision</th><th>check</th><th>at</th></tr></thead>
            <tbody>
              <tr v-for="(row, i) in (probes.data.value ?? []).slice(0, 50)" :key="i">
                <td class="mono small">{{ row.probe_id }}</td>
                <td class="mono small">{{ row.caller }}</td>
                <td><StatusPill :value="row.decision" /></td>
                <td class="mono small">{{ row.check }}</td>
                <td class="small muted">{{ row.at }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="(probes.data.value?.length ?? 0) > 50" class="muted small more-note">
          仅显示前 50 条（共 {{ probes.data.value?.length }} 条）。
        </p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.cards {
  margin-bottom: 18px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
.dead-note {
  margin: 10px 0 0;
}
.more-note {
  padding: 8px var(--tg-space-5);
}
.split .card {
  margin-bottom: 0;
}
@media (max-width: 1160px) {
  .cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 560px) {
  .cards {
    grid-template-columns: 1fr;
  }
}
</style>
