"""阀门核验证据：持久化事件链、证据状态推导、矛盾处置与受影响残余路径。

设计边界（培训演示）：

- 证据只是“观察记录”，**永不自动改写模型阀态**；模型只能被显式的
  复核/纠正事件（review_completed）或显式阀门操作事件改变。
- 迟到/重复观察（observed_at 不晚于该阀当前有效证据）只存档、不生效，
  当前状态不会因此倒退。
- 拓扑/阀态一经改变（topology_version 递增），旧证据即过期，原始记录保留。
- 矛盾证据建立“待处置”状态，阻塞依赖该阀的隔离方案被确认，
  直到显式复核/纠正事件完成。
- 以上全部是演示工作流状态，不代表真实安全确认。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation
from .models import Evidence, EventLog, IsolationPlan, MetaState, Valve

OBSERVED_VALUES = ("open", "closed", "unknown")

# 证据显示状态（推导得出，不落库）
STATUS_LABELS = {
    "verified": "已核验",
    "pending": "待核验",
    "expired": "已过期",
    "contradiction": "存在矛盾",
    "superseded": "未生效（迟到/重复/被取代）",
    "resolved_corrected": "已处置·采纳观察并纠正模型",
    "resolved_dismissed": "已处置·驳回观察",
}

# 方案显示状态（推导得出，不落库）
PLAN_STATUS_LABELS = {
    "confirmable": "可确认",
    "blocked": "被阻塞（存在矛盾核验）",
    "stale": "已过期（模型已变更）",
    "confirmed": "已确认",
    "infeasible": "无可行方案",
}


class VerificationError(RuntimeError):
    """核验工作流冲突（如无可处置矛盾、确认被阻塞）。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------- 拓扑版本与事件链 ----------------

def get_topology_version(db: Session) -> int:
    row = db.get(MetaState, "topology_version")
    if row is None:
        row = MetaState(key="topology_version", value="1")
        db.add(row)
        db.flush()
    return int(row.value)


def bump_topology_version(db: Session) -> int:
    version = get_topology_version(db) + 1
    row = db.get(MetaState, "topology_version")
    row.value = str(version)  # type: ignore[union-attr]
    db.flush()
    return version


def emit_event(db: Session, type_: str, payload: dict[str, Any]) -> EventLog:
    ev = EventLog(type=type_, payload=payload, created_at=_utcnow())
    db.add(ev)
    db.flush()  # 分配自增 seq（提交序号）
    return ev


def topology_snapshot(db: Session) -> dict[str, Any]:
    """当前拓扑快照：拓扑版本 + 全部阀门的模型开闭/锁定状态。"""
    _, edges = isolation._load(db)
    return {
        "topology_version": get_topology_version(db),
        "valves": {
            e["valve_id"]: {"is_open": e["is_open"], "locked": e["locked"]}
            for e in edges.values()
            if e["valve_id"]
        },
    }


# ---------------- 证据记录与状态推导 ----------------

def _effective_by_valve(db: Session) -> dict[str, list[Evidence]]:
    out: dict[str, list[Evidence]] = {}
    for ev in db.scalars(select(Evidence).order_by(Evidence.seq)).all():
        if ev.effective:
            out.setdefault(ev.valve_id, []).append(ev)
    return out


def current_evidence(db: Session) -> dict[str, Evidence]:
    """每只阀门的当前有效证据 = 生效证据中 (observed_at, seq) 最大者。"""
    return {
        vid: max(evs, key=lambda e: (e.observed_at, e.seq))
        for vid, evs in _effective_by_valve(db).items()
    }


def record_evidence(
    db: Session,
    valve: Valve,
    observed: str,
    observed_at: datetime,
    plan_id: str | None,
    note: str,
) -> Evidence:
    """追加一条核验证据。迟到/重复观察（observed_at 不晚于当前有效证据）
    只存档不生效，绝不覆盖较新的证据。"""
    if observed not in OBSERVED_VALUES:
        raise VerificationError(f"非法观察值: {observed}")
    current = current_evidence(db).get(valve.id)
    effective = current is None or observed_at > current.observed_at

    version = get_topology_version(db)
    event = emit_event(
        db,
        "evidence_submitted",
        {
            "valve_id": valve.id,
            "observed": observed,
            "observed_at": observed_at.isoformat(),
            "plan_id": plan_id,
            "effective": effective,
            "topology_version": version,
        },
    )
    ev = Evidence(
        id=f"EV-{event.seq:04d}",
        seq=event.seq,
        valve_id=valve.id,
        observed=observed,
        observed_at=observed_at,
        plan_id=plan_id,
        topology_version=version,
        model_is_open=valve.is_open,
        model_locked=valve.locked,
        snapshot=topology_snapshot(db),
        note=note,
        effective=effective,
        created_at=_utcnow(),
    )
    db.add(ev)
    db.flush()
    event.payload = {**event.payload, "evidence_id": ev.id}
    db.flush()
    return ev


