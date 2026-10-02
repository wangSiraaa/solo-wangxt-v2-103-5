"""FastAPI 入口：拓扑、阀态/锁定、隔离方案、核验证据与复核处置。

所有状态变更都先写仅追加事件链（app.events），再更新投影；
矛盾证据不会通过任何接口自动改写模型阀态。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import events, isolation, verification
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import IsolationPlan, Valve, ValveEvidence
from .schemas import (
    ConfirmIn,
    EvidenceIn,
    IsolationIn,
    IsolationOut,
    ReviewIn,
    TopologyOut,
    ValveLockIn,
    ValveStateIn,
)
from .seed import reset_database, seed_database


@asynccontextmanager
async def lifespan(app: FastAPI):  # pragma: no cover
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="管网隔离方案演示 API（培训用）",
    description=(
        "基于固定拓扑与阀门模型的检修隔离候选集合计算与阀门核验证据管理，"
        "不连接真实控制系统；任何确认均仅为培训记录，不构成真实安全确认。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _parse_observed_at(value: str | None) -> str:
    """校验/归一化现场时刻。接受 ISO-8601（可带 Z）；缺省取服务端 UTC 当前时间。"""
    if not value:
        return events.utc_now_iso()
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"observed_at 不是合法 ISO-8601 时间: {value}") from exc
    # 无时区信息的时间戳按 UTC 解释（培训演示约定）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _get_valve_or_404(db: Session, valve_id: str) -> Valve:
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    return valve


def _get_plan_or_404(db: Session, plan_id: int) -> IsolationPlan:
    plan = db.get(IsolationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"方案不存在: {plan_id}")
    return plan


def _get_evidence_or_404(db: Session, evidence_code: str) -> ValveEvidence:
    ev = db.scalar(select(ValveEvidence).where(ValveEvidence.evidence_code == evidence_code))
    if ev is None:
        raise HTTPException(status_code=404, detail=f"证据不存在: {evidence_code}")
    return ev


def _enrich_valves_with_verification(
    db: Session, payload: dict, *, key: str = "valves", only_ids: set[str] | None = None
) -> dict:
    vmap = verification.valve_verification_map(db)
    for v in payload[key]:
        if only_ids is not None and v["id"] not in only_ids:
            continue
        v["verification"] = vmap.get(v["id"])
    return payload


# ---------------------------------------------------------------------------
# 基础接口
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/topology", response_model=TopologyOut)
def get_topology(db: Session = Depends(get_db)) -> TopologyOut:
    seed_database(db)
    payload = isolation.topology_payload(db)
    _enrich_valves_with_verification(db, payload)
    payload["model_revision"] = events.current_revision(db)
    return TopologyOut(**payload)


@app.post("/api/valves/{valve_id}/lock")
def set_valve_lock(valve_id: str, body: ValveLockIn, db: Session = Depends(get_db)) -> dict[str, object]:
    valve = _get_valve_or_404(db, valve_id)
    event = events.record_valve_lock(db, valve, body.locked)
    db.commit()
    return {
        "id": valve.id,
        "locked": valve.locked,
        "event_seq": event.seq if event else None,
        "model_revision": events.current_revision(db),
    }


@app.post("/api/valves/{valve_id}/state")
def set_valve_state(valve_id: str, body: ValveStateIn, db: Session = Depends(get_db)) -> dict[str, object]:
    """模型侧记录阀门操作结果（开/关）。矛盾证据永远不会走这里自动改写。"""
    valve = _get_valve_or_404(db, valve_id)
    event = events.record_valve_state(db, valve, body.is_open, note=body.note)
    db.commit()
    return {
        "id": valve.id,
        "is_open": valve.is_open,
        "event_seq": event.seq if event else None,
        "model_revision": events.current_revision(db),
    }


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)) -> dict[str, object]:
    reset_database(db)
    return {"status": "reset", "model_revision": events.current_revision(db)}


# ---------------------------------------------------------------------------
# 隔离方案（计算即生成不可变方案记录）
# ---------------------------------------------------------------------------


@app.post("/api/isolation", response_model=IsolationOut)
def calc_isolation(body: IsolationIn, db: Session = Depends(get_db)) -> IsolationOut:
    seed_database(db)
    if body.locks:
        known = {v.id for v in db.scalars(select(Valve)).all()}
        unknown = [vid for vid in body.locks if vid not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知阀门: {', '.join(unknown)}")
        for vid, locked in body.locks.items():
            valve = db.get(Valve, vid)
            if valve is not None:
                events.record_valve_lock(db, valve, locked)
        db.flush()

    try:
        payload = isolation.compute_isolation(db, body.target_id)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 持久化方案：先拍快照，再追加 plan_created 事件与方案行
    revision = events.current_revision(db)
    snapshot = isolation.topology_snapshot(db, revision)
    close_valves = list(payload.get("best_solution") or [])
    plan_payload: dict = {
        "target_id": body.target_id,
        "locks": body.locks or {},
        "result": payload,
        "close_valves": close_valves,
        "model_revision": revision,
        "topology_fingerprint": snapshot["topology_fingerprint"],
        "topology_snapshot": snapshot,
    }
    event = events.append_event(db, "plan_created", plan_payload)
    plan = IsolationPlan(
        plan_code=f"PL-{event.seq}",
        event_seq=event.seq,
        target_id=body.target_id,
        feasible=bool(payload["feasible"]),
        close_valves_json=events.canonical_json(close_valves),
        locks_json=events.canonical_json(body.locks or {}),
        model_revision=revision,
        topology_fingerprint=snapshot["topology_fingerprint"],
        result_json=events.canonical_json(payload),
        created_seq=event.seq,
    )
    db.add(plan)
    db.flush()

    _enrich_valves_with_verification(db, payload, key="candidate_valves")
    payload["model_revision"] = revision
    payload["plan"] = {
        "plan_id": plan.id,
        "plan_code": plan.plan_code,
        "event_seq": event.seq,
        **verification.review_plan(db, plan),
    }
    db.commit()
    return IsolationOut(**payload)


@app.get("/api/plans")
def get_plans(db: Session = Depends(get_db)) -> dict[str, object]:
    plans = verification.list_plans(db)
    return {
        "plans": [verification.review_plan(db, p) for p in plans],
        "model_revision": events.current_revision(db),
    }


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    plan = _get_plan_or_404(db, plan_id)
    review = verification.review_plan(db, plan)
    review["result"] = json.loads(plan.result_json)
    return review


@app.post("/api/plans/{plan_id}/confirm")
def confirm_plan(plan_id: int, body: ConfirmIn, db: Session = Depends(get_db)) -> dict[str, object]:
    plan = _get_plan_or_404(db, plan_id)
    review = verification.review_plan(db, plan)
    if not review["confirmable"]:
        raise HTTPException(
            status_code=409,
            detail=f"方案当前不可确认：{review['status_text']}",
        )
    if plan.confirmed_seq is not None:
        raise HTTPException(status_code=409, detail="方案已确认，确认事件不可重复")

    payload = {
        "plan_code": plan.plan_code,
        "plan_id": plan.id,
        "target_id": plan.target_id,
        "close_valves": json.loads(plan.close_valves_json),
        "model_revision": plan.model_revision,
        "note": body.note,
        "confirmer": body.confirmer,
        "training_only": True,
    }
    event = events.append_event(db, "plan_confirmed", payload)
    plan.confirmed_seq = event.seq
    plan.confirmed_at = event.occurred_at
    db.commit()
    return {
        "status": "confirmed",
        "event_seq": event.seq,
        "plan_code": plan.plan_code,
        "warning": "该确认仅为培训流程记录，不代表真实检修已满足安全隔离条件（LOTO）。",
        **verification.review_plan(db, plan),
    }


# ---------------------------------------------------------------------------
# 核验证据
# ---------------------------------------------------------------------------


@app.post("/api/evidence")
def submit_evidence(body: EvidenceIn, db: Session = Depends(get_db)) -> dict[str, object]:
    valve = _get_valve_or_404(db, body.valve_id)
    observed_at = _parse_observed_at(body.observed_at)
    if body.plan_id is not None:
        _get_plan_or_404(db, body.plan_id)

    revision = events.current_revision(db)
    snapshot = isolation.topology_snapshot(db, revision)

    evidence_payload = {
        "valve_id": valve.id,
        "observed": body.observed,
        "observed_open": verification.observed_to_bool(body.observed),
        "observed_at": observed_at,
        "plan_id": body.plan_id,
        "observer": body.observer,
        "note": body.note,
        "model_revision": revision,
        "topology_fingerprint": snapshot["topology_fingerprint"],
        "topology_snapshot": snapshot,
    }
    event = events.append_event(db, "evidence_submitted", evidence_payload)
    row = ValveEvidence(
        evidence_code=f"EV-{event.seq}",
        event_seq=event.seq,
        valve_id=valve.id,
        observed_open=verification.observed_to_bool(body.observed),
        observed_at=observed_at,
        submitted_seq=event.seq,
        model_revision=revision,
        topology_fingerprint=snapshot["topology_fingerprint"],
        topology_snapshot_json=events.canonical_json(snapshot),
        plan_id=body.plan_id,
        observer=body.observer,
        note=body.note,
    )
    db.add(row)
    db.flush()

    assessment = verification.evaluate_evidence(db, row)
    affected_plans = verification.evidence_plan_effects(db, row)
    vmap = verification.valve_verification_map(db)
    db.commit()
    return {
        "evidence_code": row.evidence_code,
        "event_seq": event.seq,
        "assessment": assessment,
        # 该阀门在整条证据链上的当前核验状态（同时刻不同值时可能与本条不同）
        "valve_current_status": vmap.get(row.valve_id, {}).get("status"),
        "affected_plans": affected_plans,
        "training_notice": "证据只记录现场观察与解释；矛盾不会自动改写模型阀态。",
    }


@app.get("/api/evidence")
def list_evidence(db: Session = Depends(get_db)) -> dict[str, object]:
    rows = verification.all_evidence(db)
    reviews = verification.reviews_for(db)
    return {
        "evidence": [
            {
                **verification.evaluate_evidence(db, row),
                "affected_plans": verification.evidence_plan_effects(db, row),
            }
            for row in rows
        ],
        "reviews": reviews,
        "model_revision": events.current_revision(db),
    }


@app.get("/api/evidence/{evidence_code}")
def get_evidence(evidence_code: str, db: Session = Depends(get_db)) -> dict[str, object]:
    row = _get_evidence_or_404(db, evidence_code)
    return {
        **verification.evaluate_evidence(db, row),
        "affected_plans": verification.evidence_plan_effects(db, row),
    }


@app.get("/api/evidence/{evidence_code}/replay")
def replay_evidence(evidence_code: str, db: Session = Depends(get_db)) -> dict[str, object]:
    row = _get_evidence_or_404(db, evidence_code)
    return verification.replay_evidence(db, row)


@app.post("/api/evidence/{evidence_code}/review")
def review_evidence(
    evidence_code: str, body: ReviewIn, db: Session = Depends(get_db)
) -> dict[str, object]:
    """对矛盾证据执行显式复核/纠正。未完成结案前，阻塞状态不会解除。"""
    row = _get_evidence_or_404(db, evidence_code)
    valve = _get_valve_or_404(db, row.valve_id)

    # reinspect 可多次登记；结案型复核若已有结论则拒绝重复结案（保持事件链纪律）
    existing = verification._resolving_review(db, row)
    if existing is not None and body.disposition != verification.DISPOSITION_REINSPECT:
        raise HTTPException(
            status_code=409,
            detail=f"该证据已有结案复核（事件 #{existing['seq']}，{existing['disposition']}）；"
            "如需变更请重新核验并提交新证据。",
        )

    set_is_open: bool | None = None
    if body.disposition == verification.DISPOSITION_CORRECT:
        if row.observed_open is None:
            raise HTTPException(status_code=400, detail="观察值为“未知”，无法据此纠正模型")
        set_is_open = row.observed_open

    payload = {
        "evidence_code": row.evidence_code,
        "valve_id": row.valve_id,
        "disposition": body.disposition,
        "set_is_open": set_is_open,
        "observed_open": row.observed_open,
        "previous_model_is_open": valve.is_open,
        "note": body.note,
        "reviewer": body.reviewer,
    }
    event = events.append_event(db, "review_completed", payload)
    if body.disposition == verification.DISPOSITION_CORRECT and set_is_open is not None:
        # append_event 已为本次 review 自增过模型版本；这里只做投影更新，
        # 不再单独追加 valve_state_changed（该事件即纠正事件本身）。
        valve.is_open = set_is_open
    db.commit()

    return {
        "event_seq": event.seq,
        "disposition": body.disposition,
        "assessment": verification.evaluate_evidence(db, row),
        "model_revision": events.current_revision(db),
    }


# ---------------------------------------------------------------------------
# 事件链（审计 / 教学）
# ---------------------------------------------------------------------------


@app.get("/api/events")
def get_events(db: Session = Depends(get_db)) -> dict[str, object]:
    evs = events.list_events(db)
    return {
        "events": [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at,
                "model_revision": e.model_revision,
                "prev_hash": e.prev_hash,
                "self_hash": e.self_hash,
                "payload": json.loads(e.payload_json),
            }
            for e in evs
        ],
        "chain": events.verify_chain(db),
    }


@app.get("/api/events/verify")
def verify_events(db: Session = Depends(get_db)) -> dict[str, object]:
    return events.verify_chain(db)


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
