// Thin typed wrapper over fetch. There is no mock layer here on purpose: every
// call hits the real FastAPI router, and failures surface as errors in the UI.

import type {
  AuditRow,
  CandidateDetail,
  CandidateRow,
  CaseDetail,
  CaseRow,
  ChainSummary,
  DatasetRow,
  Diagnosis,
  EventRow,
  EvidenceRow,
  ExperimentRow,
  ExplorationSummary,
  ExplorationTrace,
  GraphEdge,
  GraphNode,
  IntentRow,
  Overview,
  PolicyCatalogue,
  ProbeSpecRow,
  RepairProposal,
  RunDetail,
  RunReport,
  RunRow,
  Subgraph,
  TaskRow,
} from './types'

const BASE = '/api/v1'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return requestRaw(BASE + path, init)
}

// Same semantics, but for root-level paths outside /api/v1 (/health, /version).
async function requestRoot<T>(path: string, init?: RequestInit): Promise<T> {
  return requestRaw(path, init)
}

async function requestRaw<T>(url: string, init?: RequestInit): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new ApiError(0, '无法连接后端服务，请确认 API 已启动')
  }
  if (!resp.ok) {
    let message = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      message = body?.error?.message ?? message
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, message)
  }
  if (resp.status === 204) {
    return undefined as T
  }
  return (await resp.json()) as T
}

