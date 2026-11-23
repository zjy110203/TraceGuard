<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import { messageOf, useAsync } from '../composables/useAsync'
import Pagination from '../components/Pagination.vue'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'
import AppIcon from '../components/AppIcon.vue'
import type { GraphEdge, GraphNode, Subgraph } from '../api/types'

const nodes = useAsync(() => api.graphNodes())
const edges = useAsync(() => api.graphEdges())

// ---- palette: CSS vars so the graph follows the active theme ----
const TYPE_COLORS: Record<string, string> = {
  event: 'var(--tg-c1)',
  evidence: 'var(--tg-c6)',
  run: 'var(--tg-c3)',
  task: 'var(--tg-c4)',
  case: 'var(--tg-c2)',
  hypothesis: 'var(--tg-c5)',
}
function colorOf(type: string): string {
  return TYPE_COLORS[type] ?? 'var(--tg-neutral)'
}

// ---- filters ----
const typeFilter = ref<string[]>([]) // empty = show all types
const search = ref('')

function toggleType(type: string): void {
  const set = new Set(typeFilter.value)
  if (set.has(type)) set.delete(type)
  else set.add(type)
  typeFilter.value = [...set]
}

const allTypes = computed(() => {
  const types = new Set((nodes.data.value ?? []).map((n) => n.node_type))
  return [...types].sort()
})

const visibleNodes = computed(() => {
  const kw = search.value.trim().toLowerCase()
  const types = typeFilter.value
  return (nodes.data.value ?? []).filter((n) => {
    if (types.length && !types.includes(n.node_type)) return false
    if (kw && !n.node_id.toLowerCase().includes(kw)) return false
    return true
  })
})

// ---- viewport: zoom + pan ----
const zoom = ref(1)
const pan = ref({ x: 0, y: 0 })
const dragging = ref(false)
// Pointer capture on the SVG swallows the click that follows a press, so a
// press that starts on a node must never begin a pan — otherwise selecting a
// node is impossible. Panning also waits for a small threshold so a sloppy
// click on empty canvas does not shift the whole graph.
const dragState = { armed: false, moved: false, startX: 0, startY: 0, originX: 0, originY: 0 }

function onWheel(e: WheelEvent): void {
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12
  zoom.value = Math.min(3, Math.max(0.4, Number((zoom.value * factor).toFixed(3))))
}

function onDown(e: PointerEvent): void {
  const target = e.target as Element | null
  if (target?.closest('[data-node-id]')) {
    dragState.armed = false // node press = select, not pan
    return
  }
  dragState.armed = true
  dragState.moved = false
  dragState.startX = e.clientX
  dragState.startY = e.clientY
  dragState.originX = pan.value.x
  dragState.originY = pan.value.y
}

function onMove(e: PointerEvent): void {
  if (!dragState.armed) return
  const dx = e.clientX - dragState.startX
  const dy = e.clientY - dragState.startY
  if (!dragState.moved && Math.hypot(dx, dy) < 4) return
  dragState.moved = true
  dragging.value = true
  pan.value = { x: dragState.originX + dx, y: dragState.originY + dy }
}

function onUp(): void {
  dragState.armed = false
  dragState.moved = false
  dragging.value = false
}

/** Ring labels: keep the distinguishing tail, drop the shared prefix.
 *  Every node id here starts with "run_2026…"/"evd_…", so a plain prefix
 *  truncation produced labels that were both wider than the ring spacing and
 *  identical to each other. */
function shortNode(id: string): string {
  return id.length <= 16 ? id : `${id.slice(0, 5)}…${id.slice(-10)}`
}

function resetView(): void {
  zoom.value = 1
  pan.value = { x: 0, y: 0 }
}

const viewTransform = computed(
  () => `translate(${pan.value.x.toFixed(1)} ${pan.value.y.toFixed(1)}) scale(${zoom.value})`,
)

