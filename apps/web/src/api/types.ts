// Shapes mirror what the FastAPI routers actually return. Fields are optional
// where a run may not have reached that stage yet — the UI must not invent them.

export interface RunBrief {
  run_id: string
  task_id: string
  case_id: string
  attempt: number
  status: string
  execution_status: string
  test_verdict: string
  created_at: string
  finished_at: string
}

export interface Overview {
  tasks: number
  runs: number
  runs_by_status: Record<string, number>
  runs_by_verdict: Record<string, number>
  /** Fixed 14-day buckets, zero-filled; feeds the trend sparkline. */
  runs_daily: { date: string; count: number }[]
  /** Runs that actually reached a verdict (denominator for the rates). */
  evaluated: number
  /** null when nothing has been evaluated yet — never a fake 0%. */
  success_rate: number | null
  failure_rate: number | null
  recent_runs: RunBrief[]
  cases: number
  /** Case files that failed validation; skipped, but named so they can be fixed. */
  invalid_cases?: { file: string; reason: string }[]
  events: number
  evidence: number
  repairs_pending: number
  explorations: { total: number; scripted: number; llm: number }
}

export interface IntentRow {
  intent_id: string
  description: string
  source_ref: string
  case_id: string | null
  case_version: number | null
  origin: 'case' | 'library'
}

export interface CaseRow {
  case_id: string
  version: number
  title: string
  steps: number
  assertions: number
  compiled_from: string
  approved_by: string | null
}

export interface TestStep {
  step_id: string
  action: string
  target: string
  value: string
  expected: string
}

export interface AssertionSpec {
  assertion_id: string
  description: string
  required: boolean
  source_ref: string
  notes: string[]
}

export interface OracleSpec {
  oracle_id: string
  source_type: string
  source_ref: string
  expected: string
  version: string
  approved: boolean
  expected_value: unknown
}

export interface OracleBinding {
  assertion_id: string
  primary: OracleSpec
  supporting: OracleSpec[]
  adjudication_rule: string
}

export interface CaseVersionRef {
  version: number
  file: string
  compiled_from: string
}

export interface CaseDetail {
  case_id: string
  version: number
  title: string
  intent: { intent_id: string; description: string; source_ref: string }
  steps: TestStep[]
  assertions: AssertionSpec[]
  oracle_bindings: OracleBinding[]
  preconditions: { description: string; setup_action: string }[]
  postconditions: { description: string; cleanup_action: string }[]
  cleanup: { strategy: string; reset_endpoint: string }
  compiled_from: string
  approved_by: string
  source_file: string
  versions: CaseVersionRef[]
}

export interface TaskRow {
  task_id: string
  case_id: string
  title: string
  status: string
  created_at: string
}

export interface RunRow {
  run_id: string
  task_id: string
  case_id: string
  attempt: number
  status: string
  execution_status: string
  test_verdict: string
  diagnosis_status: string
  diagnosis_outcome: string
  created_at: string
  started_at: string
  finished_at: string
  heartbeat_at: string
  abort_reason?: string
  worker_id?: string
}

export interface StepRow {
  index: number
  step_id: string
  action: string
  ok: boolean
  detail: string
}

export interface StepReport {
  step_id: string
  action: string
  ok: boolean
  detail: string
  evidence_ids: string[]
}

export interface AssertionResult {
  assertion_id: string
  required: boolean
  verdict: string
  oracle_available: boolean
  oracle_conflict: boolean
  evaluated: boolean
  evidence_refs: string[]
  reason: string
}

export interface RunReport {
  run_id: string
  case_id: string
  case_version: number
  started_at: string
  finished_at: string
  execution_status: string
  test_verdict: string
  steps: StepReport[]
  assertion_results: AssertionResult[]
  observations: Record<string, unknown>
  verdict_notes: string[]
  evidence: Record<string, unknown>[]
  events: Record<string, unknown>[]
  aborted_reason: string
}

export interface RunDetail extends RunRow {
  steps: StepRow[]
}

export interface EventRow {
  event_id: string
  event_type: string
  sequence?: number
  run_id?: string
  step_id?: string | null
  producer_id?: string
  producer_instance_id?: string
  occurred_at?: string
  recorded_at?: string
  idempotency_key?: string
  payload?: Record<string, unknown>
}

export interface EvidenceRow {
  evidence_id: string
  evidence_type: string
  raw: boolean
  source: string
  run_id: string
  step_id: string | null
  recorded_at: string
  summary: string
  integrity_check: string
  derived_from: string[]
  redacted: boolean
  retention: string
  payload: Record<string, unknown>
}

export interface GraphNode {
  node_id: string
  node_type: string
  source_ref: string
  independence_group: string
  properties: Record<string, unknown> | null
}

export interface GraphEdge {
  edge_id: string
  src: string
  dst: string
  relation: string
  basis_fields: string
  rule_id: string
  rule_version: string
}

export interface EvidenceRef {
  ref_id: string
  source: string
  role: string
  detail: string
}

export interface Hypothesis {
  hypothesis_id: string
  statement: string
  failure_domain: string
  failure_mechanism: string
  status: string
  supporting: EvidenceRef[]
  contradicting: EvidenceRef[]
  confirmation_rule: string
}

export interface Diagnosis {
  diagnosis_id: string
  run_id: string
  symptom: string
  failure_domain: string
  failure_mechanism: string
  root_causes: string[]
  contributing_factors: string[]
  flaky_characteristic: string
  diagnosis_status: string
  disposition: string
  hypotheses: Hypothesis[]
  notes: string[]
}

export interface EvidenceAuditRow {
  action: string
  detail: string
  recorded_at: string
}

