<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ROUTES, isActive, navigate, route, routeQuery, segments } from './router'
import { api, subscribeEventsSafe } from './api/client'
import type { EventRow } from './api/types'
import AppIcon from './components/AppIcon.vue'
import OverviewView from './views/OverviewView.vue'
import IntentsView from './views/IntentsView.vue'
import ExplorerView from './views/ExplorerView.vue'
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
    case 'explorer':
      return { comp: ExplorerView, props: {} }
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

// ---- navigation chrome ----
const rail = ref(false)
const drawerOpen = ref(false)

watch(
  () => route.value,
  () => {
    drawerOpen.value = false
  },
)

// ---- theme: explicit choice wins, otherwise follow the OS ----
type Theme = 'light' | 'dark' | 'system'
const theme = ref<Theme>((localStorage.getItem('tg-theme') as Theme | null) ?? 'system')
const systemDark = ref(false)
const isDark = computed(() => (theme.value === 'system' ? systemDark.value : theme.value === 'dark'))

let mq: MediaQueryList | null = null
function onScheme(e: MediaQueryListEvent): void {
  systemDark.value = e.matches
}

function applyTheme(): void {
  const root = document.documentElement
  if (theme.value === 'system') delete root.dataset.theme
  else root.dataset.theme = theme.value
  localStorage.setItem('tg-theme', theme.value)
}

function toggleTheme(): void {
  theme.value = isDark.value ? 'light' : 'dark'
  applyTheme()
}

// ---- backend reachability + SSE liveness ----
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
  mq = window.matchMedia('(prefers-color-scheme: dark)')
  systemDark.value = mq.matches
  mq.addEventListener('change', onScheme)
  applyTheme()
  checkBackend()
  unsubscribe = subscribeSse()
})

function subscribeSse(): () => void {
  try {
    return subscribeEventsSafe(
      (row: EventRow) => {
        void row
        sseLive.value = true
      },
      () => {
        sseLive.value = false
      },
    )
  } catch {
    sseLive.value = false
    return () => undefined
  }
}

onUnmounted(() => {
  unsubscribe?.()
  mq?.removeEventListener('change', onScheme)
})

const query = computed(() => routeQuery.value)
const healthLabel = computed(() => {
  if (backendUp.value === null) return '连接中…'
  if (!backendUp.value) return '后端不可达'
  return sseLive.value ? '后端正常 · 实时流已连接' : '后端正常 · 实时流未连接'
})
</script>

