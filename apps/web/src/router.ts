// Minimal hash router. No external dependency: the workbench needs segment
// routing and back/forward support, nothing more. Routes are declared in one
// place so a broken link is a compile-time-visible entry, not a silent blank page.
//
// Hash format: `#/runs?task=T1` — path and query are parsed separately so a
// view link like `/runs?task=x` no longer falls through to the overview page.

import { ref } from 'vue'

export interface RouteDef {
  path: string
  label: string
  group: string
}

export const ROUTES: RouteDef[] = [
  { path: '/overview', label: '项目工作台', group: '工作台' },
  { path: '/intents', label: 'TestIntent', group: '测试设计' },
  { path: '/candidates', label: 'Candidate 审核', group: '测试设计' },
  { path: '/cases', label: 'TestCase', group: '测试资产' },
  { path: '/tasks', label: 'Task 队列', group: '执行中心' },
  { path: '/runs', label: 'Run 列表', group: '执行中心' },
  { path: '/events', label: 'Event', group: '证据中心' },
  { path: '/evidence', label: 'Evidence', group: '证据中心' },
  { path: '/graph', label: '证据图', group: '证据中心' },
  { path: '/diagnosis', label: '诊断', group: '智能诊断' },
  { path: '/probes', label: 'Probe 审计', group: '智能诊断' },
  { path: '/repairs', label: '修复审核', group: '修复中心' },
  { path: '/benchmark', label: 'UI-FaultBench', group: '评测' },
  { path: '/system', label: '系统与审计', group: '系统' },
]

function parseHash(): { path: string; query: URLSearchParams } {
  const raw = window.location.hash.replace(/^#/, '')
  if (!raw.startsWith('/')) return { path: '/overview', query: new URLSearchParams() }
  const qIndex = raw.indexOf('?')
  if (qIndex === -1) return { path: raw, query: new URLSearchParams() }
  return {
    path: raw.slice(0, qIndex),
    query: new URLSearchParams(raw.slice(qIndex + 1)),
  }
}

export const route = ref<string>(parseHash().path)
export const routeQuery = ref<URLSearchParams>(parseHash().query)

window.addEventListener('hashchange', () => {
  const parsed = parseHash()
  route.value = parsed.path
  routeQuery.value = parsed.query
})

export function navigate(to: string): void {
  if (window.location.hash !== `#${to}`) {
    window.location.hash = to
  }
}

export function segments(): string[] {
  return route.value.split('/').filter(Boolean)
}

export function isActive(path: string): boolean {
  return route.value === path || route.value.startsWith(`${path}/`)
}