// ---- SVG layout: deterministic circular placement over the *visible* set ----
const positioned = computed(() => {
  const list = [...visibleNodes.value].sort(
    (a, b) => a.node_type.localeCompare(b.node_type) || a.node_id.localeCompare(b.node_id),
  )
  const n = list.length
  // 760 wide (not 640) so labels anchored outside the ring — which is what
  // keeps them from stacking — still have room on both sides.
  const cx = 380
  const cy = 300
  const R = Math.min(235, 90 + n * 9)
  return list.map((node, i) => {
    const angle = (2 * Math.PI * i) / Math.max(n, 1) - Math.PI / 2
    const cos = Math.cos(angle)
    return {
      node,
      x: cx + R * cos,
      y: cy + R * Math.sin(angle),
      color: colorOf(node.node_type),
      // Labels are pushed outward along the radius: with 20+ nodes on a ring,
      // a fixed right-side offset stacks them on top of each other.
      labelX: cx + (R + 12) * cos,
      labelY: cy + (R + 12) * Math.sin(angle) + 3,
      labelAnchor: cos >= -0.05 ? 'start' : 'end',
    }
  })
})

const posIndex = computed(() => {
  const map = new Map<string, { x: number; y: number; color: string }>()
  for (const p of positioned.value) map.set(p.node.node_id, p)
  return map
})

// ---- selection, adjacency, path ----
const selected = ref('')
const sub = ref<Subgraph | null>(null)
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
    sub.value = await api.subgraph(id, subDepth.value)
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

/** 1-hop neighbours of the selected node — used to dim everything unrelated. */
const neighbours = computed(() => {
  const set = new Set<string>()
  if (!selected.value) return set
  for (const e of edges.data.value ?? []) {
    if (e.src === selected.value) set.add(e.dst)
    if (e.dst === selected.value) set.add(e.src)
  }
  return set
})

// ---- shortest path between two nodes (BFS over the real edges) ----
const pathFrom = ref('')
const pathTo = ref('')

const pathInfo = computed(() => {
  const from = pathFrom.value
  const to = pathTo.value
  if (!from || !to || from === to) return null

  const adj = new Map<string, { to: string; edge: string }[]>()
  for (const e of edges.data.value ?? []) {
    if (!adj.has(e.src)) adj.set(e.src, [])
    if (!adj.has(e.dst)) adj.set(e.dst, [])
    adj.get(e.src)!.push({ to: e.dst, edge: e.edge_id })
  }

  const prev = new Map<string, { node: string; edge: string }>()
  const seen = new Set<string>([from])
  const queue: string[] = [from]
  while (queue.length) {
    const cur = queue.shift() as string
    if (cur === to) break
    for (const next of adj.get(cur) ?? []) {
      if (seen.has(next.to)) continue
      seen.add(next.to)
      prev.set(next.to, { node: cur, edge: next.edge })
      queue.push(next.to)
    }
  }

  if (!seen.has(to)) return { found: false, nodes: [] as string[], edges: new Set<string>() }

  const pathNodes: string[] = [to]
  const pathEdges = new Set<string>()
  let cur = to
  while (cur !== from) {
    const step = prev.get(cur)
    if (!step) break
    pathEdges.add(step.edge)
    pathNodes.push(step.node)
    cur = step.node
  }
  return { found: true, nodes: pathNodes, edges: pathEdges }
})

const pathNodes = computed(() => new Set(pathInfo.value?.nodes ?? []))

// ---- edge geometry ----
const svgEdges = computed(() => {
  const out: {
    edge: GraphEdge
    x1: number
    y1: number
    x2: number
    y2: number
    selected: boolean
    onPath: boolean
    dim: boolean
  }[] = []
  for (const edge of edges.data.value ?? []) {
    const a = posIndex.value.get(edge.src)
    const b = posIndex.value.get(edge.dst)
    if (!a || !b) continue
    const onPath = pathInfo.value?.edges.has(edge.edge_id) ?? false
    const touchesSelection = selected.value !== '' && (selected.value === edge.src || selected.value === edge.dst)
    out.push({
      edge,
      x1: a.x,
      y1: a.y,
      x2: b.x,
      y2: b.y,
      selected: touchesSelection,
      onPath,
      dim: Boolean(pathInfo.value) && !onPath,
    })
  }
  return out
})

