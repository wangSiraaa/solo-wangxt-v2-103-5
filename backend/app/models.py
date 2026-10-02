"""数据库模型：节点（设备/供给点）、管段、阀门、事件链、隔离方案、核验证据。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # source/equipment/consumer/junction
    x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Valve(Base):
    """阀门与其所在管段一一对应（demo 模型）。

    is_open:       阀门当前实际开闭状态；锁定后用户不可再改变其操作状态。
    locked:        用户锁定标记，锁定阀门不参与候选关闭集合。
                   初始状态全部为打开、未锁定；当前演示中可操作的动作是“关闭”，
                   因此锁定等价于“禁止关闭”（保持打开）。
    operable:      工艺上是否允许操作（个别阀门检修/铅封，恒不可用）。
    """

    __tablename__ = "valves"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    operable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    segment: Mapped["Segment"] = relationship(back_populates="valve")


class Segment(Base):
    """有向管段：upstream -> downstream 表示介质名义流向。

    隔离计算按“物理连通”使用无向图（介质可被两侧隔离边界切断，
    且检修隔离关注连通性而非流向）；direction 用于前端箭头标注、
    来源/下游识别与结果解释。旁路是与主管并联的一对管段。
    """

    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    upstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    downstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="main")  # main/bypass/branch
    is_bypass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    valve: Mapped["Valve | None"] = relationship(back_populates="segment", uselist=False)

    __table_args__ = (UniqueConstraint("upstream_id", "downstream_id", name="uq_segment_endpoints"),)


class MetaState(Base):
    """全局元状态（键值）。目前保存 topology_version：任何阀门开闭/锁定/拓扑
    变更都会使其递增，用于判定旧证据过期与旧方案失效。"""

    __tablename__ = "meta_state"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(128), nullable=False)


class EventLog(Base):
    """持久化事件链：所有模型/证据/方案/复核变更按提交序号 seq 追加，可重放。

    seq 即“提交序号”（自增主键），证据与方案通过它关联到事件链。
    """

    __tablename__ = "event_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class IsolationPlan(Base):
    """一次隔离计算产出的方案（版本化保存，供核验证据关联与事后回放）。

    topology_version 记录计算所基于的模型版本；模型后续变化后该方案变为
    stale（仅供回放，不可确认），其 result 快照即为版本化解释。
    """

    __tablename__ = "isolation_plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # PLAN-<seq>
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 计算事件提交序号
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    topology_version: Mapped[int] = mapped_column(Integer, nullable=False)
    close_valves: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 计算时模型中已关闭的阀门：方案的隔离结论隐含依赖它们保持关闭
    assumed_closed_valves: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confirmed_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Evidence(Base):
    """阀门核验证据（原始记录永不改写、永不删除；显示状态由推导得出）。

    observed:        现场观察值 open/closed/unknown。
    observed_at:     观察发生时刻（提交方提供，UTC）。
    effective:       提交时是否生效（迟到/重复观察只存档、不生效）。
    model_is_open / model_locked / topology_version / snapshot:
                     提交时刻的模型阀态与拓扑快照（原始记录的一部分）。
    resolution:      复核结论 corrected/dismissed；未复核为 None。
    """

    __tablename__ = "valve_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # EV-<seq>
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 提交序号（事件链）
    valve_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    observed: Mapped[str] = mapped_column(String(8), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topology_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model_locked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    note: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    effective: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolved_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
