"""持久化事件链（event sourcing）：唯一可写入口与重放工具。

设计要点（培训演示）：

- *仅追加*：``event_log`` 中的行永不修改、永不删除；投影表（阀态、证据、方案）
  全部可由事件链重建。每行保存前一事件哈希与自身 SHA-256，形成哈希链，
  任何插入之外的篡改都会在 ``verify_chain`` 中暴露。
- 事件类型：
    valve_state_changed   模型侧记录的阀态变更（操作票/模型维护），自增模型版本
    valve_lock_changed    阀门锁定/解锁，自增模型版本
    plan_created          隔离方案计算并持久化（不改模型版本）
    evidence_submitted    现场核验证据（开/关/未知 + 拓扑快照），不改模型版本
    review_completed      针对矛盾证据的显式复核/纠正事件
                          （disposition=correct_model 时经阀态事件纠正模型，自增版本）
    plan_confirmed        方案确认（门禁通过后才允许）
    model_reset           全部阀门恢复打开、未锁定，自增模型版本
- 时间：服务端事件时间取 UTC；``observed_at`` 由提交者显式给出，因此允许
  “迟到观察”（先提交较新的，后补较旧的），排序以现场发生时刻为准。
- 矛盾证据不会在本模块中改写阀态；模型阀态只可能由
  valve_state_changed / review_completed(correct_model) / model_reset 改变。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import EventLog, ModelMeta, Valve

EVIDENCE_EVAL_VERSION = "evidence-eval-v1"

STATE_CHANGING_TYPES = {"valve_state_changed", "valve_lock_changed", "model_reset"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(obj: Any) -> str:
    """sort_keys + 紧凑分隔的稳定 JSON 序列化（哈希与跨库一致）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def get_meta(db: Session) -> ModelMeta:
    meta = db.scalar(select(ModelMeta).where(ModelMeta.singleton.is_(True)))
    if meta is None:
        meta = ModelMeta(singleton=True, model_revision=0)
        db.add(meta)
        db.flush()
    return meta


def current_revision(db: Session) -> int:
    return get_meta(db).model_revision


def _last_event(db: Session) -> EventLog | None:
    return db.scalar(select(EventLog).order_by(EventLog.seq.desc()).limit(1))


def append_event(
    db: Session,
    event_type: str,
    payload: dict[str, Any],
    *,
    occurred_at: str | None = None,
    bump_revision: bool | None = None,
) -> EventLog:
    """在事件链尾部追加一个事件（调用方负责随后的投影更新与 commit）。

    返回的 EventLog 已带 seq/self_hash；本函数会 flush 以取得序号。
    """
    meta = get_meta(db)
    if bump_revision is None:
        bump_revision = event_type in STATE_CHANGING_TYPES
    # review_completed 仅在“纠正模型”时自增版本；普通复核不改变模型
    if event_type == "review_completed":
        bump_revision = payload.get("disposition") == "correct_model"

    if bump_revision:
        meta.model_revision += 1

    last = _last_event(db)
    prev_hash = last.self_hash if last is not None else ""
    # 先 flush-less 构造，序号需要 insert 后才有：用临时占位计算再回填
    event = EventLog(
        event_type=event_type,
        occurred_at=occurred_at or payload.get("observed_at") or utc_now_iso(),
        payload_json=canonical_json(payload),
        model_revision=meta.model_revision,
        prev_hash=prev_hash,
        self_hash="",
    )
    db.add(event)
    db.flush()  # 取得 seq

    digest_input = "\x1f".join(
        [
            str(event.seq),
            event.event_type,
            event.occurred_at,
            event.payload_json,
            str(event.model_revision),
            event.prev_hash,
        ]
    )
    event.self_hash = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    db.flush()
    return event


# ---------------------------------------------------------------------------
# 模型阀态变更（唯一允许修改 Valve.is_open / locked 的事件路径）
# ---------------------------------------------------------------------------


def record_valve_state(db: Session, valve: Valve, is_open: bool, *, note: str | None = None) -> EventLog | None:
    """记录模型阀态变更；与现状一致时不产生事件（幂等）。"""
    if valve.is_open == is_open:
        return None
    payload: dict[str, Any] = {
        "valve_id": valve.id,
        "is_open": is_open,
        "previous_is_open": valve.is_open,
        "reason": note or "model-recorded operation",
    }
    event = append_event(db, "valve_state_changed", payload)
    valve.is_open = is_open
    return event