def evidence_status(
    ev: Evidence,
    valve: Valve,
    current_version: int,
    current_by_valve: dict[str, Evidence],
) -> str:
    """推导证据当前显示状态（与当前模型阀态/锁定及拓扑版本对比）。"""
    if ev.resolution == "corrected":
        return "resolved_corrected"
    if ev.resolution == "dismissed":
        return "resolved_dismissed"
    if not ev.effective:
        return "superseded"  # 迟到/重复，提交时即未生效
    current = current_by_valve.get(ev.valve_id)
    if current is not None and current.id != ev.id:
        return "superseded"  # 已被更新的有效证据取代
    if (
        current_version != ev.topology_version
        or valve.is_open != ev.model_is_open
        or valve.locked != ev.model_locked
    ):
        return "expired"  # 拓扑或阀态后续改变，原始记录保留但不再适用
    if ev.observed == "unknown":
        return "pending"  # 观察值未知，无法比对，待核验
    matches = (ev.observed == "open") == valve.is_open
    return "verified" if matches else "contradiction"


def evidence_payload(db: Session, ev: Evidence) -> dict[str, Any]:
    valve = db.get(Valve, ev.valve_id)
    current = current_evidence(db)
    status = evidence_status(ev, valve, get_topology_version(db), current)  # type: ignore[arg-type]
    return {
        "id": ev.id,
        "seq": ev.seq,
        "valve_id": ev.valve_id,
        "observed": ev.observed,
        "observed_at": ev.observed_at.isoformat(),
        "plan_id": ev.plan_id,
        "topology_version": ev.topology_version,
        "model_is_open": ev.model_is_open,
        "model_locked": ev.model_locked,
        "effective": ev.effective,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "resolution": ev.resolution,
        "resolved_seq": ev.resolved_seq,
        "note": ev.note,
        "snapshot": ev.snapshot,
        "created_at": ev.created_at.isoformat(),
    }


# ---------------- 矛盾处置（待处置状态） ----------------

def affected_residual_path(db: Session, valve_id: str) -> dict[str, Any] | None:
    """若按观察值把该阀视为开启，来源仍能到达目标设备的一条残余路径。

    仅用于“观察到开启而模型认为关闭”的矛盾：模型因该阀关闭而切断的
    连通实际上可能仍然存在，把这条受影响残余路径标出来供培训演示。
    """
    nodes, edges = isolation._load(db)
    sources = sorted(n.id for n in nodes if n.kind == "source")
    targets = sorted(n.id for n in nodes if n.kind == "equipment")
    seg = next((e for e in edges.values() if e["valve_id"] == valve_id), None)
    if not sources or not targets or seg is None:
        return None
    target = targets[0]

    g = nx.Graph()
    for e in edges.values():
        g.add_node(e["u"])
        g.add_node(e["v"])
        if not e["is_open"] and e["valve_id"] != valve_id:
            continue  # 其余模型关闭阀保持断开；争议阀按观察强制连通
        g.add_edge(e["u"], e["v"], edge_id=e["id"], valve_id=e["valve_id"])

    u, v = seg["u"], seg["v"]
    best: list[str] | None = None
    for s in sources:
        for h1, h2 in ((u, v), (v, u)):
            if h1 in g and h2 in g and nx.has_path(g, s, h1) and nx.has_path(g, h2, target):
                cand = nx.shortest_path(g, s, h1) + nx.shortest_path(g, h2, target)
                if best is None or len(cand) < len(best):
                    best = cand
    if best is None:
        return None
    cleaned: list[str] = []
    for n in best:
        if not cleaned or cleaned[-1] != n:
            cleaned.append(n)
    return {
        "nodes": cleaned,
        "valves": isolation._path_valves(edges, cleaned),
        "through_valve": valve_id,
    }