<template>
  <div class="app" :class="{ rail, 'drawer-open': drawerOpen }">
    <div v-if="drawerOpen" class="scrim" @click="drawerOpen = false" />

    <aside class="side" :aria-label="'主导航'">
      <button class="brand" type="button" @click="navigate('/overview')">
        <span class="logo">
          <AppIcon name="shield" :size="18" :stroke-width="1.9" />
        </span>
        <span class="brand-text">
          <strong>TraceGuard</strong>
          <span class="brand-sub">质量智能平台</span>
        </span>
      </button>

      <nav>
        <div v-for="[group, items] in groups" :key="group" class="nav-group">
          <p class="group-label">{{ group }}</p>
          <a
            v-for="item in items"
            :key="item.path"
            href="#"
            class="nav-item"
            :class="{ active: isActive(item.path) }"
            :title="rail ? item.label : item.desc"
            @click.prevent="navigate(item.path)"
          >
            <span class="nav-icon"><AppIcon :name="item.icon" :size="16" /></span>
            <span class="nav-label">{{ item.label }}</span>
          </a>
        </div>
      </nav>

      <div class="side-foot">
        <button class="theme-btn" type="button" :title="isDark ? '切换为浅色' : '切换为深色'" @click="toggleTheme">
          <AppIcon :name="isDark ? 'sun' : 'moon'" :size="15" />
          <span class="nav-label">{{ isDark ? '浅色模式' : '深色模式' }}</span>
        </button>
        <p class="foot-note nav-label">基线 v1.1 · 契约 draft</p>
      </div>
    </aside>

    <div class="content">
      <header class="topbar">
        <button
          class="icon-btn only-narrow"
          type="button"
          aria-label="打开导航"
          @click="drawerOpen = true"
        >
          <AppIcon name="menu" :size="17" />
        </button>
        <button
          class="icon-btn only-wide"
          type="button"
          :aria-label="rail ? '展开导航' : '收起导航'"
          @click="rail = !rail"
        >
          <AppIcon name="menu" :size="17" />
        </button>

        <nav class="crumbs" aria-label="面包屑">
          <a href="#" class="crumb-home" @click.prevent="navigate('/overview')">首页</a>
          <template v-for="(c, i) in crumbs" :key="c.path">
            <span class="sep" aria-hidden="true">/</span>
            <span v-if="i === crumbs.length - 1" class="cur">{{ c.label }}</span>
            <a v-else href="#" @click.prevent="navigate(c.path)">{{ c.label }}</a>
          </template>
        </nav>

        <div class="topbar-right">
          <a
            class="health"
            href="#"
            :title="`${healthLabel} · 实时事件流: ${sseLive ? '已连接' : '未连接'}`"
            @click.prevent="navigate('/system')"
          >
            <span class="dot" :class="{ up: backendUp, down: backendUp === false }" />
            <span class="dot stream" :class="{ up: sseLive }" />
            <span class="health-text">{{ healthLabel }}</span>
          </a>
        </div>
      </header>

      <main class="main" :key="route + (query.get('task') ?? '') + (query.get('run') ?? '')">
        <component :is="view.comp" v-bind="view.props" />
      </main>
    </div>
  </div>
</template>

<style scoped>
.app {
  display: grid;
  grid-template-columns: var(--tg-side-w) 1fr;
  min-height: 100vh;
  background: var(--tg-bg);
}

.app.rail {
  grid-template-columns: var(--tg-side-w-rail) 1fr;
}

/* ------------------------------------------------------------- sidebar -- */
.side {
  position: sticky;
  top: 0;
  height: 100vh;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 10px 12px;
  background: var(--tg-panel);
  border-right: 1px solid var(--tg-border);
  overflow-y: auto;
  overflow-x: hidden;
  z-index: 30;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 6px 12px;
  background: none;
  border: 0;
  cursor: pointer;
  color: inherit;
  text-align: left;
  font: inherit;
}

.logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 9px;
  background: linear-gradient(150deg, var(--tg-accent), var(--tg-c6));
  color: #fff;
  flex: 0 0 auto;
  box-shadow: var(--tg-shadow-sm);
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
  min-width: 0;
}

.brand-text strong {
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.brand-sub {
  font-size: 10.5px;
  color: var(--tg-muted);
  letter-spacing: 0.02em;
}

/* Scoped to the sidebar: a bare `nav` selector also matched the breadcrumb
   <nav>, stacking "首页 / 项目总览" into three lines that overflowed the topbar. */
.side nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1 1 auto;
}

.nav-group {
  margin-bottom: 8px;
}

.group-label {
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--tg-faint);
  margin: 6px 8px 4px;
  white-space: nowrap;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 6px 8px;
  border-radius: var(--tg-radius-sm);
  font-size: 13px;
  color: var(--tg-text-2);
  position: relative;
  white-space: nowrap;
  overflow: hidden;
  transition:
    background 0.12s ease,
    color 0.12s ease;
}

.nav-item:hover {
  background: var(--tg-panel-2);
  color: var(--tg-text);
  text-decoration: none;
}

.nav-item.active {
  background: var(--tg-accent-bg);
  color: var(--tg-accent-text);
  font-weight: 550;
}

.nav-item.active .nav-icon {
  color: var(--tg-accent);
}

.nav-icon {
  display: inline-flex;
  color: var(--tg-muted);
  flex: 0 0 auto;
}

