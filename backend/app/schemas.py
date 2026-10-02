"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ValveLockIn(BaseModel):
    locked: bool = Field(..., description="True=锁定该阀门（保持现状不可操作），False=解锁")


class ValveStateIn(BaseModel):
    is_open: bool = Field(..., description="模型侧记录的阀态：True=打开，False=关闭")
    note: str | None = Field(None, description="操作票/记录备注（仅培训记录）")


class IsolationIn(BaseModel):
    target_id: str = Field("T", description="待隔离目标设备节点 id")
    locks: dict[str, bool] | None = Field(
        None, description="可选：一次性提交的阀门锁定状态 {valve_id: locked}"
    )


ObservedValue = Literal["open", "closed", "unknown"]


class EvidenceIn(BaseModel):
    valve_id: str = Field(..., description="被核验阀门 id")
    observed: Literal["open", "closed", "unknown"] = Field(
        ..., description="现场观察值：开 / 关 / 未知"
    )
    observed_at: str | None = Field(
        None,
        description="现场发生时刻（ISO-8601）；缺省取提交时刻。显式给出以模拟迟到观察",
    )
    plan_id: int | None = Field(None, description="关联的本次隔离方案 id；可空表示独立核验")
    observer: str | None = Field(None, description="核验人员（培训记录）")
    note: str | None = Field(None, description="备注")


class ReviewIn(BaseModel):
    disposition: Literal["correct_model", "confirm_model", "reinspect"] = Field(
        ...,
        description=(
            "correct_model=以现场观察为准纠正模型阀态；"
            "confirm_model=复核后维持模型、观察不采信（需重新核验）；"
            "reinspect=暂不结案、安排重新检查（阻塞保持）"
        ),
    )
    note: str | None = Field(None, description="复核说明（培训记录）")
    reviewer: str | None = Field(None, description="复核人员")


class ConfirmIn(BaseModel):
    note: str | None = Field(None, description="确认备注（培训记录，非真实安全确认）")
    confirmer: str | None = Field(None, description="确认人")


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
    # 当前核验视图（由证据链派生）：verified/pending/stale/contradiction/resolved
    verification: dict[str, Any] | None = None


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
    model_revision: int = 0


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
    examined_combinations: int = 0
    solutions: list[dict[str, Any]] = []
    best_solution: list[str] = []
    residual_path: dict[str, Any] | None = None
    locked_witness_path: dict[str, Any] | None = None
    infeasible_reason: str | None = None
    unconstrained_best: dict[str, Any] | None = None
    # 本次计算持久化的方案记录与核验门禁复核
    plan: dict[str, Any] | None = None
