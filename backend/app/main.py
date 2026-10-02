"""FastAPI 入口：拓扑查询、阀门锁定/阀态、隔离方案计算、阀门核验证据与复核处置。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation, verification
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import Evidence, EventLog, IsolationPlan, Valve
from .schemas import (
    EvidenceIn,
    EvidenceOut,
    EventOut,
    IsolationIn,
    IsolationOut,
    PlanDetailOut,
    PlanSummaryOut,
    ReviewIn,
    TopologyOut,
    ValveLockIn,
    ValveStateIn,
)
from .seed import reset_database, seed_database


def _normalize_dt(dt: datetime) -> datetime:
    """统一为 UTC naive 存储；naive 输入按 UTC 解释。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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
        "基于固定拓扑与阀门模型的检修隔离候选集合计算与阀门核验证据工作流演示，"
        "不连接真实控制系统，任何状态均不代表真实安全确认。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _disputed_valves(db: Session) -> set[str]:
    return {d["valve_id"] for d in verification.valve_dispositions(db)}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/topology", response_model=TopologyOut)
def get_topology(db: Session = Depends(get_db)) -> TopologyOut:
    seed_database(db)
    payload = isolation.topology_payload(db)

    # 每只阀门挂接当前有效核验证据的推导状态
    version = verification.get_topology_version(db)
    current = verification.current_evidence(db)
    valves = {v.id: v for v in db.scalars(select(Valve)).all()}
    for v in payload["valves"]:
        ev = current.get(v["id"])
        if ev is None:
            v["verification"] = None
            continue
        status = verification.evidence_status(ev, valves[ev.valve_id], version, current)
        v["verification"] = {
            "status": status,
            "status_label": verification.STATUS_LABELS[status],
            "evidence_id": ev.id,
            "observed": ev.observed,
            "observed_at": ev.observed_at.isoformat(),
            "seq": ev.seq,
        }

    payload["topology_version"] = version
    payload["dispositions"] = verification.valve_dispositions(db)
    return TopologyOut(**payload)


@app.post("/api/valves/{valve_id}/lock")
def set_valve_lock(valve_id: str, body: ValveLockIn, db: Session = Depends(get_db)) -> dict[str, object]:
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    if valve.locked != body.locked:
        valve.locked = body.locked
        version = verification.bump_topology_version(db)
        verification.emit_event(
            db,
            "lock_changed",
            {"valve_id": valve_id, "locked": body.locked, "topology_version": version},
        )
        db.commit()
    return {"id": valve.id, "locked": valve.locked}


@app.post("/api/valves/{valve_id}/state")
def set_valve_state(valve_id: str, body: ValveStateIn, db: Session = Depends(get_db)) -> dict[str, object]:
    """模拟现场操作：改变模型阀态。锁定阀门禁止操作。"""
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    if valve.locked:
        raise HTTPException(status_code=409, detail=f"阀门 {valve_id} 已锁定，禁止操作")
    if valve.is_open != body.is_open:
        valve.is_open = body.is_open
        version = verification.bump_topology_version(db)
        verification.emit_event(
            db,
            "valve_state_changed",
            {"valve_id": valve_id, "is_open": body.is_open, "topology_version": version},
        )
        db.commit()
    return {
        "id": valve.id,
        "is_open": valve.is_open,
        "topology_version": verification.get_topology_version(db),
    }


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)) -> dict[str, str]:
    reset_database(db)
    version = verification.bump_topology_version(db)
    verification.emit_event(db, "reset", {"topology_version": version})
    db.commit()
    return {"status": "reset"}