function qs(params: Record<string, string | number | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

export const api = {
  overview: () => request<Overview>('/overview'),

  intents: () => request<IntentRow[]>('/intents'),
  createIntent: (body: { intent_id: string; description: string; source_ref: string }) =>
    request<IntentRow>('/intents', { method: 'POST', body: JSON.stringify(body) }),

  cases: () => request<CaseRow[]>('/cases'),
  caseDetail: (caseId: string) => request<CaseDetail>(`/cases/${encodeURIComponent(caseId)}`),

  tasks: () => request<TaskRow[]>('/tasks'),
  createTask: (body: { task_id: string; case_id: string; title: string }) =>
    request<TaskRow>('/tasks', { method: 'POST', body: JSON.stringify(body) }),
  task: (taskId: string) => request<TaskRow>(`/tasks/${encodeURIComponent(taskId)}`),
  taskRuns: (taskId: string) =>
    request<RunRow[]>(`/tasks/${encodeURIComponent(taskId)}/runs`),
  submitRun: (taskId: string, body: { case_file?: string | null; timeout_s: number }) =>
    request<RunRow>(`/tasks/${encodeURIComponent(taskId)}/runs`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  run: (runId: string) => request<RunDetail>(`/runs/${encodeURIComponent(runId)}`),
  report: (runId: string) => request<RunReport>(`/runs/${encodeURIComponent(runId)}/report`),
  cancelRun: (runId: string) =>
    request<RunRow>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' }),
  diagnosis: (runId: string) =>
    request<Diagnosis>(`/runs/${encodeURIComponent(runId)}/diagnosis`),

  events: (afterSequence = 0) =>
    request<EventRow[]>(`/events${qs({ after_sequence: afterSequence })}`),

  evidenceEvents: (params: { run_id?: string; event_type?: string; limit?: number } = {}) =>
    request<EventRow[]>(`/evidence/events${qs(params)}`),
  evidenceRecords: (params: { run_id?: string; layer?: string; limit?: number } = {}) =>
    request<EvidenceRow[]>(`/evidence/records${qs(params)}`),
  evidenceRecord: (id: string) =>
    request<EvidenceRow>(`/evidence/records/${encodeURIComponent(id)}`),
  evidenceGaps: () => request<{ producer_instance_id: string; missing_sequences: number[]; status: string; note: string }[]>(
    '/evidence/events/gaps',
  ),

  graphNodes: () => request<GraphNode[]>('/evidence/graph/nodes'),
  graphEdges: () => request<GraphEdge[]>('/evidence/graph/edges'),
  // Adjacency query. `limit` keeps a dense neighbourhood from returning the
  // whole graph at once; `truncated`/`node_total` let the panel say so.
  subgraph: (rootId: string, depth = 2, limit = 200) =>
    request<Subgraph>(
      `/evidence/graph/subgraph/${encodeURIComponent(rootId)}${qs({ depth, limit })}`,
    ),

  probes: () => request<ProbeSpecRow[]>('/probes'),
  probeAudit: () => request<AuditRow[]>('/probes/audit'),

  repairs: () => request<RepairProposal[]>('/repairs'),
  repair: (id: string) => request<RepairProposal>(`/repairs/${encodeURIComponent(id)}`),
  approveRepair: (id: string, body: { approver: string; approval_record: string }) =>
    request<RepairProposal>(`/repairs/${encodeURIComponent(id)}/approve`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  rejectRepair: (id: string, body: { reason: string; reviewer: string }) =>
    request<RepairProposal>(`/repairs/${encodeURIComponent(id)}/reject`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  benchmarkDatasets: () => request<DatasetRow[]>('/benchmark/datasets'),
  benchmarkExperiments: () => request<ExperimentRow[]>('/benchmark/experiments'),

  // Root-level endpoints (outside /api/v1) for the system page. The dev proxy
  // forwards /health and /version to the platform server.
  health: () => requestRoot<Record<string, unknown>>('/health'),
  version: () => requestRoot<Record<string, string>>('/version'),
  dispatchStatus: () =>
    request<{
      broker: Record<string, unknown>
      dispatcher: Record<string, unknown>
      workers: number
      dead_letters: {
        job_id: string
        run_id: string
        task_id: string
        case_id: string
        reason: string
        failed_at: string
        deliveries: number
      }[]
    }>('/dispatch/status'),
  evidenceAudit: () => request<import('./types').EvidenceAuditRow[]>('/evidence/events/audit'),

  // ---- Explorer ----
  explorationPolicies: () => request<PolicyCatalogue>('/explorations/policies'),
  explorations: () => request<ExplorationSummary[]>('/explorations'),
  exploration: (id: string) =>
    request<ExplorationTrace>(`/explorations/${encodeURIComponent(id)}`),
  runExploration: (body: {
    intent_ref: string
    goal?: string
    expected_outcomes?: string[]
    target?: string
    required_assertions?: string[]
    constraints?: string[]
    scenario?: string
    base_url: string
    policy: string
    max_steps: number
  }) =>
    request<ExplorationTrace>('/explorations', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ---- Candidates (compiled from a trace, gated by human review) ----
  candidates: () => request<CandidateRow[]>('/candidates'),
  candidate: (id: string) => request<CandidateDetail>(`/candidates/${encodeURIComponent(id)}`),
  compileExploration: (explorationId: string, body: { case_id?: string; title?: string }) =>
    request<CandidateRow>(`/explorations/${encodeURIComponent(explorationId)}/compile`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  approveCandidate: (
    id: string,
    body: {
      reviewer: string
      approval_record: string
      review_confirmed: string[]
      uncertainty_acknowledged: boolean
    },
  ) =>
    request<CandidateRow>(`/candidates/${encodeURIComponent(id)}/approve`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  rejectCandidate: (id: string, body: { reviewer: string; reason: string }) =>
    request<CandidateRow>(`/candidates/${encodeURIComponent(id)}/reject`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ---- Full chain (approved case -> run -> evidence -> diagnosis) ----
  chains: () => request<ChainSummary[]>('/chains'),
  chain: (id: string) => request<Record<string, unknown>>(`/chains/${encodeURIComponent(id)}`),
  runChain: (body: { case_id: string; scenario_id?: string }) =>
    request<ChainSummary>('/chains', { method: 'POST', body: JSON.stringify(body) }),
}

// SSE: real orchestration events, replayed from Last-Event-ID by the server.
//
// R5-5.1: the server also sends transport-level ``stream.notice`` frames
// (cursor-unknown / cursor-expired / gap / persist-degraded). They are NOT
// run events, so they get their own handler and never enter the event list.

export type StreamState = 'connecting' | 'live' | 'reconnecting' | 'closed'

export interface StreamNotice {
  code: string
  detail?: string
  latest?: number
  oldest_retained?: number | null
  pruned_through?: number
  from?: number
  to?: number
  missing?: number
  [key: string]: unknown
}

export interface StreamHandlers {
  onEvent: (row: EventRow) => void
  onState?: (state: StreamState, info: { reconnects: number }) => void
  onNotice?: (notice: StreamNotice) => void
}

export function subscribeEventStream(handlers: StreamHandlers): () => void {
  let reconnects = 0
  let opened = false
  let closed = false
  const source = new EventSource(`${BASE}/events/stream`)
  handlers.onState?.('connecting', { reconnects })

  source.onopen = () => {
    opened = true
    handlers.onState?.('live', { reconnects })
  }
  source.onerror = () => {
    if (closed) return
    // EventSource reconnects by itself; report it instead of pretending the
    // stream is up (the old wrapper flipped a boolean and lost the count).
    if (opened) reconnects += 1
    handlers.onState?.('reconnecting', { reconnects })
  }
  source.onmessage = (ev: MessageEvent) => {
    try {
      handlers.onEvent(JSON.parse(ev.data) as EventRow)
    } catch {
      /* ignore malformed frame */
    }
  }
  source.addEventListener('stream.notice', (ev: MessageEvent) => {
    try {
      handlers.onNotice?.(JSON.parse((ev as MessageEvent).data) as StreamNotice)
    } catch {
      /* ignore malformed notice */
    }
  })
  return () => {
    closed = true
    source.close()
    handlers.onState?.('closed', { reconnects })
  }
}

export function subscribeEvents(onEvent: (row: EventRow) => void): () => void {
  return subscribeEventStream({ onEvent })
}

// Kept for existing call sites: maps the richer state back to "is it open".
export function subscribeEventsSafe(
  onEvent: (row: EventRow) => void,
  onState: (open: boolean) => void,
): () => void {
  return subscribeEventStream({
    onEvent,
    onState: (state) => onState(state === 'live' || state === 'connecting'),
  })
}
