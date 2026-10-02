import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import {
  Disposition,
  EvidenceList,
  EvidenceSubmitResponse,
  EventList,
  IsolationResult,
  ObservedValue,
  PlanReview,
  ReplayResponse,
  Topology,
} from './models';

export interface EvidenceInput {
  valve_id: string;
  observed: ObservedValue;
  observed_at?: string | null;
  plan_id?: number | null;
  observer?: string | null;
  note?: string | null;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  topology(): Observable<Topology> {
    return this.http.get<Topology>('/api/topology');
  }

  setLock(valveId: string, locked: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/lock`, { locked });
  }

  setValveState(valveId: string, isOpen: boolean, note?: string): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/state`, { is_open: isOpen, note });
  }

  reset(): Observable<unknown> {
    return this.http.post('/api/reset', {});
  }

  isolate(targetId: string, locks?: Record<string, boolean>): Observable<IsolationResult> {
    return this.http.post<IsolationResult>('/api/isolation', {
      target_id: targetId,
      locks: locks ?? null,
    });
  }

  // ---- 方案 ----

  plans(): Observable<{ plans: PlanReview[]; model_revision: number }> {
    return this.http.get<{ plans: PlanReview[]; model_revision: number }>('/api/plans');
  }

  plan(planId: number): Observable<PlanReview> {
    return this.http.get<PlanReview>(`/api/plans/${planId}`);
  }

  confirmPlan(planId: number, body: { note?: string; confirmer?: string } = {}): Observable<PlanReview & { warning?: string }> {
    return this.http.post<PlanReview & { warning?: string }>(`/api/plans/${planId}/confirm`, body);
  }

  // ---- 核验证据 ----

  evidence(): Observable<EvidenceList> {
    return this.http.get<EvidenceList>('/api/evidence');
  }

  submitEvidence(body: EvidenceInput): Observable<EvidenceSubmitResponse> {
    return this.http.post<EvidenceSubmitResponse>('/api/evidence', body);
  }

  reviewEvidence(
    evidenceCode: string,
    disposition: Disposition,
    extra: { note?: string; reviewer?: string } = {},
  ): Observable<unknown> {
    return this.http.post(`/api/evidence/${evidenceCode}/review`, {
      disposition,
      note: extra.note ?? null,
      reviewer: extra.reviewer ?? null,
    });
  }

  replayEvidence(evidenceCode: string): Observable<ReplayResponse> {
    return this.http.get<ReplayResponse>(`/api/evidence/${evidenceCode}/replay`);
  }

  // ---- 事件链 ----

  events(): Observable<EventList> {
    return this.http.get<EventList>('/api/events');
  }
}
