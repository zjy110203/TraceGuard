<script setup lang="ts">
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'

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
  return typeof v === 'number' ? v : 0
}

function jsonEntries(v: unknown): [string, unknown][] {
  return v && typeof v === 'object' && !Array.isArray(v)
    ? Object.entries(v as Record<string, unknown>)
    : []
}

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

    <div class="cards">
      <MetricCard
        label="Platform API"
        :value="health.error.value ? 'DOWN' : 'UP'"
        :tone="health.error.value ? 'danger' : 'success'"
        @open="health.reload()"
      />
      <MetricCard
        label="Worker 数"
        :value="dispatch.data.value?.workers ?? 0"
        @open="dispatch.reload()"
      />
      <MetricCard
        label="Broker 深度"
        :value="num(dispatch.data.value?.broker)"
        :hint="Object.keys(dispatch.data.value?.broker ?? {}).join(' / ')"
        @open="dispatch.reload()"
      />
      <MetricCard
        label="死信队列"
        :value="dispatch.data.value?.dead_letters.length ?? 0"
        :tone="(dispatch.data.value?.dead_letters.length ?? 0) > 0 ? 'danger' : 'success'"
        @open="dispatch.reload()"
      />
    </div>

    <div class="split">
      <div class="panel">
        <h2>服务状态</h2>
        <StateBlock :loading="health.loading.value" :error="health.error.value" :retry="health.reload">
          <ul class="kv">
            <li v-for="[k, v] in jsonEntries(health.data.value)" :key="k">
              <span class="mono">{{ k }}</span><span class="small">{{ String(v) }}</span>
            </li>
          </ul>
        </StateBlock>
        <StateBlock :loading="version.loading.value" :error="version.error.value" :retry="version.reload">
          <ul class="kv">
            <li v-for="[k, v] in jsonEntries(version.data.value)" :key="k">
              <span class="mono">{{ k }}</span><span class="mono small">{{ String(v) }}</span>
            </li>
          </ul>
        </StateBlock>
      </div>

      <div class="panel">
        <h2>模型提供者</h2>
        <ul class="kv">
          <li>
            <span>LLM Provider</span>
            <StatusPill :value="llmState" />
          </li>
          <li>
            <span>说明</span>
            <span class="small muted">真实 LLM 通过 DeepSeek 接入（见 R4 记录）；本页展示运行时状态</span>
          </li>
        </ul>
        <h2>调度队列</h2>
        <StateBlock :loading="dispatch.loading.value" :error="dispatch.error.value" :retry="dispatch.reload">
          <ul class="kv">
            <li v-for="[k, v] in jsonEntries(dispatch.data.value?.dispatcher)" :key="k">
              <span class="mono">{{ k }}</span><span class="small">{{ String(v) }}</span>
            </li>
          </ul>
          <p v-if="dispatch.data.value?.dead_letters.length" class="error-text small">
            存在 {{ dispatch.data.value.dead_letters.length }} 条死信，需人工排查。
          </p>
          <p v-else class="muted small">无死信。</p>
        </StateBlock>
      </div>
    </div>

    <h2>证据完整性审计</h2>
    <StateBlock
      :loading="audit.loading.value"
      :error="audit.error.value"
      :empty="!audit.data.value?.length"
      empty-hint="尚无证据审计记录。执行 Run 并产生证据操作后会出现（如校验、派生）。"
      :retry="audit.reload"
    >
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
    </StateBlock>

    <h2>Probe 审计</h2>
    <StateBlock
      :loading="probes.loading.value"
      :error="probes.error.value"
      :empty="!probes.data.value?.length"
      empty-hint="尚无 Probe 执行审计记录。"
      :retry="probes.reload"
    >
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
      <p v-if="(probes.data.value?.length ?? 0) > 50" class="muted small">
        仅显示前 50 条（共 {{ probes.data.value?.length }} 条）。
      </p>
    </StateBlock>
  </section>
</template>

<style scoped>
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
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
</style>