def record_valve_lock(db: Session, valve: Valve, locked: bool) -> EventLog | None:
    if valve.locked == locked:
        return None
    payload = {"valve_id": valve.id, "locked": locked, "previous_locked": valve.locked}
    event = append_event(db, "valve_lock_changed", payload)
    valve.locked = locked
    return event


def record_reset(db: Session, valve_ids: list[str]) -> EventLog:
    """重置事件：所有阀门恢复打开、未锁定（事件链保留全部历史）。"""
    event = append_event(
        db,
        "model_reset",
        {"valve_ids": valve_ids, "is_open": True, "locked": False},
    )
    for vid in valve_ids:
        valve = db.get(Valve, vid)
        if valve is not None:
            valve.is_open = True
            valve.locked = False
    return event


# ---------------------------------------------------------------------------
# 重放 / 校验
# ---------------------------------------------------------------------------


def list_events(db: Session, *, limit: int | None = None) -> list[EventLog]:
    stmt = select(EventLog).order_by(EventLog.seq.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def expected_hash(event: EventLog) -> str:
    digest_input = "\x1f".join(
        [
            str(event.seq),
            event.event_type,
            event.occurred_at,
            event.payload_json,
            str(event.model_revision),
            event.prev_hash,
        ]
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def verify_chain(db: Session) -> dict[str, Any]:
    """重放哈希链。返回 {ok, count, broken_seq, reason}。"""
    events = list_events(db)
    prev = ""
    for ev in events:
        if ev.prev_hash != prev:
            return {
                "ok": False,
                "count": len(events),
                "broken_seq": ev.seq,
                "reason": "prev_hash 与上一事件不一致（事件链可能被插入/删除）",
            }
        if expected_hash(ev) != ev.self_hash:
            return {
                "ok": False,
                "count": len(events),
                "broken_seq": ev.seq,
                "reason": "self_hash 校验失败（事件载荷或字段被篡改）",
            }
        prev = ev.self_hash
    return {"ok": True, "count": len(events), "broken_seq": None, "reason": None}


def replay_valve_model(db: Session, upto_seq: int | None = None) -> dict[str, dict[str, Any]]:
    """纯函数式重放事件链，得到指定序号时刻的阀态模型（用于版本化重放）。

    返回 {valve_id: {"is_open": bool, "locked": bool, "revision": int}}。
    不读 Valve 投影表；plan/evidence 事件不影响阀态。
    """
    from .seed import SEGMENTS

    state: dict[str, dict[str, Any]] = {
        valve_id: {"is_open": True, "locked": False, "revision": 0}
        for _, _, _, valve_id, _, _, _ in SEGMENTS
    }
    revision = 0
    for ev in list_events(db):
        if upto_seq is not None and ev.seq > upto_seq:
            break
        p = json.loads(ev.payload_json)
        if ev.event_type == "valve_state_changed":
            vid = p["valve_id"]
            state.setdefault(vid, {"is_open": True, "locked": False, "revision": 0})
            state[vid]["is_open"] = p["is_open"]
            state[vid]["revision"] = ev.model_revision
            revision = ev.model_revision
        elif ev.event_type == "valve_lock_changed":
            vid = p["valve_id"]
            state.setdefault(vid, {"is_open": True, "locked": False, "revision": 0})
            state[vid]["locked"] = p["locked"]
            state[vid]["revision"] = ev.model_revision
            revision = ev.model_revision
        elif ev.event_type == "model_reset":
            for vid in p["valve_ids"]:
                state.setdefault(vid, {"is_open": True, "locked": False, "revision": 0})
                state[vid]["is_open"] = True
                state[vid]["locked"] = False
                state[vid]["revision"] = ev.model_revision
            revision = ev.model_revision
        elif ev.event_type == "review_completed" and p.get("disposition") == "correct_model":
            vid = p["valve_id"]
            state.setdefault(vid, {"is_open": True, "locked": False, "revision": 0})
            state[vid]["is_open"] = p["set_is_open"]
            state[vid]["revision"] = ev.model_revision
            revision = ev.model_revision
    return state
