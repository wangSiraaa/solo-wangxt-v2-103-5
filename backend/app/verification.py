"""阀门核验证据：评估、门禁复核与版本化重放。

四类显示状态（培训术语，*不是*真实安全确认）：

- ``verified``     已核验：证据观察值与其提交时刻模型阀态一致，且至今未被取代/过期
- ``pending``      待核验：尚无有效证据，或观察值为“未知”，或矛盾结案后需重新核验
- ``stale``        过期：证据之后模型/阀态/锁定发生过变更、拓扑指纹变化，
                     或已被同一阀门更晚（按现场发生时刻）的证据取代/重复
- ``contradiction`` 矛盾：观察值与模型阀态不一致且未经显式复核处置

核心纪律：

1. 矛盾证据**绝不**改写模型阀态；模型阀态只能经事件链中的
   valve_state_changed / review(correct_model) / model_reset 改变。
2. 未处置的矛盾使依赖该阀的方案不可确认；NetworkX 用“观察覆盖”重算
   受影响残余路径，全程不触碰模型。
3. 迟到/重复观察按 (observed_at, submitted_seq) 排序，永不覆盖较新证据。
4. 复核/纠正事件完成前，“待处置”状态一直挂起；结案只追加事件，旧记录保留。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import events, isolation
from .events import EVIDENCE_EVAL_VERSION, canonical_json
from .models import EventLog, IsolationPlan, Node, Valve, ValveEvidence

OBS_OPEN = "open"
OBS_CLOSED = "closed"
OBS_UNKNOWN = "unknown"
OBS_VALUES = (OBS_OPEN, OBS_CLOSED, OBS_UNKNOWN)

STATUS_VERIFIED = "verified"
STATUS_PENDING = "pending"
STATUS_STALE = "stale"
STATUS_CONTRADICTION = "contradiction"
# 矛盾经“维持模型”复核结案（非纠正）：矛盾解除，但不构成对阀态的核验
STATUS_RESOLVED = "resolved"

STATUS_LABELS = {
    STATUS_VERIFIED: "已核验",
    STATUS_PENDING: "待核验",
    STATUS_STALE: "过期",
    STATUS_CONTRADICTION: "矛盾",
    STATUS_RESOLVED: "已复核结案",
}

DISPOSITION_CORRECT = "correct_model"   # 以现场观察为准，纠正模型
DISPOSITION_CONFIRM = "confirm_model"   # 复核维持模型，观察不采信（需重新核验）
DISPOSITION_REINSPECT = "reinspect"     # 暂不结案，安排重新检查（阻塞保持）
DISPOSITIONS = (DISPOSITION_CORRECT, DISPOSITION_CONFIRM, DISPOSITION_REINSPECT)


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------


def observed_to_bool(value: str) -> bool | None:
    if value == OBS_OPEN:
        return True
    if value == OBS_CLOSED:
        return False
    return None


def _load_snapshot(ev: ValveEvidence) -> dict[str, Any]:
    return json.loads(ev.topology_snapshot_json)


def _snapshot_valves(ev: ValveEvidence) -> dict[str, dict[str, Any]]:
    snap = _load_snapshot(ev)
    return {v["id"]: v for v in snap["valves"]}


def all_evidence(db: Session) -> list[ValveEvidence]:
    # 长生命周期 Session（如 TestClient/请求内多次提交）中，ORM identity map
    # 可能缓存了“查询时证据表为空”的结果；expire 后强制回库读取新证据。
    db.expire_all()
    return list(db.scalars(select(ValveEvidence).order_by(ValveEvidence.id.asc())).all())


def reviews_for(db: Session, evidence_code: str | None = None) -> list[dict[str, Any]]:
    """读取复核事件（按链序）。可选只取某条证据的。"""
    out: list[dict[str, Any]] = []
    stmt = select(EventLog).where(EventLog.event_type == "review_completed").order_by(EventLog.seq.asc())
    for ev in db.scalars(stmt).all():
        p = json.loads(ev.payload_json)
        if evidence_code is not None and p.get("evidence_code") != evidence_code:
            continue
        out.append(
            {
                "seq": ev.seq,
                "occurred_at": ev.occurred_at,
                "evidence_code": p.get("evidence_code"),
                "valve_id": p.get("valve_id"),
                "disposition": p.get("disposition"),
                "note": p.get("note"),
                "set_is_open": p.get("set_is_open"),
                "model_revision": ev.model_revision,
            }
        )
    return out


def _resolving_review(db: Session, ev: ValveEvidence) -> dict[str, Any] | None:
    """该证据是否已被一次“结案型”复核处置（纠正模型 / 维持模型）。

    reinspect 只是安排重新检查，不解除待处置状态。
    """
    for r in reviews_for(db, ev.evidence_code):
        if r["disposition"] in (DISPOSITION_CORRECT, DISPOSITION_CONFIRM):
            return r
    return None


def _current_valves(db: Session) -> dict[str, Valve]:
    return {v.id: v for v in db.scalars(select(Valve)).all()}


def _snapshot_structural_fingerprint(snapshot: dict[str, Any]) -> str:
    """从证据快照 JSON 计算结构指纹（不依赖提交之后的任何数据）。"""
    material = {
        "nodes": sorted(n["id"] for n in snapshot["nodes"]),
        "segments": sorted(
            (
                s["id"],
                tuple(sorted((s["source"], s["target"]))),
                s["kind"],
                s["is_bypass"],
                s["valve_id"],
            )
            for s in snapshot["segments"]
        ),
    }
    import hashlib

    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def _order_key(ev: ValveEvidence) -> tuple[str, int]:
    return (ev.observed_at, ev.submitted_seq)


def latest_evidence_by_valve(
    db: Session, *, evidence: list[ValveEvidence] | None = None
) -> dict[str, ValveEvidence]:
    """每只阀门“当前有效”的最新证据——迟到/重复观察不会胜出。

    规则（按现场发生时刻 observed_at 为主序）：
    - 取该阀最新的“现场观察时刻”；
    - 该时刻下可能有多条（同值重复、或冲突值先后补录）：先按
      (值相同的最早提交者) 去重，再在不同值之间取提交序号最大者；
    - 该记录本身若因模型/拓扑变更而过期，那是*状态*层面的过期，
      仍是“最新证据”（页面据此显示“过期”），不会回退到更早的观察。
    """
    evidence = evidence if evidence is not None else all_evidence(db)
    by_valve: dict[str, list[ValveEvidence]] = {}
    for ev in evidence:
        by_valve.setdefault(ev.valve_id, []).append(ev)

    result: dict[str, ValveEvidence] = {}
    for valve_id, rows in by_valve.items():
        latest_at = max(e.observed_at for e in rows)
        at_latest = [e for e in rows if e.observed_at == latest_at]

        # 同值重复：保留最早提交的一条（原始记录代表该观察）
        by_value: dict[bool | None, list[ValveEvidence]] = {}
        for ev in at_latest:
            by_value.setdefault(ev.observed_open, []).append(ev)
        representatives = [min(group, key=lambda e: e.submitted_seq) for group in by_value.values()]
        result[valve_id] = max(representatives, key=lambda e: e.submitted_seq)
    return result


def valve_last_model_change_seq(db: Session, valve_id: str) -> int | None:
    """该阀门最后一次“模型侧变更”（阀态/锁定/重置/纠正）的事件序号。

    其他阀门的变更不应使本阀证据过期，因此按阀门分别追踪。
    """
    last: int | None = None
    for ev in events.list_events(db):
        p = json.loads(ev.payload_json)
        if ev.event_type == "model_reset" and valve_id in p.get("valve_ids", []):
            last = ev.seq
        elif ev.event_type in ("valve_state_changed", "valve_lock_changed"):
            if p.get("valve_id") == valve_id:
                last = ev.seq
        elif ev.event_type == "review_completed":
            if p.get("valve_id") == valve_id and p.get("disposition") == DISPOSITION_CORRECT:
                last = ev.seq
    return last


# ---------------------------------------------------------------------------
# 单条证据评估
# ---------------------------------------------------------------------------


@dataclass
class EvalContext:
    """评估上下文：当前阀态/版本/指纹与同阀证据集合，可切换为历史重放。"""

    valves: dict[str, Any]
    model_revision: int
    topology_fingerprint: str
    same_valve: list[ValveEvidence]
    current_seq: int | None = None  # 重放时：只考虑 submitted_seq <= current_seq 的证据


def _is_dup_or_superseded(ev: ValveEvidence, ctx: EvalContext) -> tuple[bool, str | None]:
    """返回 (是否过期, 原因)。重复=同值同时刻的晚到副本；被取代=存在更晚的观察。"""
    prior_same: list[ValveEvidence] = []
    for other in ctx.same_valve:
        if other.id == ev.id:
            continue
        if ctx.current_seq is not None and other.submitted_seq > ctx.current_seq:
            continue
        if other.submitted_seq < ev.submitted_seq:
            prior_same.append(other)

    # 重复：更早提交过同一现场时刻 + 同一观察值（无论它本身后来是否被取代）
    for other in prior_same:
        if other.observed_at == ev.observed_at and other.observed_open == ev.observed_open:
            return True, (
                f"重复观察：{ev.observed_at} 的“{_obs_label(ev.observed_open)}”"
                f"已由证据 {other.evidence_code} 先行记录，本条不覆盖任何状态"
            )
    # 被取代（两种）：
    #  a) 存在严格更晚现场时刻的观察（迟到观察不覆盖）；
    #  b) 同一现场时刻先后补录了*不同*观察值，以提交序号决胜。
    #  同值的重复副本不算取代者（原始观察不因重复记录而失效）。
    newer: list[ValveEvidence] = []
    for other in ctx.same_valve:
        if other.id == ev.id:
            continue
        if ctx.current_seq is not None and other.submitted_seq > ctx.current_seq:
            continue
        if other.observed_open == ev.observed_open:
            continue
        if (other.observed_at, other.submitted_seq) > (ev.observed_at, ev.submitted_seq):
            newer.append(other)
    if newer:
        newer.sort(key=lambda e: (e.observed_at, e.submitted_seq))
        n = newer[-1]
        return True, (
            f"已被更晚的现场观察取代：{n.evidence_code}（{n.observed_at}，"
            f"{_obs_label(n.observed_open)}）；迟到观察不回退当前核验状态"
        )
    return False, None


def _obs_label(observed_open: bool | None) -> str:
    return {True: "开", False: "关", None: "未知"}[observed_open]


def evaluate_evidence(
    db: Session,
    ev: ValveEvidence,
    *,
    at_submission: bool = False,
) -> dict[str, Any]:
    """评估一条证据。at_submission=True 时按其提交时刻的模型与已知证据解释（重放）。"""
    if at_submission:
        snap = _load_snapshot(ev)
        ctx = EvalContext(
            valves={v["id"]: v for v in snap["valves"]},
            model_revision=ev.model_revision,
            topology_fingerprint=ev.topology_fingerprint,
            same_valve=[e for e in all_evidence(db) if e.valve_id == ev.valve_id],
            current_seq=ev.submitted_seq,
        )
        revision_now = ev.model_revision
        fingerprint_now = ev.topology_fingerprint
        resolving = None  # 提交时刻不可能已有针对它的复核
    else:
        current = _current_valves(db)
        nodes = list(db.scalars(select(Node)).all())
        _, edges = isolation._load(db)
        ctx = EvalContext(
            valves=current,
            model_revision=events.current_revision(db),
            topology_fingerprint=isolation.structural_fingerprint(nodes, edges),
            same_valve=[e for e in all_evidence(db) if e.valve_id == ev.valve_id],
        )
        revision_now = ctx.model_revision
        fingerprint_now = ctx.topology_fingerprint
        resolving = _resolving_review(db, ev)

    base: dict[str, Any] = {
        "evidence_code": ev.evidence_code,
        "valve_id": ev.valve_id,
        "observed_open": ev.observed_open,
        "observed_label": _obs_label(ev.observed_open),
        "observed_at": ev.observed_at,
        "submitted_seq": ev.submitted_seq,
        "model_revision_at_evidence": ev.model_revision,
        "model_revision_now": revision_now,
        "eval_version": EVIDENCE_EVAL_VERSION,
        "plan_id": ev.plan_id,
        "observer": ev.observer,
        "note": ev.note,
        "resolution": None,
    }

    model_valve = ctx.valves.get(ev.valve_id)
    if model_valve is None:
        return {
            **base,
            "status": STATUS_STALE,
            "status_label": STATUS_LABELS[STATUS_STALE],
            "explanation": "阀门已不在当前拓扑中，证据仅作历史保留。",
            "stale_reason": "valve_missing",
        }
    model_open = model_valve["is_open"] if isinstance(model_valve, dict) else model_valve.is_open
    model_locked = model_valve["locked"] if isinstance(model_valve, dict) else model_valve.locked

    # 1) 显式复核/纠正事件优先：待处置状态已有人负责结案
    if resolving is not None:
        if resolving["disposition"] == DISPOSITION_CORRECT:
            return {
                **base,
                "status": STATUS_VERIFIED,
                "status_label": STATUS_LABELS[STATUS_VERIFIED],
                "explanation": (
                    f"矛盾已经复核结案：以现场观察“{_obs_label(ev.observed_open)}”为准，"
                    f"模型阀态已在事件 #{resolving['seq']} 纠正（版本→{resolving['model_revision']}）。"
                ),
                "resolved_by": resolving,
                "was_contradiction": True,
            }
        return {
            **base,
            "status": STATUS_RESOLVED,
            "status_label": STATUS_LABELS[STATUS_RESOLVED],
            "explanation": (
                f"矛盾已经复核结案：维持模型阀态（{'开' if model_open else '关'}），"
                f"该观察不采信；事件 #{resolving['seq']}。该阀仍需重新核验后方案方可确认。"
            ),
            "resolved_by": resolving,
            "was_contradiction": True,
        }

    # 2) 重复 / 被更晚观察取代（迟到、重复永不覆盖较新证据）
    stale, stale_msg = _is_dup_or_superseded(ev, ctx)
    if stale:
        return {
            **base,
            "status": STATUS_STALE,
            "status_label": STATUS_LABELS[STATUS_STALE],
            "explanation": stale_msg,
            "stale_reason": "duplicate" if "重复" in (stale_msg or "") else "superseded",
            "model_valve_open": model_open,
            "model_valve_locked": model_locked,
        }

    # 3) 提交之后*该阀门*的模型态发生变更（其他阀门的操作不使其过期）
    #    → 旧证据过期但保留原始记录
    if not at_submission:
        change_seq = valve_last_model_change_seq(db, ev.valve_id)
        if change_seq is not None and change_seq > ev.submitted_seq:
            return {
                **base,
                "status": STATUS_STALE,
                "status_label": STATUS_LABELS[STATUS_STALE],
                "explanation": (
                    f"证据之后该阀门模型态发生变更（变更事件 #{change_seq}），"
                    "原始观察已过期，仅作历史保留；需要按新模型重新核验。"
                ),
                "stale_reason": "valve_model_changed",
                "model_valve_open": model_open,
                "model_valve_locked": model_locked,
            }

    # 4) 拓扑结构变化（管段连接/拓扑本身改变）
    if not at_submission:
        snap = _load_snapshot(ev)
        if _snapshot_structural_fingerprint(snap) != fingerprint_now:
            return {
                **base,
                "status": STATUS_STALE,
                "status_label": STATUS_LABELS[STATUS_STALE],
                "explanation": "证据提交后管网拓扑结构发生改变，快照与当前拓扑不一致；证据过期保留。",
                "stale_reason": "topology_changed",
                "model_valve_open": model_open,
                "model_valve_locked": model_locked,
            }

    # 5) 与当前（=快照）模型对比
    if ev.observed_open is None:
        return {
            **base,
            "status": STATUS_PENDING,
            "status_label": STATUS_LABELS[STATUS_PENDING],
            "explanation": "现场观察值为“未知”，不能作为核验依据，阀门仍待核验。",
            "model_valve_open": model_open,
            "model_valve_locked": model_locked,
        }

    if ev.observed_open == model_open:
        lock_note = "（该阀处于锁定状态）" if model_locked else ""
        return {
            **base,
            "status": STATUS_VERIFIED,
            "status_label": STATUS_LABELS[STATUS_VERIFIED],
            "explanation": (
                f"现场观察“{_obs_label(ev.observed_open)}”与模型阀态一致{lock_note}，"
                "模型与观察在当前拓扑版本下相互核验通过。"
            ),
            "model_valve_open": model_open,
            "model_valve_locked": model_locked,
        }

    return {
        **base,
        "status": STATUS_CONTRADICTION,
        "status_label": STATUS_LABELS[STATUS_CONTRADICTION],
        "explanation": (
            f"现场观察为“{_obs_label(ev.observed_open)}”，而模型认为"
            f"“{'开' if model_open else '关'}”{('且该阀被锁定' if model_locked else '')}；"
            "任一方均不得自动采信，已建立待处置状态，依赖该阀的方案暂停确认。"
        ),
        "model_valve_open": model_open,
        "model_valve_locked": model_locked,
    }


def evaluate_many(db: Session, evidence_rows: list[ValveEvidence] | None = None) -> list[dict[str, Any]]:
    rows = evidence_rows if evidence_rows is not None else all_evidence(db)
    return [evaluate_evidence(db, ev) for ev in rows]


# ---------------------------------------------------------------------------
# 阀门当前核验视图（拓扑页标记用）
# ---------------------------------------------------------------------------


def valve_verification_map(db: Session) -> dict[str, dict[str, Any]]:
    """每只阀门的当前核验状态 = 其最新（按现场时刻）证据的评估；无证据则待核验。"""
    latest = latest_evidence_by_valve(db)
    result: dict[str, dict[str, Any]] = {}
    for vid, valve in _current_valves(db).items():
        ev = latest.get(vid)
        if ev is None:
            result[vid] = {
                "status": STATUS_PENDING,
                "status_label": STATUS_LABELS[STATUS_PENDING],
                "evidence_code": None,
                "observed_open": None,
                "explanation": "尚无核验证据。",
            }
        else:
            assessment = evaluate_evidence(db, ev)
            result[vid] = {
                "status": assessment["status"],
                "status_label": assessment["status_label"],
                "evidence_code": ev.evidence_code,
                "observed_open": ev.observed_open,
                "explanation": assessment["explanation"],
                "resolved_by": assessment.get("resolved_by"),
                "was_contradiction": assessment.get("was_contradiction", False),
            }
    return result


# ---------------------------------------------------------------------------
# 方案记录
# ---------------------------------------------------------------------------


def _load_plan(plan: IsolationPlan) -> dict[str, Any]:
    return json.loads(plan.result_json)


def list_plans(db: Session) -> list[IsolationPlan]:
    return list(db.scalars(select(IsolationPlan).order_by(IsolationPlan.id.desc())).all())


def affected_residual_paths(
    db: Session, plan: IsolationPlan, contradictions: list[dict[str, Any]]
) -> dict[str, Any]:
    """用 NetworkX 重算矛盾观察对方案的影响（观察覆盖，不改模型）。

    基线图取方案创建快照中的阀态：先按方案关闭集合切边，
    再用每条矛盾证据的观察值覆盖对应阀门，求来源→目标仍连通、
    且经过矛盾阀的残余路径；同时检查必要供给点是否因此断供。
    """
    result = _load_plan(plan)
    views = result["candidate_valves"]
    overrides: dict[str, bool] = {vid: False for vid in json.loads(plan.close_valves_json)}
    disputed: set[str] = set()
    for c in contradictions:
        disputed.add(c["valve_id"])
        if c["observed_open"] is not None:
            overrides[c["valve_id"]] = c["observed_open"]

    g = isolation.build_graph_from_valve_views(views, overrides=overrides)
    valve_edges = {v["id"]: tuple(v["endpoints"]) for v in views}
    sources = result["sources"]
    target = plan.target_id

    paths: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in sources:
        for nodes_path in isolation.paths_through_valves(g, src, target, valve_edges, disputed):
            key = canonical_json(nodes_path)
            if key in seen:
                continue
            seen.add(key)
            valve_seq = isolation._path_valves(g, nodes_path)
            paths.append(
                {
                    "nodes": nodes_path,
                    "valves": valve_seq,
                    "contradiction_valves": sorted(set(valve_seq) & disputed),
                }
            )

    essentials_after = {
        p: {
            "supplied": isolation._reachable_sources(g, p, sources) != [],
        }
        for p in result["essentials"]
    }
    target_reachable = any(
        src in g and target in g and nx.has_path(g, src, target) for src in sources
    )
    return {
        "target_reachable_with_observations": target_reachable,
        "paths": paths,
        "essentials": essentials_after,
    }


def review_plan(db: Session, plan: IsolationPlan) -> dict[str, Any]:
    """对一条方案做核验门禁复核（NetworkX 方案复核的核心）。"""
    result = _load_plan(plan)
    close_valves: list[str] = json.loads(plan.close_valves_json)
    all_rows = all_evidence(db)
    all_assessments = evaluate_many(db, all_rows)
    # 门禁按每阀“当前有效证据”评估；历史关联需逐条证据（每阀可有多条）
    assessments = {a["valve_id"]: a for a in all_assessments}
    latest = latest_evidence_by_valve(db, evidence=all_rows)

    _, edges = isolation._load(db)
    nodes = list(db.scalars(select(Node)).all())
    current_rev = events.current_revision(db)

    blockers: list[dict[str, Any]] = []
    valve_checks: list[dict[str, Any]] = []
    contradictions_on_plan: list[dict[str, Any]] = []

    # 方案关联证据（历史核验记录，供页面展示与重放；无论当前是否过期/被取代都保留）
    linked_rows = all_rows
    linked_codes = {ev.evidence_code for ev in linked_rows if ev.plan_id == plan.id}
    linked = [a for a in all_assessments if a.get("evidence_code") in linked_codes]

    # 方案“过期”判定：
    #  - 管网拓扑结构改变（物理前提变化）
    #  - 方案创建之后锁定状态发生变化（约束变化）
    #  - 非关闭集合内的阀门态偏离创建快照（关闭集合内的阀按计划关闭属预期）
    stale_reasons: list[str] = []
    close_set = set(close_valves)
    current_valves = _current_valves(db)
    snapshot_valve_views = {v["id"]: v for v in result["candidate_valves"]}

    drifted: list[str] = []
    locks_changed: list[str] = []
    for vid, view in snapshot_valve_views.items():
        cur = current_valves.get(vid)
        if cur is None:
            continue
        if cur.locked != view["locked"]:
            locks_changed.append(vid)
        if vid not in close_set and (cur.is_open != view["is_open"] or cur.locked != view["locked"]):
            drifted.append(vid)

    created_event = db.get(EventLog, plan.event_seq)
    plan_snap = json.loads(created_event.payload_json).get("topology_snapshot")
    if plan_snap is not None and _snapshot_structural_fingerprint(plan_snap) != isolation.structural_fingerprint(nodes, edges):
        stale_reasons.append("管网拓扑结构自方案创建后已改变")
    if locks_changed:
        stale_reasons.append(f"锁定状态发生变化：{', '.join(sorted(set(locks_changed)))}")
    if drifted:
        stale_reasons.append(f"方案外阀门态偏离创建快照：{', '.join(sorted(drifted))}")

    stale = bool(stale_reasons)

    for vid in close_valves:
        ev = latest.get(vid)
        if ev is None:
            check = {
                "valve_id": vid,
                "status": STATUS_PENDING,
                "status_label": STATUS_LABELS[STATUS_PENDING],
                "evidence_code": None,
                "blocks_confirmation": True,
                "reason": "方案要求关闭该阀，但尚无核验证据。",
            }
            blockers.append(check)
        else:
            a = assessments[vid]
            check = {
                "valve_id": vid,
                "status": a["status"],
                "status_label": a["status_label"],
                "evidence_code": a["evidence_code"],
                "observed_label": a["observed_label"],
                "blocks_confirmation": False,
                "reason": a["explanation"],
            }
            if a["status"] == STATUS_VERIFIED and a["observed_open"] is False:
                check["reason"] = "现场已核验为关闭，满足该方案边界阀要求。"
            elif a["status"] == STATUS_CONTRADICTION:
                check["blocks_confirmation"] = True
                check["reason"] = a["explanation"]
                blockers.append(check)
                contradictions_on_plan.append(a)
            else:
                check["blocks_confirmation"] = True
                if a["status"] == STATUS_VERIFIED and a["observed_open"] is True:
                    check["reason"] = "最新核验显示该阀仍为开启，尚未关闭到位。"
                elif a["status"] == STATUS_STALE:
                    check["reason"] = f"核验证据已过期：{a['explanation']}"
                elif a["status"] == STATUS_RESOLVED:
                    check["reason"] = "矛盾虽已复核结案（维持模型），但该阀缺少关闭核验，需重新现场核验。"
                elif a["status"] == STATUS_PENDING:
                    check["reason"] = "观察值为未知或证据不足，阀门仍待核验为关闭。"
                blockers.append(check)
        valve_checks.append(check)

    # 方案依赖但不在关闭集合里的矛盾阀：若按观察覆盖会重新连通目标，同样阻塞
    other_contradictions = [
        a for a in assessments.values()
        if a["status"] == STATUS_CONTRADICTION and a["valve_id"] not in set(close_valves)
    ]
    impact = affected_residual_paths(db, plan, contradictions_on_plan + other_contradictions)
    dependent_other = [
        a for a in other_contradictions
        if a["valve_id"] in {v for p in impact["paths"] for v in p["valves"]}
    ]
    for a in dependent_other:
        blockers.append(
            {
                "valve_id": a["valve_id"],
                "status": STATUS_CONTRADICTION,
                "status_label": STATUS_LABELS[STATUS_CONTRADICTION],
                "evidence_code": a["evidence_code"],
                "observed_label": a["observed_label"],
                "blocks_confirmation": True,
                "reason": f"方案虽不操作该阀，但矛盾观察会重新连通目标：{a['explanation']}",
            }
        )

    if not plan.feasible:
        confirmable = False
        plan_status = "infeasible"
        status_text = "方案本身不可行，不能确认"
    elif stale:
        confirmable = False
        plan_status = "outdated"
        status_text = (
            "方案已过期（物理前提或约束已变化，请重新计算）：" + "；".join(stale_reasons)
        )
    elif blockers:
        confirmable = False
        plan_status = "blocked"
        status_text = "存在未完成的核验门禁：" + "；".join(
            f"{b['valve_id']}（{b['status_label']}）" for b in blockers
        )
    elif plan.confirmed_seq is not None:
        confirmable = False
        plan_status = "confirmed"
        status_text = f"方案已于事件 #{plan.confirmed_seq} 确认（培训记录，非真实安全确认）。"
    else:
        confirmable = True
        plan_status = "confirmable"
        status_text = "全部边界阀均已现场核验为关闭且无未处置矛盾，可确认（仅培训演示意义）。"

    # 同一目标存在更新方案时，本方案标记为被取代（提示性，不改变其门禁结论）
    newer_plan = db.scalar(
        select(IsolationPlan)
        .where(IsolationPlan.target_id == plan.target_id, IsolationPlan.id > plan.id)
        .order_by(IsolationPlan.id.asc())
        .limit(1)
    )

    return {
        "plan_code": plan.plan_code,
        "plan_id": plan.id,
        "target_id": plan.target_id,
        "feasible": plan.feasible,
        "close_valves": close_valves,
        "status": plan_status,
        "status_text": status_text,
        "confirmable": confirmable,
        "superseded_by": newer_plan.id if newer_plan is not None else None,
        "confirmed_seq": plan.confirmed_seq,
        "confirmed_at": plan.confirmed_at,
        "created_seq": plan.created_seq,
        "model_revision": plan.model_revision,
        "model_revision_now": current_rev,
        "stale_reasons": stale_reasons,
        "valve_checks": valve_checks,
        "blockers": blockers,
        "linked_evidence": linked,
        "affected_residual_paths": impact,
        "examined_combinations": result.get("examined_combinations"),
    }


# ---------------------------------------------------------------------------
# 证据 ↔ 方案影响
# ---------------------------------------------------------------------------


def evidence_plan_effects(db: Session, ev: ValveEvidence) -> list[dict[str, Any]]:
    """该证据当前影响哪些方案（拓扑/方案/历史页统一用）。"""
    out: list[dict[str, Any]] = []
    for plan in list_plans(db):
        close_valves = set(json.loads(plan.close_valves_json))
        if ev.valve_id not in close_valves and ev.plan_id != plan.id:
            continue
        review = review_plan(db, plan)
        blocking_codes = {b.get("evidence_code") for b in review["blockers"]}
        out.append(
            {
                "plan_code": plan.plan_code,
                "plan_id": plan.id,
                "in_close_set": ev.valve_id in close_valves,
                "linked": ev.plan_id == plan.id,
                "blocks_confirmation": ev.evidence_code in blocking_codes,
                "plan_status": review["status"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 版本化重放
# ---------------------------------------------------------------------------


def replay_evidence(db: Session, ev: ValveEvidence) -> dict[str, Any]:
    """重放一条证据的“提交当时解释”与“当前解释”，供历史回放。"""
    at_time = evaluate_evidence(db, ev, at_submission=True)
    current = evaluate_evidence(db, ev, at_submission=False)
    snap = _load_snapshot(ev)

    # 当时模型中该阀的快照（原始记录，永不改变）
    snap_valve = next((v for v in snap["valves"] if v["id"] == ev.valve_id), None)
    return {
        "eval_version": EVIDENCE_EVAL_VERSION,
        "evidence": {
            "evidence_code": ev.evidence_code,
            "valve_id": ev.valve_id,
            "observed_open": ev.observed_open,
            "observed_label": _obs_label(ev.observed_open),
            "observed_at": ev.observed_at,
            "submitted_seq": ev.submitted_seq,
            "plan_id": ev.plan_id,
            "observer": ev.observer,
            "note": ev.note,
        },
        "interpretation_at_submission": {
            "status": at_time["status"],
            "status_label": at_time["status_label"],
            "explanation": at_time["explanation"],
            "model_revision": ev.model_revision,
            "model_valve": snap_valve,
        },
        "interpretation_current": {
            "status": current["status"],
            "status_label": current["status_label"],
            "explanation": current["explanation"],
            "model_revision": current["model_revision_now"],
            "stale_reason": current.get("stale_reason"),
            "resolved_by": current.get("resolved_by"),
        },
        "reviews": reviews_for(db, ev.evidence_code),
        "snapshot_fingerprint": ev.topology_fingerprint,
    }
