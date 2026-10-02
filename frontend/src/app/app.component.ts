import { CommonModule } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';
import { NetworkGraphComponent } from './network-graph.component';
import {
  Disposition,
  EvidenceList,
  EvidenceRecord,
  IsolationResult,
  ObservedValue,
  PlanReview,
  ReplayResponse,
  Topology,
  VerificationStatus,
} from './models';

interface EvidenceForm {
  valve_id: string;
  observed: ObservedValue;
  observed_at: string;
  observer: string;
  note: string;
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, NetworkGraphComponent, FormsModule],
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

  // 方案 / 证据 / 事件链
  plans = signal<PlanReview[]>([]);
  evidenceList = signal<EvidenceList | null>(null);
  selectedPlan = signal<PlanReview | null>(null);
  replay = signal<ReplayResponse | null>(null);
  showEventChain = signal(false);
  eventChain = signal<{ seq: number; event_type: string; model_revision: number }[]>([]);
  chainOk = signal<boolean | null>(null);

  // 证据表单（默认关联当前最新计算的方案）
  evidenceForm = signal<EvidenceForm>({
    valve_id: 'V_TIN',
    observed: 'closed',
    observed_at: '',
    observer: '',
    note: '',
  });

  // 详情：展开的方案 / 证据
  expandedEvidence = signal<string | null>(null);

  ngOnInit(): void {
    this.reloadAll();
  }

  // ---------------------------------------------------------------- 数据加载

  reloadAll(): void {
    this.loading.set(true);
    this.api.topology().subscribe({
      next: (t) => {
        this.topology.set(t);
        const l: Record<string, boolean> = {};
        for (const v of t.valves) {
          l[v.id] = v.locked;
        }
        this.locks.set(l);
        this.loading.set(false);
      },
      error: (e) => this.fail(`无法加载拓扑：${e.message ?? e}`),
    });
    this.refreshPlans();
    this.refreshEvidence();
  }

  refreshPlans(selectPlanId?: number): void {
    this.api.plans().subscribe({
      next: (res) => {
        this.plans.set(res.plans);
        if (selectPlanId !== undefined) {
          const found = res.plans.find((p) => p.plan_id === selectPlanId) ?? null;
          this.selectedPlan.set(found);
        } else if (this.selectedPlan()) {
          const cur = this.selectedPlan()!;
          this.selectedPlan.set(res.plans.find((p) => p.plan_id === cur.plan_id) ?? null);
        }
      },
      error: (e) => this.fail(`方案列表加载失败：${e.message ?? e}`),
    });
  }

  refreshEvidence(keepExpanded = true): void {
    this.api.evidence().subscribe({
      next: (list) => this.evidenceList.set(list),
      error: (e) => this.fail(`证据列表加载失败：${e.message ?? e}`),
    });
    if (!keepExpanded) {
      this.expandedEvidence.set(null);
    }
  }

  fail(msg: string): void {
    this.error.set(msg);
    this.loading.set(false);
  }

  // ---------------------------------------------------------------- 锁定 / 计算

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  compute(): void {
    this.loading.set(true);
    this.error.set(null);
    this.notice.set(null);
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
        if (r.plan) {
          this.selectedPlan.set(r.plan);
        }
        this.loading.set(false);
        this.refreshPlans(r.plan?.plan_id);
        this.refreshEvidence();
      },
      error: (e) => this.fail(`计算失败：${e.error?.detail ?? e.message ?? e}`),
    });
  }

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
          this.refreshPlans();
          this.refreshEvidence();
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
      this.selectedPlan.set(null);
      this.replay.set(null);
      this.reloadAll();
      this.notice.set('已重置：阀态恢复初始；历史方案与证据保留（旧证据标记为过期）。');
    });
  }

  // ---------------------------------------------------------------- 模型阀态

  setModelState(valveId: string, isOpen: boolean): void {
    this.api.setValveState(valveId, isOpen, isOpen ? '培训：记录阀门开启' : '培训：记录阀门关闭到位').subscribe({
      next: () => {
        this.reloadAll();
        this.notice.set(`已在模型侧记录 ${valveId} = ${isOpen ? '打开' : '关闭'}（事件链留痕）。`);
      },
      error: (e) => this.fail(`阀态记录失败：${e.error?.detail ?? e.message ?? e}`),
    });
  }

  /** 便捷：把当前方案要求关闭的阀门一次性记录为模型关闭。 */
  recordPlanValvesClosed(): void {
    const plan = this.selectedPlan() ?? this.result()?.plan;
    if (!plan) {
      return;
    }
    let pending = plan.close_valves.length;
    if (pending === 0) {
      return;
    }
    for (const vid of plan.close_valves) {
      this.api.setValveState(vid, false, '培训：按方案记录关闭').subscribe({
        next: () => {
          pending -= 1;
          if (pending === 0) {
            this.reloadAll();
            this.refreshPlans(plan.plan_id);
          }
        },
        error: (e) => this.fail(`阀态记录失败：${e.error?.detail ?? e.message ?? e}`),
      });
    }
  }

  // ---------------------------------------------------------------- 证据提交

  updateForm<K extends keyof EvidenceForm>(key: K, value: EvidenceForm[K]): void {
    this.evidenceForm.update((f) => ({ ...f, [key]: value }));
  }

  currentPlanId(): number | null {
    return this.selectedPlan()?.plan_id ?? this.result()?.plan?.plan_id ?? null;
  }

  submitEvidence(): void {
    const form = this.evidenceForm();
    const body = {
      valve_id: form.valve_id,
      observed: form.observed,
      observed_at: form.observed_at ? new Date(form.observed_at).toISOString() : null,
      plan_id: this.currentPlanId(),
      observer: form.observer || null,
      note: form.note || null,
    };
    this.error.set(null);
    this.api.submitEvidence(body).subscribe({
      next: (res) => {
        const labels: Record<string, string> = {
          verified: '已核验（观察与模型一致）',
          contradiction: '矛盾——模型阀态未被改写，方案确认已被阻塞',
          stale: '过期/重复——当前核验状态不倒退',
          pending: '待核验（观察值未知）',
          resolved: '已复核结案',
        };
        this.notice.set(
          `证据 ${res.evidence_code} 已入链：${labels[res.assessment.status] ?? res.assessment.status}`,
        );
        this.evidenceForm.update((f) => ({ ...f, note: '', observed_at: '' }));
        this.refreshEvidence();
        this.refreshPlans(this.selectedPlan()?.plan_id);
        this.api.topology().subscribe((t) => this.topology.set(t));
      },
      error: (e) => this.fail(`证据提交失败：${e.error?.detail ?? e.message ?? e}`),
    });
  }

  // ---------------------------------------------------------------- 复核处置

  review(code: string, disposition: Disposition): void {
    const promptMap: Record<Disposition, string> = {
      correct_model: '以现场观察为准，纠正模型阀态（请填写复核说明）',
      confirm_model: '复核后维持模型、该观察不采信，阀门需重新核验',
      reinspect: '暂不结案，安排重新检查（阻塞保持）',
    };
    const note = prompt(promptMap[disposition]);
    if (note === null) {
      return;
    }
    this.api.reviewEvidence(code, disposition, { note, reviewer: '培训复核人' }).subscribe({
      next: () => {
        this.notice.set(`证据 ${code} 复核事件已追加（${disposition}）。`);
        this.reloadAll();
      },
      error: (e) =>
        this.fail(`复核失败：${typeof e.error?.detail === 'string' ? e.error.detail : e.message ?? e}`),
    });
  }

  // ---------------------------------------------------------------- 方案

  selectPlan(plan: PlanReview): void {
    this.api.plan(plan.plan_id).subscribe({
      next: (full) => {
        this.selectedPlan.set(full);
        this.result.set(full.result ?? null);
      },
      error: (e) => this.fail(`方案加载失败：${e.message ?? e}`),
    });
  }

  confirmPlan(plan: PlanReview): void {
    if (!confirm('确认仅为培训流程记录，不代表真实检修满足安全隔离条件（LOTO）。是否继续？')) {
      return;
    }
    this.api.confirmPlan(plan.plan_id, { confirmer: '培训确认人' }).subscribe({
      next: (res) => {
        this.notice.set(`方案 ${plan.plan_code} 已确认（事件 #${res.confirmed_seq}）。${res.warning ?? ''}`);
        this.refreshPlans(plan.plan_id);
      },
      error: (e) =>
        this.fail(`确认被拒绝：${typeof e.error?.detail === 'string' ? e.error.detail : e.message ?? e}`),
    });
  }

  toggleEvidenceDetail(code: string): void {
    this.expandedEvidence.set(this.expandedEvidence() === code ? null : code);
  }

  showReplay(code: string): void {
    this.api.replayEvidence(code).subscribe({
      next: (r) => this.replay.set(r),
      error: (e) => this.fail(`重放失败：${e.message ?? e}`),
    });
  }

  closeReplay(): void {
    this.replay.set(null);
  }

  toggleEvents(): void {
    this.showEventChain.set(!this.showEventChain());
    if (this.showEventChain()) {
      this.api.events().subscribe({
        next: (res) => {
          this.eventChain.set(res.events.map((e) => ({ seq: e.seq, event_type: e.event_type, model_revision: e.model_revision })));
          this.chainOk.set(res.chain.ok);
        },
      });
    }
  }

  // ---------------------------------------------------------------- 展示辅助

  valveName(id: string): string {
    return this.topology()?.valves.find((v) => v.id === id)?.name ?? id;
  }

  formatValves(valves: (string | null)[]): string {
    return valves.map((v) => v ?? '—').join('、');
  }

  joinIds(ids: string[]): string {
    return ids.join('、');
  }

  statusClass(status: VerificationStatus | string): string {
    return `st-${status}`;
  }

  statusLabel(status: VerificationStatus | string): string {
    const map: Record<string, string> = {
      verified: '已核验',
      pending: '待核验',
      stale: '过期',
      contradiction: '矛盾',
      resolved: '已复核结案',
      confirmable: '可确认',
      blocked: '被阻塞',
      outdated: '已过期',
      infeasible: '不可行',
      confirmed: '已确认',
    };
    return map[status] ?? status;
  }

  evidenceForValve(valveId: string): EvidenceRecord | null {
    const rows = this.evidenceList()?.evidence ?? [];
    const forValve = rows.filter((e) => e.valve_id === valveId);
    return forValve.length ? forValve[forValve.length - 1] : null;
  }

  planCount(): number {
    return this.plans().length;
  }
}

function prompt(text: string): string | null {
  // 简化交互：浏览器 prompt；培训页面使用
  return window.prompt(text, '') ?? null;
}
