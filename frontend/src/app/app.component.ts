import { JsonPipe } from '@angular/common';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { forkJoin } from 'rxjs';
import { ApiService } from './api.service';
import {
  EvidenceRecord,
  EventRecord,
  IsolationResult,
  PlanDetail,
  PlanSummary,
  Topology,
} from './models';
import { NetworkGraphComponent } from './network-graph.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [NetworkGraphComponent, FormsModule, JsonPipe],
  templateUrl: './app.component.html',
})
export class AppComponent implements OnInit {
  private api = inject(ApiService);

  topology = signal<Topology | null>(null);
  result = signal<IsolationResult | null>(null);
  targetId = signal<string>('T');
  loading = signal(false);
  error = signal<string | null>(null);
  notice = signal<string | null>(null);

  // 阀门锁定状态（仅前端选择，计算时随请求提交并由后端持久化）
  locks = signal<Record<string, boolean>>({});

  // ---- 核验证据工作流 ----
  evidenceList = signal<EvidenceRecord[]>([]);
  plans = signal<PlanSummary[]>([]);
  events = signal<EventRecord[]>([]);
  showEvents = signal(false);
  /** 历史方案回放：非空时结果区/拓扑图显示该方案的版本化快照 */
  viewingPlan = signal<PlanDetail | null>(null);

  evForm = {
    valve_id: 'V_TIN',
    observed: 'closed' as 'open' | 'closed' | 'unknown',
    observed_at: '',
    note: '',
  };

  /** 待处置矛盾（来自拓扑响应，刷新页面后依然保留） */
  dispositions = computed(() => this.topology()?.dispositions ?? []);

  /** 图上展示的结果：历史回放时显示旧方案快照，否则显示实时计算结果 */
  displayResult = computed<IsolationResult | null>(
    () => this.viewingPlan()?.result ?? this.result(),
  );

  /** 当前实时计算对应的方案摘要（确认门禁状态以后端推导为准） */
  currentPlan = computed(() => {
    const pid = this.result()?.plan_id;
    return pid ? (this.plans().find((p) => p.id === pid) ?? null) : null;
  });

  ngOnInit(): void {
    this.refreshAll();
  }

