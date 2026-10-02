"""数据库模型：节点（设备/供给点）、管段、阀门、事件链与核验投影。"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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


class ModelMeta(Base):
    """单行元数据：阀门模型当前版本号（每次阀态/锁定/重置类事件自增）。

    证据按其提交时的 model_revision 与拓扑指纹判断是否“过期”：
    只要证据之后发生过模型层面的变更，旧证据即成为历史记录，
    原始快照与解释仍可重放，但不再代表当前阀态。
    """

    __tablename__ = "model_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    singleton: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, unique=True)
    model_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EventLog(Base):
    """仅追加（append-only）的持久化事件链。

    任何状态变更或核验动作都先落事件再更新投影；行永不修改、永不删除。
    prev_hash/self_hash 组成 SHA-256 哈希链，可随时重放校验。
    payload 为不可变 JSON 文本（证据含拓扑快照、方案序号等）。
    时间统一存 UTC ISO-8601 字符串，observed_at 由培训人员显式给出，
    允许“迟到观察”（提交晚、发生时刻早）。
    """

    __tablename__ = "event_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    self_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class ValveEvidence(Base):
    """阀门核验证据投影（内容由 evidence_submitted 事件固化，行不可变）。

    observed_open: 现场观察值 —— True=开 / False=关 / None=未知。
    plan_id:       关联的本次隔离方案；也允许无方案的独立核验。
    topology_snapshot_json: 提交时刻的完整拓扑 + 阀态 + 模型版本快照。
    复核/纠正动作只追加 review 事件，不改写本行；当前状态由
    verification 模块依据事件链实时派生。
    """

    __tablename__ = "valve_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    event_seq: Mapped[int] = mapped_column(ForeignKey("event_log.seq"), nullable=False, unique=True)
    valve_id: Mapped[str] = mapped_column(ForeignKey("valves.id"), nullable=False)
    observed_open: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    observed_at: Mapped[str] = mapped_column(String(40), nullable=False)
    submitted_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    model_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    topology_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    topology_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("isolation_plans.id"), nullable=True)
    observer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class IsolationPlan(Base):
    """隔离方案记录（每次 /api/isolation 计算生成一条，历史可查、可重放）。

    result_json 为创建时刻的完整计算结果（方案版本化解释的一部分）；
    close_valves_json 为方案要求关闭的阀门，核验门禁据此判断。
    确认动作只追加 plan_confirmed 事件，confirmed_seq 为其投影。
    """

    __tablename__ = "isolation_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    event_seq: Mapped[int] = mapped_column(ForeignKey("event_log.seq"), nullable=False, unique=True)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    close_valves_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    locks_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    model_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    topology_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
