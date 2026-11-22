// Thin typed wrapper over fetch. There is no mock layer here on purpose: every
// call hits the real FastAPI router, and failures surface as errors in the UI.

import type {
  AuditRow,
  CaseDetail,
  CaseRow,
  DatasetRow,
  Diagnosis,
  EventRow,
  EvidenceRow,
  ExperimentRow,
  GraphEdge,
  GraphNode,
  IntentRow,
  Overview,
  ProbeSpecRow,
  RepairProposal,
  RunDetail,
  RunReport,
  RunRow,
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
  subgraph: (rootId: string, depth = 2) =>
    request<unknown>(
      `/evidence/graph/subgraph/${encodeURIComponent(rootId)}${qs({ depth })}`,
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
}

// SSE: real orchestration events, replayed from Last-Event-ID by the server.
// The safe wrapper reports connection state so the UI can distinguish "live"
// from "reconnecting/disconnected" instead of pretending the stream is up.
export function subscribeEvents(onEvent: (row: EventRow) => void): () => void {
  const source = new EventSource(`${BASE}/events/stream`)
  const handler = (ev: MessageEvent) => {
    try {
      onEvent(JSON.parse(ev.data) as EventRow)
    } catch {
      /* ignore malformed frame */
    }
  }
  source.onmessage = handler
  return () => source.close()
}

export function subscribeEventsSafe(
  onEvent: (row: EventRow) => void,
  onState: (open: boolean) => void,
): () => void {
  const source = new EventSource(`${BASE}/events/stream`)
  const handler = (ev: MessageEvent) => {
    onState(true)
    try {
      onEvent(JSON.parse(ev.data) as EventRow)
    } catch {
      /* ignore malformed frame */
    }
  }
  source.onopen = () => onState(true)
  source.onerror = () => onState(false)
  source.onmessage = handler
  return () => source.close()
}