def valve_dispositions(db: Session) -> list[dict[str, Any]]:
    """当前所有“待处置”矛盾：当前有效证据与模型阀态冲突且未经复核。"""
    version = get_topology_version(db)
    valves = {v.id: v for v in db.scalars(select(Valve)).all()}
    current = current_evidence(db)
    out: list[dict[str, Any]] = []
    for vid, ev in sorted(current.items()):
        valve = valves.get(vid)
        if valve is None:
            continue
        if evidence_status(ev, valve, version, current) != "contradiction":
            continue
        affected = None
        if ev.observed == "open" and not valve.is_open:
            affected = affected_residual_path(db, vid)
        out.append(
            {
                "valve_id": vid,
                "evidence_id": ev.id,
                "observed": ev.observed,
                "model_is_open": valve.is_open,
                "model_locked": valve.locked,
                "observed_at": ev.observed_at.isoformat(),
                "affected_residual_path": affected,
            }
        )
    return out


def review_valve(db: Session, valve: Valve, action: str, note: str) -> dict[str, Any]:
    """显式复核/纠正事件：处置该阀当前有效证据的矛盾。

    correct_model: 采纳观察，把模型阀态纠正为观察值（这是唯一会改变
                   模型阀态的复核路径，且它是显式事件而非偷偷改写）。
    dismiss:       驳回观察，模型保持不变。
    """
    version = get_topology_version(db)
    current = current_evidence(db)
    ev = current.get(valve.id)
    if ev is None or evidence_status(ev, valve, version, current) != "contradiction":
        raise VerificationError(f"阀门 {valve.id} 当前没有待处置的矛盾证据")

    if action == "correct_model":
        if ev.observed == "unknown":
            raise VerificationError("观察值为未知，无法据此纠正模型")
        valve.is_open = ev.observed == "open"
        ev.resolution = "corrected"
        new_version = bump_topology_version(db)
    elif action == "dismiss":
        ev.resolution = "dismissed"
        new_version = version
    else:
        raise VerificationError(f"非法复核动作: {action}")

    event = emit_event(
        db,
        "review_completed",
        {
            "valve_id": valve.id,
            "evidence_id": ev.id,
            "action": action,
            "observed": ev.observed,
            "model_is_open": valve.is_open,
            "note": note,
            "topology_version": new_version,
        },
    )
    ev.resolved_seq = event.seq
    db.flush()
    return {
        "valve_id": valve.id,
        "evidence_id": ev.id,
        "action": action,
        "model_is_open": valve.is_open,
        "resolved_seq": event.seq,
        "topology_version": new_version,
    }


# ---------------- 方案确认门禁 ----------------

def plan_depends_valves(plan: IsolationPlan) -> set[str]:
    """方案结论依赖的阀门：候选关闭集合 + 计算时模型已关闭的阀门。"""
    return set(plan.close_valves or []) | set(plan.assumed_closed_valves or [])


def plan_status(
    plan: IsolationPlan, current_version: int, disputed_valves: set[str]
) -> tuple[str, list[str]]:
    """推导方案状态与阻塞它的阀门列表。"""
    if plan.confirmed_seq is not None:
        return "confirmed", []
    if plan.topology_version != current_version:
        return "stale", []
    if not plan.feasible:
        return "infeasible", []
    blocked = sorted(plan_depends_valves(plan) & disputed_valves)
    if blocked:
        return "blocked", blocked
    return "confirmable", []


def plan_summary_payload(
    plan: IsolationPlan, current_version: int, disputed_valves: set[str]
) -> dict[str, Any]:
    status, blocked_by = plan_status(plan, current_version, disputed_valves)
    return {
        "id": plan.id,
        "seq": plan.seq,
        "target_id": plan.target_id,
        "feasible": plan.feasible,
        "topology_version": plan.topology_version,
        "close_valves": plan.close_valves,
        "assumed_closed_valves": plan.assumed_closed_valves,
        "status": status,
        "status_label": PLAN_STATUS_LABELS[status],
        "blocked_by": blocked_by,
        "confirmed_seq": plan.confirmed_seq,
        "created_at": plan.created_at.isoformat(),
    }