@app.post("/api/isolation", response_model=IsolationOut)
def calc_isolation(body: IsolationIn, db: Session = Depends(get_db)) -> IsolationOut:
    seed_database(db)
    if body.locks:
        known = {v.id for v in db.scalars(select(Valve)).all()}
        unknown = [vid for vid in body.locks if vid not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知阀门: {', '.join(unknown)}")
        changed: dict[str, bool] = {}
        for vid, locked in body.locks.items():
            valve = db.get(Valve, vid)
            if valve is not None and valve.locked != locked:
                valve.locked = locked
                changed[vid] = locked
        if changed:
            version = verification.bump_topology_version(db)
            verification.emit_event(
                db, "locks_updated", {"locks": changed, "topology_version": version}
            )
        db.commit()

    try:
        payload = isolation.compute_isolation(db, body.target_id)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 持久化本次方案（版本化，供证据关联与事后回放）
    version = verification.get_topology_version(db)
    event = verification.emit_event(
        db,
        "plan_computed",
        {
            "target_id": body.target_id,
            "feasible": payload["feasible"],
            "best_solution": payload["best_solution"],
            "topology_version": version,
        },
    )
    plan = IsolationPlan(
        id=f"PLAN-{event.seq:04d}",
        seq=event.seq,
        target_id=body.target_id,
        feasible=payload["feasible"],
        topology_version=version,
        close_valves=payload["best_solution"],
        assumed_closed_valves=payload["assumed_closed_valves"],
        result=payload,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(plan)
    db.flush()
    event.payload = {**event.payload, "plan_id": plan.id}
    db.commit()

    # 核验证据工作流：待处置矛盾 + 每个方案的阻塞情况
    dispositions = verification.valve_dispositions(db)
    disputed = {d["valve_id"] for d in dispositions}
    for sol in payload["solutions"]:
        depends = set(sol["close_valves"]) | set(payload["assumed_closed_valves"])
        sol["blocked_by"] = sorted(depends & disputed)
    blocked_by = sorted(
        (set(payload["best_solution"]) | set(payload["assumed_closed_valves"])) & disputed
    )
    confirmable = bool(payload["feasible"]) and not blocked_by
    payload.update(
        {
            "plan_id": plan.id,
            "topology_version": version,
            "confirmable": confirmable,
            "blocked_reason": (
                None
                if confirmable
                else (
                    f"阀门 {', '.join(blocked_by)} 存在矛盾核验证据，待复核处置"
                    if blocked_by
                    else "无可行方案，不能确认"
                )
            ),
            "dispositions": dispositions,
        }
    )
    return IsolationOut(**payload)


# ---------------- 阀门核验证据 ----------------

@app.post("/api/evidence", response_model=EvidenceOut, status_code=201)
def submit_evidence(body: EvidenceIn, db: Session = Depends(get_db)) -> EvidenceOut:
    """提交一条阀门核验证据。证据永不改写模型阀态；迟到/重复观察只存档不生效。"""
    seed_database(db)
    valve = db.get(Valve, body.valve_id)
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {body.valve_id}")
    if body.plan_id is not None and db.get(IsolationPlan, body.plan_id) is None:
        raise HTTPException(status_code=400, detail=f"隔离方案不存在: {body.plan_id}")
    try:
        ev = verification.record_evidence(
            db, valve, body.observed, _normalize_dt(body.observed_at), body.plan_id, body.note
        )
    except verification.VerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return EvidenceOut(**verification.evidence_payload(db, ev))


@app.get("/api/evidence", response_model=list[EvidenceOut])
def list_evidence(db: Session = Depends(get_db)) -> list[EvidenceOut]:
    """全部核验证据（历史），按提交序号倒序；过期/被取代的原始记录同样保留。"""
    seed_database(db)
    evs = db.scalars(select(Evidence).order_by(Evidence.seq.desc())).all()
    return [EvidenceOut(**verification.evidence_payload(db, ev)) for ev in evs]


@app.post("/api/valves/{valve_id}/review")
def review_valve(valve_id: str, body: ReviewIn, db: Session = Depends(get_db)) -> dict[str, object]:
    """显式复核/纠正事件：处置该阀当前有效证据的矛盾，解除方案确认阻塞。"""
    valve = db.get(Valve, valve_id)
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    try:
        result = verification.review_valve(db, valve, body.action, body.note)
    except verification.VerificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return result


# ---------------- 隔离方案：查询 / 确认 / 回放 ----------------

@app.get("/api/plans", response_model=list[PlanSummaryOut])
def list_plans(db: Session = Depends(get_db)) -> list[PlanSummaryOut]:
    seed_database(db)
    version = verification.get_topology_version(db)
    disputed = _disputed_valves(db)
    plans = db.scalars(select(IsolationPlan).order_by(IsolationPlan.seq.desc())).all()
    return [
        PlanSummaryOut(**verification.plan_summary_payload(p, version, disputed)) for p in plans
    ]


@app.get("/api/plans/{plan_id}", response_model=PlanDetailOut)
def get_plan(plan_id: str, db: Session = Depends(get_db)) -> PlanDetailOut:
    plan = db.get(IsolationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"隔离方案不存在: {plan_id}")
    version = verification.get_topology_version(db)
    disputed = _disputed_valves(db)
    summary = verification.plan_summary_payload(plan, version, disputed)
    stale = plan.topology_version != version
    if stale:
        explanation = (
            f"方案 {plan.id} 于事件 #{plan.seq} 基于拓扑版本 v{plan.topology_version} 计算；"
            f"当前模型版本为 v{version}，模型已变化，结论仅供回放，不可确认。"
        )
    else:
        explanation = (
            f"方案 {plan.id} 于事件 #{plan.seq} 基于拓扑版本 v{plan.topology_version} 计算；"
            "模型自计算以来未变化，结论仍然适用。"
        )
    related = db.scalars(
        select(Evidence).where(Evidence.plan_id == plan.id).order_by(Evidence.seq)
    ).all()
    return PlanDetailOut(
        **summary,
        result=plan.result,
        current_topology_version=version,
        stale=stale,
        explanation=explanation,
        evidence=[EvidenceOut(**verification.evidence_payload(db, ev)) for ev in related],
    )


@app.post("/api/plans/{plan_id}/confirm")
def confirm_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    """确认方案。依赖矛盾阀门或已过期的方案拒绝确认（演示工作流门禁）。"""
    plan = db.get(IsolationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"隔离方案不存在: {plan_id}")
    if plan.confirmed_seq is not None:
        return {"id": plan.id, "status": "confirmed", "confirmed_seq": plan.confirmed_seq}

    version = verification.get_topology_version(db)
    status, blocked_by = verification.plan_status(plan, version, _disputed_valves(db))
    if status == "stale":
        raise HTTPException(
            status_code=409,
            detail=f"方案基于拓扑版本 v{plan.topology_version}，当前为 v{version}，请重新计算",
        )
    if status == "infeasible":
        raise HTTPException(status_code=409, detail="无可行方案，不能确认")
    if status == "blocked":
        raise HTTPException(
            status_code=409,
            detail=f"阀门 {', '.join(blocked_by)} 存在矛盾核验证据，需先完成复核/纠正",
        )

    event = verification.emit_event(
        db, "plan_confirmed", {"plan_id": plan.id, "topology_version": version}
    )
    plan.confirmed_seq = event.seq
    db.commit()
    return {"id": plan.id, "status": "confirmed", "confirmed_seq": event.seq}


@app.get("/api/events", response_model=list[EventOut])
def list_events(db: Session = Depends(get_db)) -> list[EventOut]:
    """持久化事件链（按提交序号升序），用于历史重放。"""
    seed_database(db)
    events = db.scalars(select(EventLog).order_by(EventLog.seq)).all()
    return [
        EventOut(seq=e.seq, type=e.type, created_at=e.created_at, payload=e.payload)
        for e in events
    ]


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
