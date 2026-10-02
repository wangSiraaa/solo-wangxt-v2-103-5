export interface TopoNode {
  id: string;
  name: string;
  kind: 'source' | 'equipment' | 'consumer' | 'junction';
  x: number;
  y: number;
  essential: boolean;
}

export interface Segment {
  id: string;
  source: string;
  target: string;
  direction: string;
  kind: 'main' | 'bypass' | 'branch';
  is_bypass: boolean;
  valve_id: string | null;
}

export type VerificationStatus =
  | 'verified'
  | 'pending'
  | 'stale'
  | 'contradiction'
  | 'resolved';

export interface VerificationView {
  status: VerificationStatus;
  status_label: string;
  evidence_code: string | null;
  observed_open: boolean | null;
  explanation: string;
  resolved_by?: ReviewRecord | null;
  was_contradiction?: boolean;
}

export interface Valve {
  id: string;
  name: string;
  segment_id: string;
  endpoints: string[];
  is_open: boolean;
  locked: boolean;
  operable: boolean;
  is_bypass: boolean;
  verification?: VerificationView | null;
}

export interface Topology {
  nodes: TopoNode[];
  segments: Segment[];
  valves: Valve[];
  model_revision: number;
}

export interface Solution {
  close_valves: string[];
  size: number;
  alternative_rank: number;
  closes_bypass_valves: string[];
  supply_paths: Record<string, string[] | null>;
}

export interface ResidualPath {
  nodes: string[];
  valves: (string | null)[];
  locked_valves_on_path: string[];
  uses_bypass: boolean;
}

export interface AffectedResidualPath {
  nodes: string[];
  valves: (string | null)[];
  contradiction_valves: string[];
}

export interface AffectedResidualImpact {
  target_reachable_with_observations: boolean;
  paths: AffectedResidualPath[];
  essentials: Record<string, { supplied: boolean }>;
}

export interface ValveCheck {
  valve_id: string;
  status: VerificationStatus | string;
  status_label: string;
  evidence_code: string | null;
  observed_label?: string;
  blocks_confirmation: boolean;
  reason: string;
}

export interface LinkedEvidence {
  evidence_code: string;
  valve_id: string;
  observed_label: string;
  observed_at: string;
  status: VerificationStatus;
  status_label: string;
  explanation: string;
  plan_id: number | null;
  stale_reason?: string | null;
  was_contradiction?: boolean;
}

export type PlanStatus =
  | 'confirmable'
  | 'blocked'
  | 'outdated'
  | 'infeasible'
  | 'confirmed';

export interface PlanReview {
  plan_code: string;
  plan_id: number;
  target_id: string;
  feasible: boolean;
  close_valves: string[];
  status: PlanStatus;
  status_text: string;
  confirmable: boolean;
  superseded_by: number | null;
  confirmed_seq: number | null;
  confirmed_at: string | null;
  created_seq: number;
  model_revision: number;
  model_revision_now: number;
  stale_reasons: string[];
  valve_checks: ValveCheck[];
  blockers: ValveCheck[];
  linked_evidence: LinkedEvidence[];
  affected_residual_paths: AffectedResidualImpact;
  examined_combinations: number;
  result?: IsolationResult;
}

export interface IsolationResult {
  feasible: boolean;
  target_id: string;
  sources: string[];
  essentials: string[];
  candidate_valves: Valve[];
  examined_combinations: number;
  solutions: Solution[];
  best_solution: string[];
  residual_path: ResidualPath | null;
  locked_witness_path: ResidualPath | null;
  infeasible_reason: string | null;
  unconstrained_best: {
    close_valves: string[];
    size: number;
    disconnects_essentials: string[];
    unavoidable_essentials: string[];
  } | null;
  plan?: PlanReview;
}

// ---------------------------------------------------------------------------
// 核验证据 / 事件链
// ---------------------------------------------------------------------------

export type ObservedValue = 'open' | 'closed' | 'unknown';
export type Disposition = 'correct_model' | 'confirm_model' | 'reinspect';

export interface EvidenceAssessment {
  evidence_code: string;
  valve_id: string;
  observed_open: boolean | null;
  observed_label: string;
  observed_at: string;
  submitted_seq: number;
  model_revision_at_evidence: number;
  model_revision_now: number;
  eval_version: string;
  plan_id: number | null;
  observer: string | null;
  note: string | null;
  status: VerificationStatus;
  status_label: string;
  explanation: string;
  stale_reason?: string | null;
  model_valve_open?: boolean;
  model_valve_locked?: boolean;
  resolved_by?: ReviewRecord | null;
  was_contradiction?: boolean;
  resolution?: unknown;
}

export interface PlanEffect {
  plan_code: string;
  plan_id: number;
  in_close_set: boolean;
  linked: boolean;
  blocks_confirmation: boolean;
  plan_status: PlanStatus;
}

export interface EvidenceRecord extends EvidenceAssessment {
  affected_plans: PlanEffect[];
}

export interface ReviewRecord {
  seq: number;
  occurred_at: string;
  evidence_code: string;
  valve_id: string;
  disposition: Disposition;
  note: string | null;
  set_is_open: boolean | null;
  model_revision: number;
}

export interface EvidenceList {
  evidence: EvidenceRecord[];
  reviews: ReviewRecord[];
  model_revision: number;
}

export interface EvidenceSubmitResponse {
  evidence_code: string;
  event_seq: number;
  assessment: EvidenceAssessment;
  valve_current_status: VerificationStatus;
  affected_plans: PlanEffect[];
  training_notice: string;
}

export interface ReplayResponse {
  eval_version: string;
  evidence: {
    evidence_code: string;
    valve_id: string;
    observed_open: boolean | null;
    observed_label: string;
    observed_at: string;
    submitted_seq: number;
    plan_id: number | null;
    observer: string | null;
    note: string | null;
  };
  interpretation_at_submission: {
    status: VerificationStatus;
    status_label: string;
    explanation: string;
    model_revision: number;
    model_valve: {
      id: string;
      is_open: boolean;
      locked: boolean;
      operable: boolean;
    } | null;
  };
  interpretation_current: {
    status: VerificationStatus;
    status_label: string;
    explanation: string;
    model_revision: number;
    stale_reason: string | null;
    resolved_by: ReviewRecord | null;
  };
  reviews: ReviewRecord[];
  snapshot_fingerprint: string;
}

export interface ChainEvent {
  seq: number;
  event_type: string;
  occurred_at: string;
  model_revision: number;
  prev_hash: string;
  self_hash: string;
  payload: Record<string, unknown>;
}

export interface EventList {
  events: ChainEvent[];
  chain: {
    ok: boolean;
    count: number;
    broken_seq: number | null;
    reason: string | null;
  };
}