const byRelation = computed(() => {
  const counts: Record<string, number> = {}
  for (const e of edges.data.value ?? []) counts[e.relation] = (counts[e.relation] ?? 0) + 1
  return Object.entries(counts).sort((a, b) => b[1] - a[1])
})

// ---- edge table pagination ----
const page = ref(1)
const pageSize = 20
const pageEdges = computed(() => {
  const list = edges.data.value ?? []
  return list.slice((page.value - 1) * pageSize, page.value * pageSize)
})

function loadAll(): void {
  void nodes.reload()
  void edges.reload()
}
</script>

<template>
  <section class="page">
    <PageHeader
      title="证据图谱"
      subtitle="Event ↔ Evidence 关联关系。边只来自已验证的上下文引用，未知引用不产生边。"
    >
      <button class="btn" type="button" @click="loadAll">
        <AppIcon name="refresh" :size="14" /> 刷新
      </button>
    </PageHeader>

    <EmptyState
      v-if="!nodes.loading.value && !nodes.error.value && !nodes.data.value?.length"
      icon="graph"
      title="图中还没有节点"
      hint="执行一次 Run 并产生证据后，关联图会自动构建。图谱中的节点与边全部来自真实数据，不会凭空生成。"
    >
      <button class="btn sm" type="button" @click="loadAll">重新加载</button>
    </EmptyState>

    <div v-else-if="nodes.error.value" class="card">
      <p class="error-text">{{ nodes.error.value }}</p>
      <button class="btn sm" type="button" @click="loadAll">重试</button>
    </div>

    <p v-else-if="nodes.loading.value" class="muted small">加载图谱…</p>

    <div v-else class="split graph-split">
      <div class="card graph-card">
        <div class="graph-toolbar">
          <label class="search-box">
            <AppIcon name="search" :size="14" />
            <span class="sr-only">搜索节点</span>
            <input v-model="search" placeholder="搜索节点 ID" />
          </label>
          <div class="zoom-group">
            <button class="icon-btn" type="button" aria-label="放大" @click="zoom = Math.min(3, zoom * 1.2)">
              <AppIcon name="zoomIn" :size="15" />
            </button>
            <button class="icon-btn" type="button" aria-label="缩小" @click="zoom = Math.max(0.4, zoom / 1.2)">
              <AppIcon name="zoomOut" :size="15" />
            </button>
            <button class="btn sm" type="button" @click="resetView">重置</button>
          </div>
        </div>

        <div class="legend">
          <button
            v-for="t in allTypes"
            :key="t"
            type="button"
            class="legend-chip"
            :class="{ off: typeFilter.length > 0 && !typeFilter.includes(t), on: typeFilter.includes(t) }"
            :aria-pressed="typeFilter.includes(t)"
            @click="toggleType(t)"
          >
            <span class="dot" :style="{ background: colorOf(t) }" />{{ t }}
          </button>
          <span v-if="typeFilter.length" class="legend-reset small muted" @click="typeFilter = []">
            清除筛选
          </span>
        </div>

        <svg
          class="graph"
          :class="{ grabbing: dragging }"
          viewBox="0 0 760 620"
          role="img"
          aria-label="证据关联图，可缩放拖动"
          @wheel.prevent="onWheel"
          @pointerdown="onDown"
          @pointermove="onMove"
          @pointerup="onUp"
          @pointercancel="onUp"
          @pointerleave="onUp"
        >
          <g :transform="viewTransform">
            <line
              v-for="e in svgEdges"
              :key="e.edge.edge_id"
              :x1="e.x1"
              :y1="e.y1"
              :x2="e.x2"
              :y2="e.y2"
              :class="['edge', { selected: e.selected, onPath: e.onPath, dim: e.dim }]"
            />
            <g v-for="p in positioned" :key="p.node.node_id">
              <circle
                :cx="p.x"
                :cy="p.y"
                r="7"
                :fill="p.color"
                :data-node-id="p.node.node_id"
                :class="[
                  'node',
                  {
                    selected: selected === p.node.node_id,
                    onPath: pathNodes.has(p.node.node_id),
                    dim:
                      (pathInfo !== null && !pathNodes.has(p.node.node_id)) ||
                      (selected !== '' && pathInfo === null && !neighbours.has(p.node.node_id) && selected !== p.node.node_id),
                  },
                ]"
                :tabindex="0"
                role="button"
                :aria-label="p.node.node_id"
                @click.stop="selectNode(p.node.node_id)"
                @keydown.enter="selectNode(p.node.node_id)"
              />
              <text
                :x="p.labelX"
                :y="p.labelY"
                :text-anchor="p.labelAnchor"
                class="node-label"
                :title="p.node.node_id"
              >
                {{ shortNode(p.node.node_id) }}
              </text>
            </g>
          </g>
        </svg>

        <div class="path-bar">
          <AppIcon name="graph" :size="14" />
          <label class="sr-only" for="path-from">路径起点</label>
          <select id="path-from" v-model="pathFrom">
            <option value="">起点</option>
            <option v-for="n in visibleNodes" :key="n.node_id" :value="n.node_id">
              {{ n.node_id }}
            </option>
          </select>
          <AppIcon name="arrowRight" :size="13" />
          <label class="sr-only" for="path-to">路径终点</label>
          <select id="path-to" v-model="pathTo">
            <option value="">终点</option>
            <option v-for="n in visibleNodes" :key="n.node_id" :value="n.node_id">
              {{ n.node_id }}
            </option>
          </select>
          <span v-if="pathInfo" class="small" :class="pathInfo.found ? 'notice-text' : 'error-text'">
            {{ pathInfo.found ? `路径 ${pathInfo.nodes.length} 节点 / ${pathInfo.edges.size} 边` : '两点间无连通路径' }}
          </span>
          <button v-if="pathFrom || pathTo" class="btn sm ghost" type="button" @click="pathFrom = ''; pathTo = ''">
            清除
          </button>
        </div>
        <p class="hint graph-hint">
          滚轮缩放 · 拖动平移 · 点击节点查看子图；选择起点与终点高亮最短路径。当前显示
          {{ visibleNodes.length }} / {{ (nodes.data.value ?? []).length }} 个节点。
        </p>
      </div>

      <div class="card">
        <div class="card-head"><h2>节点详情</h2></div>
        <div class="card-body">
          <p v-if="!selectedNode" class="muted small">尚未选择节点。点击左侧图中节点。</p>
          <div v-else class="kv-wrap">
            <ul class="kv">
              <li><span>node_id</span><span class="mono">{{ selectedNode.node_id }}</span></li>
              <li><span>type</span><span class="mono">{{ selectedNode.node_type }}</span></li>
              <li><span>independence_group</span><span class="mono">{{ selectedNode.independence_group || '—' }}</span></li>
              <li><span>source_ref</span><span class="mono small">{{ selectedNode.source_ref || '—' }}</span></li>
              <li><span>邻接节点</span><span>{{ neighbours.size }}</span></li>
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
              根节点 <span class="mono">{{ sub.root }}</span>：{{ sub.node_total }} 节点 ·
              {{ sub.edge_total }} 边
              <span v-if="sub.truncated" class="pill plain warn">已截断，显示前 {{ sub.nodes.length }}</span>
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
          <ul class="edge-list">
            <li
              v-for="e in pageEdges"
              :key="e.edge_id"
              :class="{ hi: selected === e.src || selected === e.dst }"
            >
              <span class="pill plain neutral rel">{{ e.relation }}</span>
              <span class="edge-path mono" :title="`${e.src} → ${e.dst}`">
                {{ e.src }} <b>→</b> {{ e.dst }}
              </span>
              <span class="small muted rule mono">{{ e.rule_id }}</span>
            </li>
          </ul>
          <Pagination v-model:page="page" :page-size="pageSize" :total="(edges.data.value ?? []).length" />

          <h2>关系分布</h2>
          <ul class="kv">
            <li v-for="[k, n] in byRelation" :key="k"><span>{{ k }}</span><b>{{ n }}</b></li>
          </ul>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.card-head h2 {
  margin: 0;
}

