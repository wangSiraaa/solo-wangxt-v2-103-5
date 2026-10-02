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

export type EvidenceStatus =
  | 'verified'
  | 'pending'
  | 'expired'
  | 'contradiction'
  | 'superseded'
  | 'resolved_corrected'
  | 'resolved_dismissed';

export interface VerificationInfo {
  status: EvidenceStatus;
  status_label: string;
  evidence_id: string;
  observed: 'open' | 'closed' | 'unknown';
  observed_at: string;
  seq: number;
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
  verification: VerificationInfo | null;
}

export interface AffectedPath {
  nodes: string[];
  valves: (string | null)[];
  through_valve: string;
}

export interface Disposition {
  valve_id: string;
  evidence_id: string;
  observed: 'open' | 'closed' | 'unknown';
  model_is_open: boolean;
  model_locked: boolean;
  observed_at: string;
  affected_residual_path: AffectedPath | null;
}

export interface Topology {
  nodes: TopoNode[];
  segments: Segment[];
  valves: Valve[];
  topology_version: number;
  dispositions: Disposition[];
}

export interface Solution {
  close_valves: string[];
  size: number;
  alternative_rank: number;
  closes_bypass_valves: string[];
  supply_paths: Record<string, string[] | null>;
  blocked_by?: string[];
}

export interface ResidualPath {
  nodes: string[];
  valves: (string | null)[];
  locked_valves_on_path: string[];
  uses_bypass: boolean;
}

export interface IsolationResult {
  feasible: boolean;
  target_id: string;
  sources: string[];
  essentials: string[];
  candidate_valves: Valve[];
  assumed_closed_valves: string[];
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
  plan_id: string | null;
  topology_version: number;
  confirmable: boolean;
  blocked_reason: string | null;
  dispositions: Disposition[];
}

export interface EvidenceRecord {
  id: string;
  seq: number;
  valve_id: string;
  observed: 'open' | 'closed' | 'unknown';
  observed_at: string;
  plan_id: string | null;
  topology_version: number;
  model_is_open: boolean;
  model_locked: boolean;
  effective: boolean;
  status: EvidenceStatus;
  status_label: string;
  resolution: 'corrected' | 'dismissed' | null;
  resolved_seq: number | null;
  note: string;
  snapshot: { topology_version: number; valves: Record<string, { is_open: boolean; locked: boolean }> };
  created_at: string;
}

export type PlanStatus = 'confirmable' | 'blocked' | 'stale' | 'confirmed' | 'infeasible';

export interface PlanSummary {
  id: string;
  seq: number;
  target_id: string;
  feasible: boolean;
  topology_version: number;
  close_valves: string[];
  assumed_closed_valves: string[];
  status: PlanStatus;
  status_label: string;
  blocked_by: string[];
  confirmed_seq: number | null;
  created_at: string;
}

export interface PlanDetail extends PlanSummary {
  result: IsolationResult;
  current_topology_version: number;
  stale: boolean;
  explanation: string;
  evidence: EvidenceRecord[];
}

export interface EventRecord {
  seq: number;
  type: string;
  created_at: string;
  payload: Record<string, unknown>;
}
