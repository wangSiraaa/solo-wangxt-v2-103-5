"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ValveLockIn(BaseModel):
    locked: bool = Field(..., description="True=锁定该阀门（保持现状不可操作），False=解锁")


class ValveStateIn(BaseModel):
    is_open: bool = Field(..., description="模型阀态：True=开，False=关（演示用模拟现场操作）")


class EvidenceIn(BaseModel):
    valve_id: str = Field(..., description="被核验阀门 id")
    observed: Literal["open", "closed", "unknown"] = Field(..., description="现场观察值：开/关/未知")
    observed_at: datetime = Field(..., description="观察发生时刻（ISO 8601）")
    plan_id: str | None = Field(None, description="关联的隔离方案 id（通常为最近一次计算）")
    note: str = Field("", description="备注")


class ReviewIn(BaseModel):
    action: Literal["correct_model", "dismiss"] = Field(
        ..., description="correct_model=采纳观察并纠正模型；dismiss=驳回观察"
    )
    note: str = Field("", description="复核备注")


class IsolationIn(BaseModel):
    target_id: str = Field("T", description="待隔离目标设备节点 id")
    locks: dict[str, bool] | None = Field(
        None, description="可选：一次性提交的阀门锁定状态 {valve_id: locked}"
    )


class NodeOut(BaseModel):
    id: str
    name: str
    kind: str
    x: float
    y: float
    essential: bool


class ValveOut(BaseModel):
    id: str
    name: str
    segment_id: str
    endpoints: list[str]
    is_open: bool
    locked: bool
    operable: bool
    is_bypass: bool
    verification: dict[str, Any] | None = None  # 当前有效核验证据的推导状态


class SegmentOut(BaseModel):
    id: str
    source: str
    target: str
    direction: str
    kind: str
    is_bypass: bool
    valve_id: str | None


class TopologyOut(BaseModel):
    nodes: list[NodeOut]
    segments: list[SegmentOut]
    valves: list[ValveOut]
    topology_version: int = 0
    dispositions: list[dict[str, Any]] = []  # 待处置矛盾（含受影响残余路径）


class IsolationSolution(BaseModel):
    close_valves: list[str]
    size: int
    alternative_rank: int
    closes_bypass_valves: list[str] = []
    supply_paths: dict[str, list[str] | None] = {}


class ResidualPath(BaseModel):
    nodes: list[str]
    valves: list[str | None]
    locked_valves_on_path: list[str]
    uses_bypass: bool


class UnconstrainedBest(BaseModel):
    close_valves: list[str]
    size: int
    disconnects_essentials: list[str]
    unavoidable_essentials: list[str] = []


class IsolationOut(BaseModel):
    feasible: bool
    target_id: str
    sources: list[str]
    essentials: list[str]
    candidate_valves: list[ValveOut]
    assumed_closed_valves: list[str] = []
    examined_combinations: int = 0
    solutions: list[dict[str, Any]] = []
    best_solution: list[str] = []
    residual_path: dict[str, Any] | None = None
    locked_witness_path: dict[str, Any] | None = None
    infeasible_reason: str | None = None
    unconstrained_best: dict[str, Any] | None = None
    # 核验证据工作流
    plan_id: str | None = None
    topology_version: int = 0
    confirmable: bool = False
    blocked_reason: str | None = None
    dispositions: list[dict[str, Any]] = []


class EvidenceOut(BaseModel):
    id: str
    seq: int
    valve_id: str
    observed: str
    observed_at: datetime
    plan_id: str | None
    topology_version: int
    model_is_open: bool
    model_locked: bool
    effective: bool
    status: str
    status_label: str
    resolution: str | None
    resolved_seq: int | None
    note: str
    snapshot: dict[str, Any]
    created_at: datetime


class PlanSummaryOut(BaseModel):
    id: str
    seq: int
    target_id: str
    feasible: bool
    topology_version: int
    close_valves: list[str]
    assumed_closed_valves: list[str]
    status: str
    status_label: str
    blocked_by: list[str]
    confirmed_seq: int | None
    created_at: datetime


class PlanDetailOut(PlanSummaryOut):
    result: dict[str, Any]
    current_topology_version: int
    stale: bool
    explanation: str  # 版本化解释：方案基于哪个模型版本计算、当前是否仍适用
    evidence: list[EvidenceOut]  # 关联到本方案的核验证据（原始记录）


class EventOut(BaseModel):
    seq: int
    type: str
    created_at: datetime
    payload: dict[str, Any]