.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
}

.side-foot {
  margin-top: auto;
  padding-top: 10px;
  border-top: 1px solid var(--tg-border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.theme-btn {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 6px 8px;
  border: 0;
  background: none;
  color: var(--tg-muted);
  font: inherit;
  font-size: 12.5px;
  border-radius: var(--tg-radius-sm);
  cursor: pointer;
}

.theme-btn:hover {
  background: var(--tg-panel-2);
  color: var(--tg-text);
}

.foot-note {
  font-size: 10.5px;
  color: var(--tg-faint);
  padding: 0 8px;
}

/* Rail mode: icons only, tooltips carry the labels. */
.app.rail .brand-text,
.app.rail .nav-label,
.app.rail .group-label {
  display: none;
}

.app.rail .nav-item {
  justify-content: center;
  padding: 7px 0;
}

.app.rail .side nav {
  gap: 4px;
}

/* -------------------------------------------------------------- topbar -- */
.content {
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--tg-topbar-h);
  padding: 0 20px;
  background: var(--tg-panel);
  border-bottom: 1px solid var(--tg-border);
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--tg-muted);
  border-radius: var(--tg-radius-sm);
  cursor: pointer;
  flex: 0 0 auto;
}

.icon-btn:hover {
  background: var(--tg-panel-2);
  color: var(--tg-text);
}

.only-narrow {
  display: none;
}

.crumbs {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--tg-muted);
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  height: 100%;
}

.crumbs .sep {
  color: var(--tg-faint);
}

.crumbs .cur {
  color: var(--tg-text);
  font-weight: 550;
  overflow: hidden;
  text-overflow: ellipsis;
}

.crumb-home {
  color: var(--tg-muted);
}

.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
}

.health {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 3px 9px 3px 8px;
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius-pill);
  background: var(--tg-panel-2);
  color: var(--tg-muted);
  font-size: 11.5px;
  max-width: 340px;
}

.health:hover {
  text-decoration: none;
  border-color: var(--tg-border-strong);
  color: var(--tg-text-2);
}

.health-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--tg-faint);
  flex: 0 0 auto;
}

.dot.up {
  background: var(--tg-success);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--tg-success) 18%, transparent);
}

.dot.down {
  background: var(--tg-danger);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--tg-danger) 18%, transparent);
}

.dot.stream.up {
  background: var(--tg-c2);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--tg-c2) 18%, transparent);
}

/* ---------------------------------------------------------------- main -- */
.main {
  padding: 20px 24px 56px;
  min-width: 0;
  flex: 1 1 auto;
}

.scrim {
  display: none;
}

/* --------------------------------------------------------- responsive -- */
@media (max-width: 1080px) {
  .health-text {
    display: none;
  }
}

@media (max-width: 860px) {
  .app,
  .app.rail {
    grid-template-columns: 1fr;
  }

  .only-wide {
    display: none;
  }

  .only-narrow {
    display: inline-flex;
  }

  .side {
    position: fixed;
    inset: 0 auto 0 0;
    width: min(var(--tg-side-w), 84vw);
    transform: translateX(-102%);
    transition: transform 0.2s ease;
    box-shadow: var(--tg-shadow-lg);
  }

  .app.drawer-open .side {
    transform: translateX(0);
  }

  .app.drawer-open .scrim {
    display: block;
    position: fixed;
    inset: 0;
    background: var(--tg-overlay);
    z-index: 25;
  }

  /* Labels are hidden by rail mode on desktop only; the drawer always shows them. */
  .app.rail .brand-text,
  .app.rail .nav-label,
  .app.rail .group-label {
    display: revert;
  }

  .app.rail .nav-item {
    justify-content: flex-start;
    padding: 6px 8px;
  }

  .main {
    padding: 16px 14px 48px;
  }

  .topbar {
    padding: 0 12px;
  }
}
</style>