  refreshAll(): void {
    this.loading.set(true);
    forkJoin({
      topo: this.api.topology(),
      evidence: this.api.listEvidence(),
      plans: this.api.listPlans(),
      events: this.api.events(),
    }).subscribe({
      next: ({ topo, evidence, plans, events }) => {
        this.topology.set(topo);
        this.evidenceList.set(evidence);
        this.plans.set(plans);
        this.events.set(events);
        const l: Record<string, boolean> = {};
        for (const v of topo.valves) {
          l[v.id] = v.locked;
        }
        this.locks.set(l);
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`无法加载：${e.message ?? e}`);
        this.loading.set(false);
      },
    });
  }

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  /** 模拟现场操作：改变模型阀态（会立即使旧证据/旧方案过期） */
  setValveState(valveId: string, isOpen: boolean): void {
    this.run(() => this.api.setValveState(valveId, isOpen), `阀门 ${valveId} 模型阀态已更新`);
  }

  compute(): void {
    this.loading.set(true);
    this.error.set(null);
    this.viewingPlan.set(null);
    this.api.isolate(this.targetId(), this.locks()).subscribe({
      next: (r) => {
        this.result.set(r);
        this.locks.update((l) => {
          const next = { ...l };
          for (const v of r.candidate_valves) {
            next[v.id] = v.locked;
          }
          return next;
        });
        this.loading.set(false);
        this.refreshAll();
      },
      error: (e) => {
        this.error.set(`计算失败：${e.error?.detail ?? e.message ?? e}`);
        this.loading.set(false);
      },
    });
  }

  /** 快捷装载三个培训样例的锁定组合（样例 1 先重置）。 */
  loadSample(sample: 1 | 2 | 3): void {
    const applyLocksAndCompute = () => {
      const l: Record<string, boolean> = {};
      for (const v of this.topology()?.valves ?? []) {
        l[v.id] = false;
      }
      if (sample === 2) {
        l['V_TIN'] = true;
      } else if (sample === 3) {
        l['V_TOUT'] = true;
      }
      this.locks.set(l);
      this.compute();
    };

    if (sample === 1) {
      this.api.reset().subscribe(() => {
        this.api.topology().subscribe((t) => {
          this.topology.set(t);
          const l: Record<string, boolean> = {};
          for (const v of t.valves) {
            l[v.id] = v.locked;
          }
          this.locks.set(l);
          applyLocksAndCompute();
        });
      });
    } else {
      applyLocksAndCompute();
    }
  }

  resetAll(): void {
    this.api.reset().subscribe(() => {
      this.result.set(null);
      this.viewingPlan.set(null);
      this.refreshAll();
    });
  }

  // ---- 核验证据 ----

  submitEvidence(): void {
    const observedAt = this.evForm.observed_at
      ? new Date(this.evForm.observed_at).toISOString()
      : new Date().toISOString();
    this.run(
      () =>
        this.api.submitEvidence({
          valve_id: this.evForm.valve_id,
          observed: this.evForm.observed,
          observed_at: observedAt,
          plan_id: this.result()?.plan_id ?? null,
          note: this.evForm.note,
        }),
      `证据已提交（发生时刻 ${observedAt}）`,
    );
  }

  review(valveId: string, action: 'correct_model' | 'dismiss'): void {
    this.run(
      () => this.api.reviewValve(valveId, action),
      action === 'correct_model'
        ? `复核完成：已采纳观察并纠正模型（${valveId}），请重新计算方案`
        : `复核完成：已驳回观察（${valveId}），模型保持不变`,
    );
  }

  confirmPlan(planId: string): void {
    this.error.set(null);
    this.notice.set(null);
    this.api.confirmPlan(planId).subscribe({
      next: () => {
        this.notice.set(`方案 ${planId} 已确认（演示工作流，不代表真实安全确认）`);
        this.refreshAll();
      },
      error: (e) => {
        this.error.set(`确认被拒绝：${e.error?.detail ?? e.message ?? e}`);
        this.refreshAll();
      },
    });
  }

  /** 查看历史方案：载入其版本化快照与关联证据（回放模式） */
  viewPlan(planId: string): void {
    this.api.getPlan(planId).subscribe({
      next: (p) => this.viewingPlan.set(p),
      error: (e) => this.error.set(`无法载入方案：${e.error?.detail ?? e.message ?? e}`),
    });
  }

  closePlanView(): void {
    this.viewingPlan.set(null);
  }

  toggleEvents(): void {
    this.showEvents.update((v) => !v);
  }

  private run(action: () => import('rxjs').Observable<unknown>, okMessage: string): void {
    this.error.set(null);
    this.notice.set(null);
    action().subscribe({
      next: () => {
        this.notice.set(okMessage);
        this.refreshAll();
      },
      error: (e) => {
        this.error.set(`操作失败：${e.error?.detail ?? e.message ?? e}`);
        this.refreshAll();
      },
    });
  }

  valveName(id: string): string {
    return this.topology()?.valves.find((v) => v.id === id)?.name ?? id;
  }

  nodeName(id: string): string {
    return this.topology()?.nodes.find((n) => n.id === id)?.name ?? id;
  }

  formatValves(valves: (string | null)[]): string {
    return valves.map((v) => v ?? '—').join('、');
  }

  joinIds(ids: string[]): string {
    return ids.join('、');
  }

  observedLabel(observed: string): string {
    return observed === 'open' ? '开' : observed === 'closed' ? '关' : '未知';
  }

  formatTime(iso: string): string {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  eventLabel(type: string): string {
    const labels: Record<string, string> = {
      valve_state_changed: '模型阀态变更',
      lock_changed: '锁定变更',
      locks_updated: '锁定批量变更',
      reset: '全部重置',
      plan_computed: '方案计算',
      plan_confirmed: '方案确认',
      evidence_submitted: '核验证据提交',
      review_completed: '复核/纠正完成',
    };
    return labels[type] ?? type;
  }
}
