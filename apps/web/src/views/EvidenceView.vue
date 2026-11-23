<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import Pagination from '../components/Pagination.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate, routeQuery } from '../router'

const runFilter = ref(routeQuery.value.get('run') ?? '')
const layer = ref('')

const records = useAsync(() => api.evidenceRecords({ run_id: runFilter.value, limit: 200 }))
const gaps = useAsync(() => api.evidenceGaps())
const page = ref(1)
const pageSize = 15
const expanded = ref<string | null>(null)

function toggleRow(id: string): void {
  expanded.value = expanded.value === id ? null : id
}

const rows = computed(() => {
  const all = records.data.value ?? []
  const filtered = layer.value
    ? all.filter((r) => (layer.value === 'raw' ? r.raw : !r.raw))
    : all
  return filtered.slice((page.value - 1) * pageSize, page.value * pageSize)
})
const total = computed(() => {
  const all = records.data.value ?? []
  return layer.value ? all.filter((r) => (layer.value === 'raw' ? r.raw : !r.raw)).length : all.length
})

async function applyFilter(): Promise<void> {
  page.value = 1
  await records.reload()
}
</script>

<template>
  <section class="page">
    <PageHeader title="Evidence" subtitle="证据记录与完整性">
      <label class="sr-only" for="evidence-run">按 run_id 过滤</label>
      <input
        id="evidence-run"
        v-model="runFilter"
        placeholder="按 run_id 过滤"
        @keyup.enter="applyFilter"
      />
      <label class="sr-only" for="evidence-layer">证据层级</label>
      <select id="evidence-layer" v-model="layer" @change="page = 1">
        <option value="">全部层级</option>
        <option value="raw">原始（raw）</option>
        <option value="derived">派生（derived）</option>
      </select>
      <button class="btn" type="button" @click="applyFilter">应用</button>
    </PageHeader>
    <p class="hint">派生证据不会与来源算作独立佐证；完整性以 sha256 记录。点击行可展开详情。</p>
    <p v-if="runFilter && total" class="xlinks">
      <a href="#" @click.prevent="navigate(`/runs/${runFilter}`)">← 运行详情</a>
      <a href="#" @click.prevent="navigate(`/runs/${runFilter}/diagnosis`)">该 Run 诊断</a>
    </p>

    <StateBlock
      :loading="records.loading.value"
      :error="records.error.value"
      :empty="!total"
      empty-hint="尚无证据记录。先提交一次 Run，执行会产生截图与 trace 证据。"
      :retry="records.reload"
    >
      <div class="table-wrap"><table>
        <thead><tr><th>evidence_id</th><th>类型</th><th>层级</th><th>来源</th><th>run</th><th>完整性</th></tr></thead>
        <tbody>
          <template v-for="r in rows" :key="r.evidence_id">
            <tr
              class="clickable"
              tabindex="0"
              :aria-expanded="expanded === r.evidence_id"
              @click="toggleRow(r.evidence_id)"
              @keydown.enter.prevent="toggleRow(r.evidence_id)"
            >
              <td class="mono">{{ r.evidence_id }}</td>
              <td>{{ r.evidence_type }}</td>
              <td>
                <span class="badge" :class="r.raw ? 'raw' : 'derived'">{{ r.raw ? 'raw' : 'derived' }}</span>
              </td>
              <td>{{ r.source }}</td>
              <td class="mono small muted">{{ r.run_id }}</td>
              <td class="mono small">{{ (r.integrity_check || '—').slice(0, 22) }}…</td>
            </tr>
            <tr v-if="expanded === r.evidence_id" class="detail">
              <td colspan="6">
                <p><b>摘要</b> {{ r.summary }}</p>
                <p><b>完整性</b> <span class="mono">{{ r.integrity_check || '未记录' }}</span></p>
                <p><b>派生自</b> {{ r.derived_from.length ? r.derived_from.join(', ') : '—' }}</p>
                <p><b>保留期</b> {{ r.retention }} · <b>脱敏</b> {{ r.redacted ? '是' : '否' }}</p>
                <pre class="payload">{{ JSON.stringify(r.payload, null, 2) }}</pre>
              </td>
            </tr>
          </template>
        </tbody>
      </table></div>
      <Pagination v-model:page="page" :page-size="pageSize" :total="total" />
    </StateBlock>

    <h2>序列缺口检测</h2>
    <StateBlock
      :loading="gaps.loading.value"
      :error="gaps.error.value"
      :empty="!gaps.data.value?.length"
      empty-hint="未检测到序号缺口，证据序列完整。"
      :retry="gaps.reload"
    >
      <div class="table-wrap"><table>
        <thead><tr><th>producer_instance_id</th><th>缺失序号</th><th>状态</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="g in gaps.data.value ?? []" :key="g.producer_instance_id">
            <td class="mono">{{ g.producer_instance_id }}</td>
            <td class="mono">{{ g.missing_sequences.join(', ') }}</td>
            <td>{{ g.status }}</td>
            <td class="small muted">{{ g.note }}</td>
          </tr>
        </tbody>
      </table></div>
    </StateBlock>
  </section>
</template>

<style scoped>
.xlinks {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 13px;
  margin: 0 0 12px;
}
.clickable {
  cursor: pointer;
}
.clickable:hover {
  background: var(--tg-accent-bg);
}
.badge {
  font-size: 12px;
  border-radius: 9px;
  padding: 0 7px;
  border: 1px solid var(--tg-border);
}
.badge.raw {
  color: var(--tg-accent);
}
.badge.derived {
  color: var(--tg-warn);
}
.detail {
  background: var(--tg-panel-2);
}
.detail p {
  font-size: 12px;
  margin: 3px 0;
}
.payload {
  font-size: 12px;
  margin: 6px 0 0;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--tg-muted);
}
</style>
