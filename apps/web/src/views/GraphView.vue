<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import { messageOf, useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import Pagination from '../components/Pagination.vue'
import PageHeader from '../components/PageHeader.vue'
import type { GraphEdge, GraphNode } from '../api/types'

const nodes = useAsync(() => api.graphNodes())
const edges = useAsync(() => api.graphEdges())

// ---- SVG layout: deterministic circular placement, grouped by type ----
const TYPE_COLORS: Record<string, string> = {
  event: '#185fa5',
  evidence: '#5b4ea6',
  run: '#1d7a3d',
  task: '#8a5a00',
  case: '#0f766e',
  hypothesis: '#b3261e',
}
function colorOf(type: string): string {
  return TYPE_COLORS[type] ?? '#6f6d69'
}

const positioned = computed(() => {
  const list = [...(nodes.data.value ?? [])].sort(
    (a, b) => a.node_type.localeCompare(b.node_type) || a.node_id.localeCompare(b.node_id),
  )
  const n = list.length
  const cx = 320
  const cy = 300
  const R = Math.min(250, 90 + n * 9)
  return list.map((node, i) => {
    const angle = (2 * Math.PI * i) / Math.max(n, 1) - Math.PI / 2
    return {
      node,
      x: cx + R * Math.cos(angle),
      y: cy + R * Math.sin(angle),
      color: colorOf(node.node_type),
    }
  })
})

const posIndex = computed(() => {
  const map = new Map<string, { x: number; y: number; color: string }>()
  for (const p of positioned.value) map.set(p.node.node_id, p)
  return map
})

const svgEdges = computed(() => {
  const out: { edge: GraphEdge; x1: number; y1: number; x2: number; y2: number; selected: boolean }[] = []
  for (const edge of edges.data.value ?? []) {
    const a = posIndex.value.get(edge.src)
    const b = posIndex.value.get(edge.dst)
    if (!a || !b) continue
    out.push({
      edge,
      x1: a.x,
      y1: a.y,
      x2: b.x,
      y2: b.y,
      selected: selected.value === edge.src || selected.value === edge.dst,
    })
  }
  return out
})

const byRelation = computed(() => {
  const counts: Record<string, number> = {}
  for (const e of edges.data.value ?? []) counts[e.relation] = (counts[e.relation] ?? 0) + 1
  return Object.entries(counts).sort((a, b) => b[1] - a[1])
})

const legendTypes = computed(() => {
  const types = new Set((nodes.data.value ?? []).map((n) => n.node_type))
  return [...types].sort()
})

// ---- selection & subgraph ----
const selected = ref('')
const sub = ref<{ root: string; nodes: GraphNode[]; edges: GraphEdge[] } | null>(null)
const subLoading = ref(false)
const subError = ref('')
const subDepth = ref(2)

function selectNode(id: string): void {
  selected.value = id
  void loadSubgraph(id)
}

async function loadSubgraph(id: string): Promise<void> {
  if (!id.trim()) return
  subLoading.value = true
  subError.value = ''
  try {
    sub.value = (await api.subgraph(id, subDepth.value)) as {
      root: string
      nodes: GraphNode[]
      edges: GraphEdge[]
    }
  } catch (err) {
    subError.value = messageOf(err)
    sub.value = null
  } finally {
    subLoading.value = false
  }
}

const selectedNode = computed(
  () => (nodes.data.value ?? []).find((n) => n.node_id === selected.value) ?? null,
)

// ---- edge table pagination ----
const page = ref(1)
const pageSize = 20
const pageEdges = computed(() => {
  const list = edges.data.value ?? []
  return list.slice((page.value - 1) * pageSize, page.value * pageSize)
})
</script>

<template>
  <section class="page">
    <PageHeader
      title="证据图"
      subtitle="Event ↔ Evidence 关联关系。边只来自已验证的上下文引用，未知引用不产生边。"
    >
      <button class="btn" type="button" @click="nodes.reload(); edges.reload()">刷新</button>
    </PageHeader>

    <StateBlock
      :loading="nodes.loading.value || edges.loading.value"
      :error="nodes.error.value || edges.error.value"
      :empty="!nodes.data.value?.length"
      empty-hint="图中还没有节点。执行一次 Run 并产生证据后，关联图会自动构建。"
      :retry="nodes.reload"
    >
      <div class="split">
        <div class="panel">
          <div class="legend">
            <span v-for="t in legendTypes" :key="t" class="legend-item small">
              <span class="dot" :style="{ background: colorOf(t) }" />{{ t }}
            </span>
          </div>
          <svg
            class="graph"
            viewBox="0 0 640 600"
            role="img"
            aria-label="证据关联图"
          >
            <g v-for="e in svgEdges" :key="e.edge.edge_id">
              <line
                :x1="e.x1" :y1="e.y1" :x2="e.x2" :y2="e.y2"
                :class="['edge', { selected: e.selected }]"
              />
            </g>
            <g v-for="p in positioned" :key="p.node.node_id">
              <circle
                :cx="p.x" :cy="p.y" r="7"
                :fill="p.color"
                :class="['node', { selected: selected === p.node.node_id }]"
                :tabindex="0"
                role="button"
                :aria-label="p.node.node_id"
                @click="selectNode(p.node.node_id)"
                @keydown.enter="selectNode(p.node.node_id)"
              />
              <text :x="p.x + 10" :y="p.y + 3" class="node-label">
                {{ p.node.node_id.length > 16 ? `${p.node.node_id.slice(0, 16)}…` : p.node.node_id }}
              </text>
            </g>
          </svg>
          <p class="hint">点击节点查看其子图与属性；边为关联规则产生的有向关系。</p>
        </div>

        <div class="panel">
          <h2>节点详情</h2>
          <p v-if="!selectedNode" class="muted small">尚未选择节点。点击左侧图中节点。</p>
          <div v-else class="kv-wrap">
            <ul class="kv">
              <li><span>node_id</span><span class="mono">{{ selectedNode.node_id }}</span></li>
              <li><span>type</span><span class="mono">{{ selectedNode.node_type }}</span></li>
              <li><span>independence_group</span><span class="mono">{{ selectedNode.independence_group || '—' }}</span></li>
              <li><span>source_ref</span><span class="mono small">{{ selectedNode.source_ref || '—' }}</span></li>
            </ul>
            <details v-if="selectedNode.properties && Object.keys(selectedNode.properties).length">
              <summary class="small">properties</summary>
              <pre class="payload">{{ JSON.stringify(selectedNode.properties, null, 2) }}</pre>
            </details>
            <h3 class="sub-head small">子图（depth {{ subDepth }}）</h3>
            <div v-if="subLoading" class="muted small">子图查询中…</div>
            <p v-else-if="subError" class="error-text small">{{ subError }}</p>
            <p v-else-if="!sub" class="muted small">尚未加载。</p>
            <p v-else class="small">
              根节点 <span class="mono">{{ sub.root }}</span>：{{ sub.nodes.length }} 节点 ·
              {{ sub.edges.length }} 边
              <label class="depth-label small">
                深度
                <select v-model.number="subDepth" @change="loadSubgraph(selected)">
                  <option :value="1">1</option>
                  <option :value="2">2</option>
                  <option :value="3">3</option>
                  <option :value="4">4</option>
                </select>
              </label>
            </p>
          </div>

          <h2>边明细（{{ (edges.data.value ?? []).length }}）</h2>
          <table>
            <thead><tr><th>src</th><th>relation</th><th>dst</th><th>rule</th></tr></thead>
            <tbody>
              <tr v-for="e in pageEdges" :key="e.edge_id" :class="{ hi: selected === e.src || selected === e.dst }">
                <td class="mono small">{{ e.src }}</td>
                <td class="mono small">{{ e.relation }}</td>
                <td class="mono small">{{ e.dst }}</td>
                <td class="mono small">{{ e.rule_id }}@v{{ e.rule_version }}</td>
              </tr>
            </tbody>
          </table>
          <Pagination v-model:page="page" :page-size="pageSize" :total="(edges.data.value ?? []).length" />

          <h2>关系分布</h2>
          <ul class="kv">
            <li v-for="[k, n] in byRelation" :key="k"><span>{{ k }}</span><b>{{ n }}</b></li>
          </ul>
        </div>
      </div>
    </StateBlock>
  </section>
</template>

<style scoped>
.panel {
  background: var(--tg-panel);
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 14px 16px;
}
.panel h2 {
  margin-top: 0;
}
.legend {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.graph {
  width: 100%;
  height: auto;
  border: 1px solid var(--tg-border);
  border-radius: 6px;
  background: var(--tg-bg);
}
.edge {
  stroke: var(--tg-border-strong);
  stroke-width: 1;
  opacity: 0.6;
}
.edge.selected {
  stroke: var(--tg-accent);
  stroke-width: 2;
  opacity: 1;
}
.node {
  cursor: pointer;
  stroke: var(--tg-panel);
  stroke-width: 2;
}
.node.selected {
  stroke: var(--tg-accent);
  stroke-width: 3;
}
.node:focus-visible {
  outline: 2px solid var(--tg-accent);
}
.node-label {
  font-size: 10px;
  fill: var(--tg-muted);
  pointer-events: none;
}
.kv-wrap {
  margin-bottom: 16px;
}
.sub-head {
  margin-top: 14px;
}
.depth-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 10px;
}
.payload {
  background: var(--tg-panel-2);
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
tr.hi td {
  background: var(--tg-accent-bg);
}
</style>
