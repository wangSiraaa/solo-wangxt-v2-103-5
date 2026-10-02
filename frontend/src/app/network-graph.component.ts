import {
  AfterViewInit,
  Component,
  ElementRef,
  Input,
  OnChanges,
  OnDestroy,
  ViewChild,
} from '@angular/core';
import cytoscape from 'cytoscape';
import {
  AffectedResidualPath,
  IsolationResult,
  PlanReview,
  Topology,
  VerificationStatus,
} from './models';

/**
 * Cytoscape.js 拓扑渲染：
 * - 节点：来源 / 目标设备 / 必要供给点 / 普通节点
 * - 边：管段（按保存的名义方向显示箭头），旁路使用虚线
 * - 高亮：
 *   候选关闭阀（红）、方案后供给路径（青）、残余/绕回路径（橙）
 *   核验状态：已核验（绿描边）/ 待核验（灰虚）/ 过期（黄）/ 矛盾（红闪烁粗边）
 *   受矛盾影响的残余路径（品红粗线）
 */
@Component({
  selector: 'app-network-graph',
  standalone: true,
  template: `<div #cy class="cy"></div>`,
  styles: [
    `
      .cy {
        width: 100%;
        height: 560px;
        background: #0f172a;
        border-radius: 10px;
      }
    `,
  ],
})
export class NetworkGraphComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() topology: Topology | null = null;
  @Input() result: IsolationResult | null = null;
  /** 当前查看的方案复核（含矛盾影响路径），可选 */
  @Input() planReview: PlanReview | null = null;

  @ViewChild('cy') cyHost!: ElementRef<HTMLDivElement>;

  private cy: cytoscape.Core | null = null;

  ngAfterViewInit(): void {
    this.render();
  }

  ngOnChanges(): void {
    if (this.cy) {
      this.render();
    }
  }

  ngOnDestroy(): void {
    this.cy?.destroy();
    this.cy = null;
  }

  private verificationClass(status: VerificationStatus | string | undefined): string {
    switch (status) {
      case 'verified':
        return 'v-verified';
      case 'pending':
        return 'v-pending';
      case 'stale':
        return 'v-stale';
      case 'contradiction':
        return 'v-contradiction';
      case 'resolved':
        return 'v-resolved';
      default:
        return 'v-pending';
    }
  }

  private render(): void {
    if (!this.topology) {
      return;
    }
    const topo = this.topology;

    const elements: cytoscape.ElementDefinition[] = [
      ...topo.nodes.map((n) => ({
        data: {
          id: n.id,
          label: `${n.name}${n.essential ? ' ★' : ''}`,
          kind: n.kind,
          essential: n.essential,
        },
        position: { x: n.x, y: n.y },
      })),
      ...topo.segments.map((s) => {
        const valve = topo.valves.find((v) => v.id === s.valve_id);
        return {
          data: {
            id: s.id,
            source: s.source,
            target: s.target,
            label: s.valve_id ?? '',
            bypass: s.is_bypass,
            vstatus: valve?.verification?.status ?? 'pending',
            observed: valve?.verification?.observed_open,
          },
        };
      }),
    ];

    if (this.cy) {
      this.cy.destroy();
    }
    this.cy = cytoscape({
      container: this.cyHost.nativeElement,
      elements,
      zoomingEnabled: true,
      userZoomingEnabled: true,
      panningEnabled: true,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': '#94a3b8',
            label: 'data(label)',
            color: '#e2e8f0',
            'font-size': '12px',
            'text-valign': 'bottom',
            'text-margin-y': 6,
            width: 26,
            height: 26,
            'border-width': 2,
            'border-color': '#cbd5e1',
          },
        },
        {
          selector: 'node[kind = "source"]',
          style: { 'background-color': '#16a34a', 'border-color': '#86efac' },
        },
        {
          selector: 'node[kind = "equipment"]',
          style: {
            'background-color': '#f59e0b',
            'border-color': '#fcd34d',
            width: 34,
            height: 34,
          },
        },
        {
          selector: 'node[essential = true]',
          style: { 'background-color': '#0284c7', 'border-color': '#7dd3fc' },
        },
        {
          selector: 'node.hl-target',
          style: { 'border-color': '#f8fafc', 'border-width': 4 },
        },
        {
          selector: 'edge',
          style: {
            width: 2.5,
            'line-color': '#64748b',
            'target-arrow-color': '#64748b',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            label: 'data(label)',
            'font-size': '10px',
            color: '#cbd5e1',
            'text-background-color': '#0f172a',
            'text-background-opacity': 0.85,
            'text-background-padding': '2px',
          },
        },
        {
          selector: 'edge[bypass = true]',
          style: {
            'line-style': 'dashed',
            'line-color': '#a78bfa',
            'target-arrow-color': '#a78bfa',
          },
        },
        // ---- 核验状态（作用在阀门所在边）----
        {
          selector: 'edge.v-verified',
          style: {
            'target-arrow-color': '#4ade80',
            'line-color': '#4ade80',
            width: 3.5,
            label: (el: cytoscape.EdgeSingular) => `${el.data('label')} ✓`,
            color: '#bbf7d0',
          },
        },
        {
          selector: 'edge.v-pending',
          style: { 'line-style': 'dashed', 'line-dash-pattern': [4, 4] },
        },
        {
          selector: 'edge.v-stale',
          style: {
            'target-arrow-color': '#facc15',
            'line-color': '#ca8a04',
            label: (el: cytoscape.EdgeSingular) => `${el.data('label')} ⧗`,
            color: '#fde68a',
          },
        },
        {
          selector: 'edge.v-contradiction',
          style: {
            'line-color': '#ef4444',
            'target-arrow-color': '#ef4444',
            width: 6,
            label: (el: cytoscape.EdgeSingular) => `${el.data('label')} ⚠`,
            color: '#fecaca',
          },
        },
        {
          selector: 'edge.v-resolved',
          style: {
            'target-arrow-color': '#38bdf8',
            'line-color': '#0369a1',
            width: 3.5,
            label: (el: cytoscape.EdgeSingular) => `${el.data('label')} ⚑`,
            color: '#bae6fd',
          },
        },
        // ---- 方案/路径高亮 ----
        {
          selector: 'edge.close',
          style: {
            'line-color': '#ef4444',
            'target-arrow-color': '#ef4444',
            width: 5,
            color: '#fca5a5',
          },
        },
        {
          selector: 'edge.residual',
          style: {
            'line-color': '#fb923c',
            'target-arrow-color': '#fb923c',
            width: 4,
            'line-style': 'dotted',
          },
        },
        {
          selector: 'edge.witness',
          style: {
            'line-color': '#f87171',
            'target-arrow-color': '#f87171',
            width: 4,
          },
        },
        {
          selector: 'edge.supply',
          style: {
            'line-color': '#22d3ee',
            'target-arrow-color': '#22d3ee',
            width: 4,
          },
        },
        {
          selector: 'edge.affected',
          style: {
            'line-color': '#f472b6',
            'target-arrow-color': '#f472b6',
            width: 6,
            'line-style': 'solid',
          },
        },
        {
          selector: 'node.onpath',
          style: { 'border-color': '#67e8f9', 'border-width': 3 },
        },
        {
          selector: 'node.affected-node',
          style: { 'border-color': '#f472b6', 'border-width': 4 },
        },
      ] as cytoscape.StylesheetJsonBlock[],
      layout: { name: 'preset' },
    });

    this.applyVerificationClasses();
    this.applyHighlights();
    this.cy!.fit(undefined, 40);
  }

  private edgeByValve(): Map<string, string> {
    const map = new Map<string, string>();
    for (const s of this.topology?.segments ?? []) {
      if (s.valve_id) {
        map.set(s.valve_id, s.id);
      }
    }
    return map;
  }

  /** 按当前拓扑中的核验状态给所有阀门边上底色类（拓扑页标记）。 */
  private applyVerificationClasses(): void {
    const cy = this.cy;
    const topo = this.topology;
    if (!cy || !topo) {
      return;
    }
    const edgeByValve = this.edgeByValve();
    for (const v of topo.valves) {
      const eid = edgeByValve.get(v.id);
      if (!eid) {
        continue;
      }
      const status = v.verification?.status ?? 'pending';
      cy.$(`edge#${eid}`).addClass(this.verificationClass(status));
    }
  }

  private markNodePath(nodePath: string[], edgeCls: string, nodeCls?: string): void {
    const cy = this.cy;
    if (!cy) {
      return;
    }
    nodePath.forEach((n) => {
      cy.$(`node#${n}`).addClass('onpath');
      if (nodeCls) {
        cy.$(`node#${n}`).addClass(nodeCls);
      }
    });
    for (let i = 0; i < nodePath.length - 1; i++) {
      cy.edges(`[source = "${nodePath[i]}"][target = "${nodePath[i + 1]}"]`).addClass(edgeCls);
      cy.edges(`[source = "${nodePath[i + 1]}"][target = "${nodePath[i]}"]`).addClass(edgeCls);
    }
  }

  private applyHighlights(): void {
    const cy = this.cy;
    const result = this.result;
    const topo = this.topology;
    if (!cy || !topo) {
      return;
    }
    const edgeByValve = this.edgeByValve();

    if (result) {
      cy.$(`node#${result.target_id}`).addClass('hl-target');

      if (result.feasible && result.solutions.length) {
        const best = result.solutions[0];
        for (const vid of best.close_valves) {
          const eid = edgeByValve.get(vid);
          if (eid) {
            cy.$(`edge#${eid}`).addClass('close');
          }
        }
        for (const path of Object.values(best.supply_paths)) {
          if (path) {
            this.markNodePath(path, 'supply');
          }
        }
      } else if (!result.feasible) {
        if (result.residual_path) {
          this.markNodePath(result.residual_path.nodes, 'residual');
        }
        if (result.locked_witness_path) {
          this.markNodePath(result.locked_witness_path.nodes, 'witness');
        }
      }
    }

    // 方案复核：矛盾观察覆盖后的受影响残余路径（品红）
    const review: PlanReview | null = this.planReview ?? result?.plan ?? null;
    if (review && review.affected_residual_paths?.paths?.length) {
      const paths: AffectedResidualPath[] = review.affected_residual_paths.paths.slice(0, 3);
      for (const p of paths) {
        this.markNodePath(p.nodes, 'affected', 'affected-node');
      }
    }
  }
}
