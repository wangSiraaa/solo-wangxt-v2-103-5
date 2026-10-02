"""阀门核验证据：持久化事件链、证据评估、方案门禁与版本化重放测试。

覆盖用户验收四条主线：
1) 与模型一致的关阀核验使相关方案显示可确认；
2) 观察到开启而模型认为关闭时方案被阻塞并标出受影响残余路径；
3) 较早或重复观察在较新核验之后到达时当前状态不倒退；
4) 完成复核后重新计算，新方案可确认，旧证据与其版本化解释仍可重放。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import events, verification
from app.database import Base, SessionLocal, engine
from app.main import app
from app.seed import seed_database


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()
    yield
    db.close() if db.is_active else None


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _iso(minute: int) -> str:
    return f"2026-10-02T{10 + minute:02d}:00:00+00:00"


def _plan(client, target="T", locks=None):
    body = {"target_id": target}
    if locks is not None:
        body["locks"] = locks
    r = client.post("/api/isolation", json=body)
    assert r.status_code == 200, r.text
    return r.json()["plan"]


def _state(client, vid, is_open):
    r = client.post(f"/api/valves/{vid}/state", json={"is_open": is_open})
    assert r.status_code == 200, r.text


def _observe(client, vid, observed, at=None, plan_id=None):
    body: dict = {"valve_id": vid, "observed": observed}
    if at is not None:
        body["observed_at"] = at
    if plan_id is not None:
        body["plan_id"] = plan_id
    r = client.post("/api/evidence", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1) 一致关阀核验 -> 可确认
# ---------------------------------------------------------------------------


def test_1_consistent_closed_verification_makes_plan_confirmable(client):
    plan = _plan(client)
    assert plan["status"] == "blocked"
    assert plan["close_valves"] == ["V_TIN", "V_TOUT"]
    # 未核验时确认被拒
    assert client.post(f"/api/plans/{plan['plan_id']}/confirm", json={}).status_code == 409

    _state(client, "V_TIN", False)
    e1 = _observe(client, "V_TIN", "closed", at=_iso(0), plan_id=plan["plan_id"])
    assert e1["assessment"]["status"] == verification.STATUS_VERIFIED

    # 只核验一只仍不可确认
    review = client.get(f"/api/plans/{plan['plan_id']}").json()
    assert review["status"] == "blocked"
    assert {b["valve_id"] for b in review["blockers"]} == {"V_TOUT"}

    _state(client, "V_TOUT", False)
    _observe(client, "V_TOUT", "closed", at=_iso(1), plan_id=plan["plan_id"])
    review = client.get(f"/api/plans/{plan['plan_id']}").json()
    assert review["status"] == "confirmable"
    assert review["confirmable"] is True

    confirmed = client.post(
        f"/api/plans/{plan['plan_id']}/confirm",
        json={"confirmer": "trainer", "note": "培训记录"},
    ).json()
    assert confirmed["status"] == "confirmed"
    assert "不代表真实" in confirmed["warning"]
    # 重复确认被拒
    assert client.post(f"/api/plans/{plan['plan_id']}/confirm", json={}).status_code == 409


def test_unknown_observation_keeps_pending(client):
    plan = _plan(client)
    _state(client, "V_TIN", False)
    ev = _observe(client, "V_TIN", "unknown", plan_id=plan["plan_id"])
    assert ev["assessment"]["status"] == verification.STATUS_PENDING
    review = client.get(f"/api/plans/{plan['plan_id']}").json()
    assert review["status"] == "blocked"


# ---------------------------------------------------------------------------
# 2) 观察开 / 模型关 -> 矛盾阻塞 + 受影响残余路径
# ---------------------------------------------------------------------------


def test_2_open_observation_against_closed_model_blocks_and_marks_residual(client):
    plan = _plan(client)
    pid = plan["plan_id"]

    _state(client, "V_TIN", False)
    contradiction = _observe(client, "V_TIN", "open", at=_iso(2), plan_id=pid)
    assert contradiction["assessment"]["status"] == verification.STATUS_CONTRADICTION
    assert contradiction["affected_plans"]
    # 模型阀态绝不能被证据改写
    topo = client.get("/api/topology").json()
    vtin = next(v for v in topo["valves"] if v["id"] == "V_TIN")
    assert vtin["is_open"] is False
    assert vtin["verification"]["status"] == verification.STATUS_CONTRADICTION

    _state(client, "V_TOUT", False)
    _observe(client, "V_TOUT", "closed", at=_iso(3), plan_id=pid)

    review = client.get(f"/api/plans/{pid}").json()
    assert review["status"] == "blocked"
    blocker = next(b for b in review["blockers"] if b["valve_id"] == "V_TIN")
    assert blocker["status"] == "contradiction"

    # NetworkX 复核：按观察覆盖后目标经矛盾阀重新可达
    impact = review["affected_residual_paths"]
    assert impact["target_reachable_with_observations"] is True
    assert impact["paths"], "必须标出经矛盾阀的受影响残余路径"
    first = impact["paths"][0]
    assert first["nodes"][0] == "SRC" and first["nodes"][-1] == "T"
    assert "V_TIN" in first["contradiction_valves"]

    # 确认被拒
    r = client.post(f"/api/plans/{pid}/confirm", json={})
    assert r.status_code == 409
    assert "矛盾" in r.json()["detail"] or "门禁" in r.json()["detail"]


def test_unresolved_contradiction_keeps_open_even_on_newer_matching_evidence_reinspect(client):
    """reinspect 只是安排重新检查，不解除待处置状态。"""
    plan = _plan(client)
    _state(client, "V_TIN", False)
    ev = _observe(client, "V_TIN", "open", at=_iso(2))
    r = client.post(
        f"/api/evidence/{ev['evidence_code']}/review",
        json={"disposition": "reinspect", "note": "再去现场看一次"},
    )
    assert r.status_code == 200
    assert r.json()["assessment"]["status"] == verification.STATUS_CONTRADICTION


# ---------------------------------------------------------------------------
# 3) 迟到 / 重复观察不倒退
# ---------------------------------------------------------------------------


def test_3_late_and_duplicate_observations_do_not_roll_back(client):
    plan = _plan(client)
    _state(client, "V_TIN", False)
    _state(client, "V_TOUT", False)

    # 较新的现场核验（时刻 t5，关）
    newer = _observe(client, "V_TIN", "closed", at=_iso(5), plan_id=plan["plan_id"])
    assert newer["assessment"]["status"] == verification.STATUS_VERIFIED

    # 迟到观察：提交更晚但发生时刻更早（开）—— 不能取代
    late = _observe(client, "V_TIN", "open", at=_iso(2))
    assert late["assessment"]["status"] == verification.STATUS_STALE
    assert late["assessment"]["stale_reason"] == "superseded"

    # 重复观察：同一现场时刻 + 同一观察值
    duplicate = _observe(client, "V_TIN", "closed", at=_iso(5))
    assert duplicate["assessment"]["status"] == verification.STATUS_STALE
    assert duplicate["assessment"]["stale_reason"] == "duplicate"
    assert "不覆盖" in duplicate["assessment"]["explanation"]

    # 同时刻不同值：后提交者优先（序号决胜），较早提交的一条在当前视图中变 stale
    a = _observe(client, "V_TOUT", "closed", at=_iso(6))
    b = _observe(client, "V_TOUT", "open", at=_iso(6))
    # 提交 a 的当时它确实是已核验；b 到达后响应即反映：b 为当前矛盾，a 被取代
    assert b["valve_current_status"] == verification.STATUS_CONTRADICTION
    current = {
        e["evidence_code"]: e for e in client.get("/api/evidence").json()["evidence"]
    }
    assert current[a["evidence_code"]]["status"] == verification.STATUS_STALE
    assert current[b["evidence_code"]]["status"] == verification.STATUS_CONTRADICTION  # 模型关，观察开

    # 当前阀门核验视图不倒退
    topo = client.get("/api/topology").json()
    by_id = {v["id"]: v["verification"] for v in topo["valves"]}
    assert by_id["V_TIN"]["status"] == verification.STATUS_VERIFIED
    assert by_id["V_TOUT"]["status"] == verification.STATUS_CONTRADICTION

    # 证据列表中原始记录全部保留（V_TIN 三条：原始/迟到/重复副本）
    listed = client.get("/api/evidence").json()["evidence"]
    vtin_rows = [e for e in listed if e["valve_id"] == "V_TIN"]
    assert len(vtin_rows) == 3
    assert all(not e["evidence_code"].startswith("del") for e in vtin_rows)


def test_lock_change_blocks_old_plan_and_marks_outdated(client):
    plan = _plan(client)
    # 锁定另一只阀门（约束变化）应使未执行的旧方案过期
    client.post("/api/valves/V1/lock", json={"locked": True})
    review = client.get(f"/api/plans/{plan['plan_id']}").json()
    assert review["status"] == "outdated"
    assert review["confirmable"] is False
    assert any("锁定" in r for r in review["stale_reasons"])


# ---------------------------------------------------------------------------
# 4) 复核/纠正 -> 重算 -> 新方案确认 + 版本化重放
# ---------------------------------------------------------------------------


def test_4_review_correct_model_then_new_plan_confirms_and_replay_available(client):
    plan1 = _plan(client)
    _state(client, "V_TIN", False)
    bad = _observe(client, "V_TIN", "open", at=_iso(2), plan_id=plan1["plan_id"])
    _state(client, "V_TOUT", False)
    _observe(client, "V_TOUT", "closed", at=_iso(3), plan_id=plan1["plan_id"])
    assert client.get(f"/api/plans/{plan1['plan_id']}").json()["status"] == "blocked"

    # 完成复核：以现场观察为准纠正模型（事件链内自增版本，模型被显式改为开）
    review = client.post(
        f"/api/evidence/{bad['evidence_code']}/review",
        json={"disposition": "correct_model", "reviewer": "lead", "note": "现场确认开启"},
    )
    assert review.status_code == 200
    topo = client.get("/api/topology").json()
    vtin = next(v for v in topo["valves"] if v["id"] == "V_TIN")
    assert vtin["is_open"] is True  # 仅显式纠正事件可改模型
    assert review.json()["assessment"]["status"] == verification.STATUS_VERIFIED
    assert review.json()["assessment"]["was_contradiction"] is True

    # 重新执行并核验，新方案可确认
    plan2 = _plan(client)
    assert plan2["plan_id"] != plan1["plan_id"]
    _state(client, "V_TIN", False)
    _observe(client, "V_TIN", "closed", at=_iso(8), plan_id=plan2["plan_id"])
    _state(client, "V_TOUT", True)
    _state(client, "V_TOUT", False)
    _observe(client, "V_TOUT", "closed", at=_iso(9), plan_id=plan2["plan_id"])
    r2 = client.get(f"/api/plans/{plan2['plan_id']}").json()
    assert r2["status"] == "confirmable"
    assert client.post(f"/api/plans/{plan2['plan_id']}/confirm", json={}).status_code == 200

    # 刷新页面（重新拉列表）后查看旧方案：仍可打开，保留其历史门禁结论，且被标记取代
    plans = {p["plan_id"]: p for p in client.get("/api/plans").json()["plans"]}
    old = plans[plan1["plan_id"]]
    assert old["superseded_by"] == plan2["plan_id"]
    old_detail = client.get(f"/api/plans/{plan1['plan_id']}").json()
    assert old_detail["result"]["best_solution"] == ["V_TIN", "V_TOUT"]
    # 该方案关联的原始证据（含已结案的矛盾证据）仍挂在历史方案上
    linked_codes = {e["evidence_code"] for e in old_detail["linked_evidence"]}
    assert bad["evidence_code"] in linked_codes

    # 旧证据的版本化解释可重放：提交当时=矛盾；当前=已核验（纠正后）
    replay = client.get(f"/api/evidence/{bad['evidence_code']}/replay").json()
    assert replay["eval_version"] == events.EVIDENCE_EVAL_VERSION
    assert replay["interpretation_at_submission"]["status"] == verification.STATUS_CONTRADICTION
    assert replay["interpretation_at_submission"]["model_valve"]["is_open"] is False
    assert replay["interpretation_current"]["status"] == verification.STATUS_VERIFIED
    assert replay["reviews"] and replay["reviews"][0]["disposition"] == "correct_model"
    # 原始快照未被改写
    assert replay["snapshot_fingerprint"]


def test_review_confirm_model_keeps_valve_pending_reverification(client):
    """复核维持模型：矛盾解除（resolved），但该阀仍需重新核验才能确认方案。"""
    plan = _plan(client)
    _state(client, "V_TIN", False)
    bad = _observe(client, "V_TIN", "open", at=_iso(2))
    r = client.post(
        f"/api/evidence/{bad['evidence_code']}/review",
        json={"disposition": "confirm_model", "note": "现场误读，模型正确"},
    )
    assert r.json()["assessment"]["status"] == verification.STATUS_RESOLVED
    review = client.get(f"/api/plans/{plan['plan_id']}").json()
    assert review["status"] == "blocked"
    check = next(c for c in review["valve_checks"] if c["valve_id"] == "V_TIN")
    assert "重新现场核验" in check["reason"]
    # 结案后再次提交结案型复核被拒
    again = client.post(
        f"/api/evidence/{bad['evidence_code']}/review",
        json={"disposition": "correct_model"},
    )
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# 事件链完整性与重置
# ---------------------------------------------------------------------------


def test_event_chain_hash_is_verifiable_and_reset_preserves_history(client):
    _plan(client)
    _state(client, "V_TIN", False)
    _observe(client, "V_TIN", "closed", at=_iso(1))
    chain = client.get("/api/events/verify").json()
    assert chain["ok"] and chain["count"] >= 3

    types = [e["event_type"] for e in client.get("/api/events").json()["events"]]
    assert "plan_created" in types
    assert "valve_state_changed" in types
    assert "evidence_submitted" in types

    # 重置：新 model_reset 事件，阀态回初始，历史证据保留但随阀态变化过期
    client.post("/api/reset")
    topo = client.get("/api/topology").json()
    assert all(v["is_open"] and not v["locked"] for v in topo["valves"])
    listed = client.get("/api/evidence").json()["evidence"]
    ev = next(e for e in listed if e["valve_id"] == "V_TIN")
    assert ev["status"] == verification.STATUS_STALE
    assert ev["stale_reason"] == "valve_model_changed"
    assert client.get("/api/events/verify").json()["ok"]


def test_evidence_snapshot_is_immutable_snapshot(client):
    """快照在提交后定型；随后阀态变化不影响快照内容。"""
    plan = _plan(client)
    _state(client, "V_TIN", False)
    ev = _observe(client, "V_TIN", "closed", at=_iso(1), plan_id=plan["plan_id"])
    _state(client, "V_TIN", True)
    replay = client.get(f"/api/evidence/{ev['evidence_code']}/replay").json()
    assert replay["interpretation_at_submission"]["model_valve"]["is_open"] is False
    # 当前解释：阀后来重开 -> 证据过期，原始记录仍在
    assert replay["interpretation_current"]["status"] == verification.STATUS_STALE


def test_bad_inputs_are_rejected(client):
    assert client.post("/api/evidence", json={"valve_id": "NOPE", "observed": "closed"}).status_code == 404
    assert (
        client.post(
            "/api/evidence", json={"valve_id": "V_TIN", "observed": "closed", "observed_at": "not-a-time"}
        ).status_code
        == 400
    )
    assert client.post("/api/evidence", json={"valve_id": "V_TIN", "observed": "maybe"}).status_code == 422
    assert client.get("/api/evidence/EV-999").status_code == 404
    assert client.post("/api/evidence/EV-999/review", json={"disposition": "correct_model"}).status_code == 404
    assert client.post("/api/plans/999/confirm", json={}).status_code == 404
