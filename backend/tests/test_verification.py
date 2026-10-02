"""阀门核验证据工作流验收测试。

覆盖验收标准：
1. 与模型一致的关阀核验使相关方案显示可确认；
2. 观察到开启而模型认为关闭时方案被阻塞并标出受影响残余路径；
3. 较早或重复观察在较新核验之后到达时当前状态不倒退；
4. 完成复核后重新计算、查看旧方案：新方案可确认，旧证据与其版本化解释仍可重放。

边界：所有“确认/核验”均为演示工作流状态，不代表真实安全确认。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.seed import seed_database

T0 = datetime(2026, 10, 2, 8, 0, 0, tzinfo=timezone.utc)


def _reset_seed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _close_valves(client: TestClient, *valve_ids: str) -> None:
    for vid in valve_ids:
        r = client.post(f"/api/valves/{vid}/state", json={"is_open": False})
        assert r.status_code == 200, r.text


def _submit(client: TestClient, valve_id: str, observed: str, observed_at: datetime, plan_id=None):
    r = client.post(
        "/api/evidence",
        json={
            "valve_id": valve_id,
            "observed": observed,
            "observed_at": _iso(observed_at),
            "plan_id": plan_id,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- 验收 1：一致核验 -> 方案可确认 ----------------

def test_consistent_close_verification_makes_plan_confirmable():
    _reset_seed()
    with TestClient(app) as client:
        # 模型中关闭方案涉及的两只阀（模拟现场已操作）
        _close_valves(client, "V_TIN", "V_TOUT")

        r = client.post("/api/isolation", json={"target_id": "T"})
        plan = r.json()
        assert plan["feasible"] is True
        assert plan["plan_id"]
        # 两阀已在模型中关闭，方案的隔离结论依赖它们保持关闭
        assert set(plan["assumed_closed_valves"]) == {"V_TIN", "V_TOUT"}

        # 培训人员核验：两阀均已关闭，与模型一致
        ev1 = _submit(client, "V_TIN", "closed", T0, plan["plan_id"])
        ev2 = _submit(client, "V_TOUT", "closed", T0 + timedelta(minutes=1), plan["plan_id"])
        assert ev1["status"] == "verified" and ev1["effective"] is True
        assert ev2["status"] == "verified"
        assert ev1["seq"] and ev2["seq"] > ev1["seq"]  # 提交序号递增
        assert ev1["snapshot"]["topology_version"] == plan["topology_version"]

        # 拓扑中显示已核验；方案显示可确认且确认成功
        topo = client.get("/api/topology").json()
        verif = {v["id"]: v["verification"] for v in topo["valves"]}
        assert verif["V_TIN"]["status"] == "verified"
        assert verif["V_TOUT"]["status"] == "verified"

        plans = {p["id"]: p for p in client.get("/api/plans").json()}
        assert plans[plan["plan_id"]]["status"] == "confirmable"
        ok = client.post(f"/api/plans/{plan['plan_id']}/confirm")
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "confirmed"


# ---------------- 验收 2：矛盾证据阻塞方案并标出受影响残余路径 ----------------

def test_contradiction_blocks_plan_and_marks_affected_residual_path():
    _reset_seed()
    with TestClient(app) as client:
        # 模型认为出口阀已关闭；据此算出方案（只关入口阀即可隔离）
        _close_valves(client, "V_TOUT")
        plan = client.post("/api/isolation", json={"target_id": "T"}).json()
        assert plan["feasible"] is True
        assert plan["best_solution"] == ["V_TIN"]
        assert plan["confirmable"] is True

        # 现场观察：出口阀其实是开着的 —— 与模型矛盾
        ev = _submit(client, "V_TOUT", "open", T0, plan["plan_id"])
        assert ev["status"] == "contradiction"

        # 矛盾证据不得偷偷改写模型阀态
        topo = client.get("/api/topology").json()
        v_tout = next(v for v in topo["valves"] if v["id"] == "V_TOUT")
        assert v_tout["is_open"] is False
        assert v_tout["verification"]["status"] == "contradiction"

        # 待处置矛盾标出受影响残余路径：来源经 V_TOUT 回到目标 T
        assert len(topo["dispositions"]) == 1
        disp = topo["dispositions"][0]
        assert disp["valve_id"] == "V_TOUT"
        path = disp["affected_residual_path"]
        assert path is not None
        assert path["nodes"][0] == "SRC" and path["nodes"][-1] == "T"
        assert "V_TOUT" in path["valves"]

        # 依赖该阀的方案被阻塞：确认返回 409
        plans = {p["id"]: p for p in client.get("/api/plans").json()}
        assert plans[plan["plan_id"]]["status"] == "blocked"
        assert plans[plan["plan_id"]]["blocked_by"] == ["V_TOUT"]
        blocked = client.post(f"/api/plans/{plan['plan_id']}/confirm")
        assert blocked.status_code == 409
        assert "V_TOUT" in blocked.json()["detail"]

        # 重新计算：新方案同样被阻塞，且响应中逐方案标出 blocked_by
        r2 = client.post("/api/isolation", json={"target_id": "T"}).json()
        assert r2["confirmable"] is False
        assert "V_TOUT" in r2["blocked_reason"]
        assert r2["solutions"][0]["blocked_by"] == ["V_TOUT"]
        assert r2["dispositions"][0]["affected_residual_path"]["nodes"] == path["nodes"]


# ---------------- 验收 3：迟到/重复观察不倒退 ----------------

def test_late_or_duplicate_observation_does_not_regress():
    _reset_seed()
    with TestClient(app) as client:
        _close_valves(client, "V_TIN")
        plan = client.post("/api/isolation", json={"target_id": "T"}).json()

        # 较新的核验：t2 时刻观察到关闭，与模型一致
        t2 = T0 + timedelta(hours=1)
        ev_new = _submit(client, "V_TIN", "closed", t2, plan["plan_id"])
        assert ev_new["status"] == "verified"

        # 迟到观察：t1 < t2 时刻的“开启”观察在较新核验之后到达
        late = _submit(client, "V_TIN", "open", T0, plan["plan_id"])
        assert late["effective"] is False
        assert late["status"] == "superseded"

        # 重复观察：与当前有效证据同一发生时刻
        dup = _submit(client, "V_TIN", "closed", t2, plan["plan_id"])
        assert dup["effective"] is False
        assert dup["status"] == "superseded"

        # 当前状态不倒退：当前证据仍是已核验，无待处置矛盾，方案仍可确认
        topo = client.get("/api/topology").json()
        v_tin = next(v for v in topo["valves"] if v["id"] == "V_TIN")
        assert v_tin["verification"]["status"] == "verified"
        assert v_tin["verification"]["evidence_id"] == ev_new["id"]
        assert topo["dispositions"] == []
        plans = {p["id"]: p for p in client.get("/api/plans").json()}
        assert plans[plan["plan_id"]]["status"] == "confirmable"

        # 迟到/重复的原始记录仍保留在历史中
        history = client.get("/api/evidence").json()
        by_id = {e["id"]: e for e in history}
        assert by_id[late["id"]]["status"] == "superseded"
        assert by_id[dup["id"]]["status"] == "superseded"
        assert by_id[ev_new["id"]]["status"] == "verified"
        assert len(history) == 3


# ---------------- 验收 4：复核 -> 重算 -> 旧方案与旧证据可回放 ----------------

def test_review_then_recompute_old_plan_and_evidence_replayable():
    _reset_seed()
    with TestClient(app) as client:
        _close_valves(client, "V_TOUT")
        old_plan = client.post("/api/isolation", json={"target_id": "T"}).json()
        ev = _submit(client, "V_TOUT", "open", T0, old_plan["plan_id"])
        assert ev["status"] == "contradiction"

        # 无矛盾可处置的阀门复核应被拒绝
        no_conflict = client.post("/api/valves/V_TIN/review", json={"action": "dismiss"})
        assert no_conflict.status_code == 409

        # 显式复核：采纳观察并纠正模型（事件链记录 review_completed）
        review = client.post(
            "/api/valves/V_TOUT/review",
            json={"action": "correct_model", "note": "现场复核确认出口阀未关"},
        )
        assert review.status_code == 200, review.text
        assert review.json()["model_is_open"] is True

        # 纠正后：模型阀态被显式改正，矛盾解除
        topo = client.get("/api/topology").json()
        v_tout = next(v for v in topo["valves"] if v["id"] == "V_TOUT")
        assert v_tout["is_open"] is True
        assert topo["dispositions"] == []

        # 重新计算：新方案基于新版本，可确认
        new_plan = client.post("/api/isolation", json={"target_id": "T"}).json()
        assert new_plan["topology_version"] > old_plan["topology_version"]
        assert new_plan["confirmable"] is True
        ok = client.post(f"/api/plans/{new_plan['plan_id']}/confirm")
        assert ok.status_code == 200

        # 旧方案已过期不可确认，但版本化解释完整可回放
        old = client.get(f"/api/plans/{old_plan['plan_id']}").json()
        assert old["status"] == "stale"
        assert old["stale"] is True
        assert f"v{old_plan['topology_version']}" in old["explanation"]
        assert f"v{new_plan['topology_version']}" in old["explanation"]
        stale_confirm = client.post(f"/api/plans/{old_plan['plan_id']}/confirm")
        assert stale_confirm.status_code == 409

        # 旧证据原始记录保留：观察值、提交时模型快照、复核结论都可查
        old_ev = next(e for e in old["evidence"] if e["id"] == ev["id"])
        assert old_ev["observed"] == "open"
        assert old_ev["model_is_open"] is False  # 提交时模型认为关闭（原始记录）
        assert old_ev["status"] == "resolved_corrected"
        assert old_ev["resolved_seq"] is not None
        history = client.get("/api/evidence").json()
        assert any(e["id"] == ev["id"] for e in history)

        # 事件链可重放：完整记录本次流程的关键事件且序号递增
        events = client.get("/api/events").json()
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        types = [e["type"] for e in events]
        for expected in (
            "valve_state_changed",
            "plan_computed",
            "evidence_submitted",
            "review_completed",
            "plan_computed",
            "plan_confirmed",
        ):
            assert expected in types, types
        review_event = next(e for e in events if e["type"] == "review_completed")
        assert review_event["payload"]["evidence_id"] == ev["id"]
        assert review_event["payload"]["action"] == "correct_model"


# ---------------- 补充：过期与驳回 ----------------

def test_model_change_expires_old_evidence_but_keeps_record():
    """拓扑/阀态后续改变使旧证据过期，原始记录保留。"""
    _reset_seed()
    with TestClient(app) as client:
        _close_valves(client, "V_TIN")
        ev = _submit(client, "V_TIN", "closed", T0)
        assert ev["status"] == "verified"

        # 模型阀态改变（重新打开）后，旧证据过期但记录保留
        client.post("/api/valves/V_TIN/state", json={"is_open": True})
        history = client.get("/api/evidence").json()
        old = next(e for e in history if e["id"] == ev["id"])
        assert old["status"] == "expired"
        assert old["observed"] == "closed"
        assert old["model_is_open"] is False
        assert old["snapshot"]["valves"]["V_TIN"]["is_open"] is False


def test_dismiss_review_keeps_model_and_unblocks():
    """驳回观察：模型保持不变，阻塞解除。"""
    _reset_seed()
    with TestClient(app) as client:
        _close_valves(client, "V_TOUT")
        plan = client.post("/api/isolation", json={"target_id": "T"}).json()
        _submit(client, "V_TOUT", "open", T0, plan["plan_id"])
        assert client.post(f"/api/plans/{plan['plan_id']}/confirm").status_code == 409

        r = client.post("/api/valves/V_TOUT/review", json={"action": "dismiss"})
        assert r.status_code == 200
        topo = client.get("/api/topology").json()
        v_tout = next(v for v in topo["valves"] if v["id"] == "V_TOUT")
        assert v_tout["is_open"] is False  # 模型未被改写
        assert topo["dispositions"] == []

        plans = {p["id"]: p for p in client.get("/api/plans").json()}
        assert plans[plan["plan_id"]]["status"] == "confirmable"
        assert client.post(f"/api/plans/{plan['plan_id']}/confirm").status_code == 200


def test_unknown_observation_is_pending_and_cannot_correct_model():
    """观察值未知 -> 待核验；不能据未知观察纠正模型。"""
    _reset_seed()
    with TestClient(app) as client:
        ev = _submit(client, "V1", "unknown", T0)
        assert ev["status"] == "pending"
        topo = client.get("/api/topology").json()
        v1 = next(v for v in topo["valves"] if v["id"] == "V1")
        assert v1["verification"]["status"] == "pending"
        assert topo["dispositions"] == []  # 未知不构成矛盾，不阻塞方案


def test_locked_valve_cannot_change_state_and_unknown_valve_rejected():
    _reset_seed()
    with TestClient(app) as client:
        client.post("/api/valves/V_TIN/lock", json={"locked": True})
        r = client.post("/api/valves/V_TIN/state", json={"is_open": False})
        assert r.status_code == 409
        r2 = client.post(
            "/api/evidence",
            json={"valve_id": "NOPE", "observed": "open", "observed_at": _iso(T0)},
        )
        assert r2.status_code == 404