export interface ProbeSpecRow {
  probe_id: string
  target_scope: string
  permission_required: string
  read_only: boolean
  side_effects: string
  estimated_cost: number
  estimated_duration: number
  risk_level: string
  timeout: number
  retry_policy: string
  cleanup_policy: string
}

export interface AuditRow {
  probe_id: string
  caller: string
  decision: string
  reason: string
  check: string
  at: string
}

export interface DiffEntry {
  kind: string
  target: string
  before: string
  after: string
}

export interface RepairProposal {
  proposal_id: string
  case_id: string
  state: string
  revision: number
  change_kinds: string[]
  diff: DiffEntry[]
  base_testcase_version: number
  candidate_testcase_version: number
  promotion_target_version: number
  validation_notes: string[]
  approved_by: string
  approval_record: string
  failure_reason: string
  created_at: string
}

export interface DatasetRow {
  dataset_id: string
  partition: string
  scenario_ids: string[]
  version: string
  metadata: Record<string, unknown>
}

export interface ExperimentRow {
  experiment_id: string
  track: string
  method: string
  dataset_id: string
  scenario_count: number
  metrics: Record<string, number | null>
  status: string
  note: string
}

// ---- Explorer -------------------------------------------------------------
// `is_llm` is the honesty switch: the UI must render a deterministic policy as
// deterministic, never as a model run.

export interface PolicyRow {
  id: string
  label: string
  kind: string
  is_llm: boolean
  verified: boolean
  agent_version: string
  note: string
}

export interface PolicyCatalogue {
  policies: PolicyRow[]
  active_policy: string
  llm_blocked_reason: string
  /** Registered business scenarios; anything else is UNSUPPORTED_SCENARIO. */
  supported_scenarios: Record<string, string>
}

/** Goal verification check: evidence-backed, or explicitly deferred. */
export interface GoalCheck {
  name: string
  expected: string
  observed: string
  ok: boolean
  evaluated: boolean
  deferred: boolean
  evidence: string
  note: string
}

export interface ExplorationSummary {
  exploration_id: string
  test_intent_ref: string
  target: string
  policy: string
  policy_label: string
  is_llm: boolean
  /** Execution status of the exploration process: COMPLETED / FAILED / ... */
  status: string
  /** Why the loop stopped — never a goal verdict by itself. */
  stop_reason: string
  /** Business-goal verdict, deliberately separate from status/stop_reason. */
  goal_status: string
  goal_reason: string
  step_count: number
  started_at: string
  finished_at: string
  source_trace_hash: string
  error: string | null
}

export interface ExplorationAction {
  tool?: string
  args?: Record<string, unknown>
  detail?: string
  rejected?: boolean
  reason?: string
}

export interface ExplorationTrace extends ExplorationSummary {
  application_version: string
  browser_version: string
  agent_version: string
  prompt_version: string
  tool_version: string
  action_sequence: ExplorationAction[]
  page_state_ref: string
  event_refs: string[]
  evidence_refs: string[]
  /** Count of credential-shaped args masked server-side before storage. */
  redacted_fields?: number
  /** The full TestIntent, embedded in the trace and covered by its hash. */
  test_intent?: {
    intent_id: string
    description: string
    source_ref: string
    scenario?: string
    goal: string
    expected_outcomes: string[]
    target: string
    required_assertions: string[]
    constraints: string[]
  }
  goal_checks?: GoalCheck[]
}

export interface ReviewChecklistItem {
  id: string
  text: string
  required: boolean
}

export interface TestDataRefRow {
  ref_id: string
  locator: string
  value: string
}

export interface CandidateRow {
  candidate_id: string
  case_id: string
  title: string
  target?: string
  status: 'PENDING_REVIEW' | 'APPROVED' | 'REJECTED' | string
  source_exploration_id: string
  source_trace_hash: string
  step_count: number
  assertion_count: number
  uncertainty_notes: string[]
  review_checklist: ReviewChecklistItem[]
  goal_status?: string
  model_version: string
  is_llm: boolean
  created_at: string
  reviewer: string
  approval_record: string
  case_file: string
}

export interface CandidateStep {
  step_id: string
  action: string
  target: string
  value: string
  expected: string
  value_ref?: string
}

export interface CandidateOracleSpec {
  oracle_id: string
  source_type: string
  source_ref: string
  expected: string
  approved: boolean
  approved_by: string
  source: string
  expected_value?: unknown
}

export interface CandidateDetail extends CandidateRow {
  intent: {
    intent_id: string
    description: string
    source_ref: string
    goal?: string
    expected_outcomes?: string[]
    target?: string
    required_assertions?: string[]
    constraints?: string[]
  }
  steps: CandidateStep[]
  oracle_bindings: {
    assertion_id: string
    primary: CandidateOracleSpec
    supporting: CandidateOracleSpec[]
  }[]
  compiler_version: string
  locator_strategy_version: string
  prompt_version: string
  compiled_from: string
  rejection_reason: string
  observations?: Record<string, unknown>[]
  data_refs?: TestDataRefRow[]
  review_checklist: ReviewChecklistItem[]
  goal_status?: string
  goal_checks?: GoalCheck[]
  goal_reason?: string
}

export interface ChainStageRow {
  name: string
  status: string
  detail: string
  at?: string
}

export interface ChainSummary {
  chain_id: string
  scenario_id: string
  case_id: string
  case_version: number
  run_id: string | null
  test_verdict: string | null
  execution_status: string | null
  evidence_count: number
  event_count: number
  diagnosis_status: string | null
  disposition: string | null
  completed: boolean
  started_at: string
  duration_s: number | null
  persisted: { events?: { written: number; skipped: number }; evidence?: { written: number; skipped: number } } | null
  stages: ChainStageRow[]
}

export interface Subgraph {
  root: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  node_total: number
  edge_total: number
  offset: number
  limit: number | null
  truncated: boolean
}