.graph-card {
  padding: 14px 16px;
}

.graph-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.search-box {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--tg-muted);
  flex: 1 1 180px;
  min-width: 0;
}

.search-box input {
  flex: 1 1 auto;
  min-width: 0;
}

.zoom-group {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid var(--tg-border);
  background: var(--tg-panel);
  color: var(--tg-muted);
  border-radius: var(--tg-radius-sm);
  cursor: pointer;
}

.icon-btn:hover {
  color: var(--tg-text);
  border-color: var(--tg-border-strong);
}

.legend {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  align-items: center;
}

.legend-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font: inherit;
  font-size: 11.5px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--tg-border);
  background: var(--tg-panel);
  color: var(--tg-text-2);
  cursor: pointer;
}

.legend-chip:hover {
  border-color: var(--tg-border-strong);
}

.legend-chip.on {
  border-color: var(--tg-accent);
  color: var(--tg-accent-text);
  background: var(--tg-accent-bg);
}

.legend-chip.off {
  opacity: 0.45;
}

.legend-reset {
  cursor: pointer;
  text-decoration: underline;
}

.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
}

.graph {
  width: 100%;
  height: auto;
  touch-action: none;
  cursor: grab;
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius-sm);
  background: var(--tg-bg);
}

.graph.grabbing {
  cursor: grabbing;
}

