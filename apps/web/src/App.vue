<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  ROUTES,
  isActive,
  navigate,
  route,
  routeQuery,
  segments,
} from './router'
import { api, subscribeEventsSafe } from './api/client'
import type { EventRow } from './api/types'
import OverviewView from './views/OverviewView.vue'
import IntentsView from './views/IntentsView.vue'
import CandidatesView from './views/CandidatesView.vue'
import CasesView from './views/CasesView.vue'
import CaseDetailView from './views/CaseDetailView.vue'
import CaseVersionsView from './views/CaseVersionsView.vue'
import TasksView from './views/TasksView.vue'
import RunsView from './views/RunsView.vue'
import RunDetailView from './views/RunDetailView.vue'
import StepsView from './views/StepsView.vue'
import AssertionsView from './views/AssertionsView.vue'
import EventsView from './views/EventsView.vue'
import EvidenceView from './views/EvidenceView.vue'
import GraphView from './views/GraphView.vue'
import DiagnosisView from './views/DiagnosisView.vue'
import ProbesView from './views/ProbesView.vue'
import RepairsView from './views/RepairsView.vue'
import BenchmarkView from './views/BenchmarkView.vue'
import SystemView from './views/SystemView.vue'

// Single place that maps a URL to a page. Unknown routes fall back to overview
// instead of rendering a blank screen.
const view = computed(() => {
  const seg = segments()
  const [head, id, sub] = seg
  switch (head) {
    case 'intents':
      return { comp: IntentsView, props: {} }
    case 'candidates':
      return { comp: CandidatesView, props: {} }
    case 'cases':
      if (!id) return { comp: CasesView, props: {} }
      if (sub === 'versions') return { comp: CaseVersionsView, props: { caseId: id } }
      return { comp: CaseDetailView, props: { caseId: id } }
    case 'tasks':
      return { comp: TasksView, props: {} }
    case 'runs':
      if (!id) return { comp: RunsView, props: {} }
      if (sub === 'steps') return { comp: StepsView, props: { runId: id } }
      if (sub === 'assertions') return { comp: AssertionsView, props: { runId: id } }
      if (sub === 'diagnosis') return { comp: DiagnosisView, props: { runId: id } }
      return { comp: RunDetailView, props: { runId: id } }
    case 'events':
      return { comp: EventsView, props: {} }
    case 'evidence':
      return { comp: EvidenceView, props: {} }
    case 'graph':
      return { comp: GraphView, props: {} }
    case 'diagnosis':
      return { comp: DiagnosisView, props: {} }
    case 'probes':
      return { comp: ProbesView, props: {} }
    case 'repairs':
      return { comp: RepairsView, props: {} }
    case 'benchmark':
      return { comp: BenchmarkView, props: {} }
    case 'system':
      return { comp: SystemView, props: {} }
    default:
      return { comp: OverviewView, props: {} }
  }
})

const groups = computed(() => {
  const map = new Map<string, typeof ROUTES>()
  for (const r of ROUTES) {
    const list = map.get(r.group) ?? []
    list.push(r)
    map.set(r.group, list)
  }
  return [...map.entries()]
})

// ---- breadcrumb (path segments → clickable crumbs) ----
const crumbs = computed(() => {
  const seg = segments()
  const out: { label: string; path: string }[] = []
  let acc = ''
  for (const s of seg) {
    acc += `/${s}`
    const known = ROUTES.find((r) => r.path === acc)
    out.push({ label: known?.label ?? s, path: acc })
  }
  return out
})

// ---- sidebar collapsed state (narrow screens) ----
const collapsed = ref(false)

// ---- backend reachability dot in the topbar (REST check, not SSE) ----
const backendUp = ref<boolean | null>(null)
const sseLive = ref(false)
let unsubscribe: (() => void) | null = null

function checkBackend(): void {
  api
    .overview()
    .then(() => {
      backendUp.value = true
    })
    .catch(() => {
      backendUp.value = false
    })
}

onMounted(() => {
  checkBackend()
  unsubscribe = subscribeSse()
})

function subscribeSse(): () => void {
  try {
    return subscribeEventsSafe((row: EventRow) => {
      void row
      sseLive.value = true
    }, () => {
      sseLive.value = false
    })
  } catch {
    sseLive.value = false
    return () => undefined
  }
}

onUnmounted(() => {
  unsubscribe?.()
})

