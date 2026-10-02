import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import {
  EvidenceRecord,
  EventRecord,
  IsolationResult,
  PlanDetail,
  PlanSummary,
  Topology,
} from './models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  topology(): Observable<Topology> {
    return this.http.get<Topology>('/api/topology');
  }

  setLock(valveId: string, locked: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/lock`, { locked });
  }

  /** 模拟现场操作：改变模型阀态（锁定阀门会被后端拒绝）。 */
  setValveState(valveId: string, isOpen: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/state`, { is_open: isOpen });
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

  submitEvidence(body: {
    valve_id: string;
    observed: 'open' | 'closed' | 'unknown';
    observed_at: string;
    plan_id: string | null;
    note: string;
  }): Observable<EvidenceRecord> {
    return this.http.post<EvidenceRecord>('/api/evidence', body);
  }

  listEvidence(): Observable<EvidenceRecord[]> {
    return this.http.get<EvidenceRecord[]>('/api/evidence');
  }

  reviewValve(
    valveId: string,
    action: 'correct_model' | 'dismiss',
    note = '',
  ): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/review`, { action, note });
  }

  listPlans(): Observable<PlanSummary[]> {
    return this.http.get<PlanSummary[]>('/api/plans');
  }

  getPlan(planId: string): Observable<PlanDetail> {
    return this.http.get<PlanDetail>(`/api/plans/${planId}`);
  }

  confirmPlan(planId: string): Observable<unknown> {
    return this.http.post(`/api/plans/${planId}/confirm`, {});
  }

  events(): Observable<EventRecord[]> {
    return this.http.get<EventRecord[]>('/api/events');
  }
}