.edge {
  stroke: var(--tg-border-strong);
  stroke-width: 1;
  opacity: 0.55;
  transition: opacity 0.15s ease;
}

.edge.selected {
  stroke: var(--tg-accent);
  stroke-width: 2;
  opacity: 1;
}

.edge.onPath {
  stroke: var(--tg-warn);
  stroke-width: 2.4;
  opacity: 1;
}

.edge.dim {
  opacity: 0.12;
}

.node {
  cursor: pointer;
  stroke: var(--tg-panel);
  stroke-width: 2;
  transition: opacity 0.15s ease;
}

.node.selected {
  stroke: var(--tg-accent);
  stroke-width: 3;
}

.node.onPath {
  stroke: var(--tg-warn);
  stroke-width: 3;
}

.node.dim {
  opacity: 0.22;
}

.node:focus-visible {
  outline: 2px solid var(--tg-accent);
}

.node-label {
  font-size: 10px;
  fill: var(--tg-muted);
  pointer-events: none;
  /* Halo so a label crossing an edge stays readable. */
  paint-order: stroke;
  stroke: var(--tg-panel);
  stroke-width: 3px;
  stroke-linejoin: round;
}

/* Edge rows instead of a 4-column table: the panel is ~400px wide and long
   node ids would either overflow it or squeeze relation/rule to "emitted…". */
.edge-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.edge-list li {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  border-radius: var(--tg-radius-sm);
  border: 1px solid transparent;
}

.edge-list li.hi {
  background: var(--tg-accent-bg);
  border-color: var(--tg-accent-border, var(--tg-border-strong));
}

.edge-list .rel {
  font-size: 10.5px;
  padding: 0 6px;
}

.edge-path {
  font-size: 11px;
  color: var(--tg-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.edge-list li.hi .edge-path {
  color: var(--tg-text);
}

.edge-list .rule {
  font-size: 10.5px;
  white-space: nowrap;
}

.path-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
  color: var(--tg-muted);
}

.path-bar select {
  max-width: 190px;
  min-width: 0;
}

.graph-hint {
  margin: 8px 0 0;
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

@media (max-width: 900px) {
  .path-bar select {
    max-width: 130px;
  }
}
@media (min-width: 1100px) {
  /* The graph needs the room; auto-fit gave it the same width as the panel. */
  .graph-split {
    grid-template-columns: minmax(0, 1.5fr) minmax(320px, 1fr);
  }
}
</style>
