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
  icon: string
  /** Shown in the page header / command palette as a one-line purpose. */
  desc?: string
}

export const ROUTES: RouteDef[] = [
  { path: '/overview', label: '项目总览', group: '工作台', icon: 'overview', desc: '质量状态与最近运行' },
  { path: '/intents', label: '测试意图', group: '测试设计', icon: 'intent', desc: 'TestIntent 与来源' },
  { path: '/explorer', label: '探索器', group: '测试设计', icon: 'explorer', desc: 'Agent 受控探索与轨迹' },
  { path: '/candidates', label: '候选审核', group: '测试设计', icon: 'candidate', desc: '编译器产出的候选用例' },
  { path: '/cases', label: '测试用例', group: '测试资产', icon: 'cases', desc: '正式用例与 Oracle' },
  { path: '/tasks', label: '任务队列', group: '执行中心', icon: 'task', desc: '调度与排队状态' },
  { path: '/runs', label: '运行记录', group: '执行中心', icon: 'runs', desc: '执行历史与判定' },
  { path: '/events', label: '事件流', group: '证据中心', icon: 'events', desc: '实时编排事件' },
  { path: '/evidence', label: '证据台账', group: '证据中心', icon: 'evidence', desc: '不可变证据记录' },
  { path: '/graph', label: '证据图谱', group: '证据中心', icon: 'graph', desc: '证据关系与路径' },
  { path: '/diagnosis', label: '诊断', group: '智能诊断', icon: 'diagnosis', desc: '假设、支持与反证' },
  { path: '/probes', label: '探针审计', group: '智能诊断', icon: 'probes', desc: 'Probe 定义与执行' },
  { path: '/repairs', label: '修复审核', group: '修复中心', icon: 'repairs', desc: 'Diff、沙箱与审批' },
  { path: '/benchmark', label: '基准评测', group: '评测', icon: 'benchmark', desc: 'UI-FaultBench 实验' },
  { path: '/system', label: '系统状态', group: '系统', icon: 'system', desc: '服务、队列与版本' },
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

export function routeDef(path: string): RouteDef | undefined {
  return ROUTES.find((r) => r.path === path)
}