const query = computed(() => routeQuery.value)
</script>

<template>
  <div class="shell" :class="{ collapsed }">
    <aside class="side">
      <div class="brand" @click="navigate('/overview')">
        <span class="logo" aria-hidden="true">
          <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M2 8.5 6 12.5 14 3.5" />
          </svg>
        </span>
        <div class="brand-text">
          <strong>TraceGuard</strong>
          <span class="muted small">质量智能工作台</span>
        </div>
      </div>
      <nav aria-label="主导航">
        <div v-for="[group, items] in groups" :key="group" class="group">
          <p class="group-label">{{ group }}</p>
          <a
            v-for="item in items"
            :key="item.path"
            href="#"
            class="nav-item"
            :class="{ active: isActive(item.path) }"
            @click.prevent="navigate(item.path)"
          >
            <span class="nav-label">{{ item.label }}</span>
          </a>
        </div>
      </nav>
      <p class="foot muted small">架构基线 v1.1 · 契约 draft</p>
    </aside>

    <div class="content">
      <header class="topbar">
        <button
          class="btn ghost toggle"
          type="button"
          :aria-label="collapsed ? '展开导航' : '折叠导航'"
          @click="collapsed = !collapsed"
        >
          ☰
        </button>
        <nav class="crumbs" aria-label="面包屑">
          <a href="#" @click.prevent="navigate('/overview')">首页</a>
          <template v-for="(c, i) in crumbs" :key="c.path">
            <span class="sep" aria-hidden="true">/</span>
            <span v-if="i === crumbs.length - 1" class="cur">{{ c.label }}</span>
            <a v-else href="#" @click.prevent="navigate(c.path)">{{ c.label }}</a>
          </template>
        </nav>
        <div class="status" :title="`后端: ${backendUp ? '正常' : '不可达'} · 实时流: ${sseLive ? '已连接' : '未连接'}`">
          <span class="dot" :class="{ up: backendUp, down: backendUp === false }" />
          <span class="dot" :class="{ up: sseLive }" />
        </div>
      </header>
      <main class="main" :key="route + (query.get('task') ?? '') + (query.get('run') ?? '')">
        <component :is="view.comp" v-bind="view.props" />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: 208px 1fr;
  min-height: 100vh;
}
.shell.collapsed {
  grid-template-columns: 56px 1fr;
}
.shell.collapsed .brand-text,
.shell.collapsed .nav-label,
.shell.collapsed .group-label,
.shell.collapsed .foot {
  display: none;
}
.side {
  border-right: 1px solid var(--tg-border);
  background: var(--tg-panel);
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 18px;
  padding: 0 6px;
  cursor: pointer;
}
.logo {
  color: var(--tg-accent);
  display: inline-flex;
}
.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}
.brand strong {
  font-weight: 600;
  font-size: 15px;
}
.group {
  margin-bottom: 14px;
}
.group-label {
  font-size: 11px;
  color: var(--tg-muted);
  margin: 0 6px 4px;
  letter-spacing: 0.05em;
}
.nav-item {
  display: block;
  padding: 5px 8px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--tg-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.nav-item:hover {
  background: var(--tg-panel-2);
  text-decoration: none;
}
.nav-item.active {
  background: var(--tg-accent-bg);
  color: var(--tg-accent);
  font-weight: 500;
}
.foot {
  margin-top: auto;
  padding: 0 6px;
}
.content {
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 28px;
  border-bottom: 1px solid var(--tg-border);
  background: var(--tg-panel);
  position: sticky;
  top: 0;
  z-index: 10;
}
.toggle {
  padding: 2px 10px;
}
.crumbs {
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
}
.crumbs .sep {
  color: var(--tg-muted);
}
.crumbs .cur {
  color: var(--tg-text);
  font-weight: 500;
}
.status {
  margin-left: auto;
  display: flex;
  gap: 6px;
  align-items: center;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--tg-muted);
  opacity: 0.5;
}
.dot.up {
  background: var(--tg-success);
  opacity: 1;
}
.dot.down {
  background: var(--tg-danger);
  opacity: 1;
}
.main {
  padding: 20px 28px 48px;
  min-width: 0;
}
@media (max-width: 1024px) {
  .shell,
  .shell.collapsed {
    grid-template-columns: 56px 1fr;
  }
  .brand-text,
  .nav-label,
  .group-label,
  .foot {
    display: none;
  }
}
</style>
