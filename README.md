# 管网隔离方案培训演示（Isolation Training Demo）

面向工艺培训的**纯演示系统**：在一张固定的模拟管网上，计算隔离目标设备所需关闭的
阀门候选集合，并由培训人员记录**阀门开闭核验证据**，用证据驱动方案的核验门禁。
**不连接任何真实控制系统，证据与方案确认均不代表真实检修已满足安全隔离条件（LOTO）。**

- 前端：Angular 19 + Cytoscape.js（拓扑、管段方向、旁路、阀门锁定、方案/残余路径、
  阀门核验状态与矛盾影响路径标记）
- 后端：FastAPI + NetworkX（候选阀门集合枚举、约束校验、矛盾观察覆盖下的残余路径复核）
- 存储：PostgreSQL（节点/管段/阀门 + **仅追加事件链**、证据投影、方案记录；
  本地无 PG 时自动回退 SQLite）

## 阀门核验证据（event-sourced）

每条证据保存：**阀门、观察值（开/关/未知）、现场发生时刻、提交时刻拓扑快照、
关联隔离方案、提交序号**。系统将其与*当前模型阀态与锁定状态*对比，派生出
在拓扑页、方案页与历史页统一展示的四种状态：

| 状态 | 含义 |
| --- | --- |
| 已核验 verified | 观察值与模型阀态一致，且证据未被取代/过期 |
| 待核验 pending | 无有效证据、观察值为“未知”，或矛盾维持模型结案后需重新核验 |
| 过期 stale | 该阀模型态后续变更、拓扑结构改变，或证据被更晚观察取代/为重复副本 |
| 矛盾 contradiction | 观察值与模型阀态不一致且未经显式复核处置（待处置） |

关键纪律：

- **矛盾绝不自动改写模型阀态**。模型阀态只能由事件链中的
  `valve_state_changed` / `review_completed(correct_model)` / `model_reset` 改变。
- 未处置矛盾建立**待处置状态**，并阻止依赖该阀的方案确认；NetworkX 用
  “观察覆盖”（不触碰模型）重算并标出**受影响残余路径**与供给点影响。
- **迟到/重复观察不覆盖较新证据**：排序以现场发生时刻为主、提交序号决胜；
  重复副本标记“过期·重复”，当前状态不倒退。
- 拓扑结构或该阀模型态后续变化，旧证据标记“过期”但**原始记录与快照永久保留**。
- 复核是**显式事件**，三种处置：
  - `correct_model`：以现场观察为准纠正模型（自增模型版本），矛盾解除；
  - `confirm_model`：复核维持模型、观察不采信（矛盾解除但该阀需重新核验）；
  - `reinspect`：暂不结案、安排重新检查（阻塞保持）。
- 所有写入先落 `event_log`（SHA-256 哈希链，`prev_hash`/`self_hash`，可随时校验），
  再更新阀态/证据/方案投影；证据行不可变，历史解释可按证据上的
  `eval_version`（`evidence-eval-v1`）**版本化重放**。
- 方案每次计算都生成不可变方案记录（含创建时刻结果与拓扑快照）。方案门禁状态：
  `confirmable / blocked / outdated / infeasible / confirmed`；
  拓扑结构改变或锁定变化使旧方案过期；同目标更新方案会把旧方案标记为“已被取代”，
  但旧方案与其关联证据仍可打开、重放。

## 演示拓扑

```
SRC ─V0─ N1 ─V1─ N2 ─V_TIN─ [T] ─V_TOUT─ N3
          │            ╲  旁路 N2─V_BP_IN─BP─V_BP_OUT─N3 ╱
         V_P1           环网联络 N1─V_LK1─N5─V_LK2─N3
          ↓                          N3 ─V_P2─ P2
          P1
```

- 每条管段显式保存名义方向（upstream→downstream，图上箭头标注）；隔离按**无向物理连通**计算。
- 旁路（紫虚线）与目标设备 T 并联；P1、P2 为**必要供给点**（任何方案不得断供）。
- 阀门与管段 1:1；初始全部打开。锁定 = 禁止关闭（保持现状），可在页面勾选后重新计算。

## 三个培训样例

| 样例 | 锁定 | 结果 |
| --- | --- | --- |
| 1 旁路绕回 | 无 | 关 `V_TIN`+`V_TOUT` 即隔离 T；旁路保持打开，P2 经 `N2→BP→N3` 绕回不断供 |
| 2 锁定入口阀 | `V_TIN` | 最小集合升为 3 阀：`V_TOUT`+`V1`+旁路一只（`V_BP_IN`/`V_BP_OUT` 两个等价方案）；旁路被封，P2 改由环网 `N1→N5→N3` 供料 |
| 3 不应断供的支路 | `V_TOUT` | **无可行方案**：任何切法都不可避免断供 P2（P1 可保住）；页面显示仍连通的残余路径 `SRC→N1→N2→T`，以及经锁定阀的见证路径 `…→N3→T`（含 `V_TOUT`） |

典型核验练习流程：计算方案 →（可选）用“记为关”把操作结果写入模型 →
逐阀提交“关”观察 → 门禁变**可确认**后确认；若提交“开”观察而模型为关，
方案立即被阻塞并显示品红残余路径 → 在证据上执行复核/纠正 → 重新计算得到新方案，
旧方案与旧证据仍可在历史中展开并版本化重放。

## 本地运行

### 后端（无 PostgreSQL 时自动用 SQLite 文件）

```bash
cd backend
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
# 测试: python3 -m pytest tests/ -q
```

指定 PostgreSQL：

```bash
export DATABASE_URL=postgresql+psycopg://isolation:isolation@localhost:5432/isolation_demo
docker compose up -d db        # 或使用任意已有 PG 实例
```

### 前端

```bash
cd frontend
npm install
npm start                      # http://localhost:4200 （/api 代理到 8000）
# 或产物构建后由后端直接托管: npx ng build  → http://127.0.0.1:8000/
```

### 一键（含 PG）

```bash
cd frontend && npm ci && npx ng build && cd ..
docker compose up --build
```

## API 摘要

拓扑与方案：

- `GET /api/topology`：节点 / 有向管段 / 阀门（含 `is_open`、`locked`、`is_bypass`、
  派生字段 `verification`）与 `model_revision`
- `POST /api/isolation`：body `{ "target_id": "T", "locks": {"V_TIN": true} }`；
  每次计算都持久化方案，响应含 `plan`（门禁复核：状态、逐阀核验、阻塞项、
  `affected_residual_paths` 受矛盾影响的残余路径）
- `GET /api/plans` / `GET /api/plans/{id}`：方案历史与详情（含创建时刻结果）
- `POST /api/plans/{id}/confirm`：仅在 `confirmable=true` 时成功（409 否则拒绝），
  响应携带“仅培训记录、非真实安全确认”警示

模型阀态（事件链留痕）：

- `POST /api/valves/{id}/state`：模型侧记录操作结果 `{is_open}`
- `POST /api/valves/{id}/lock`：持久化锁定状态
- `POST /api/reset`：阀态恢复初始并追加 `model_reset` 事件（证据/方案历史保留）

核验证据：

- `POST /api/evidence`：
  `{valve_id, observed: open|closed|unknown, observed_at?, plan_id?, observer?, note?}`；
  `observed_at` 可显式给过去时刻以模拟迟到观察
- `GET /api/evidence`：全部证据的*当前评估*与影响到的方案；原始记录永不删除
- `GET /api/evidence/{code}/replay`：提交当时解释 vs 当前解释（版本化重放，附快照指纹）
- `POST /api/evidence/{code}/review`：
  `{disposition: correct_model|confirm_model|reinspect, note?, reviewer?}`

事件链：

- `GET /api/events`：仅追加事件（含哈希与载荷）；`GET /api/events/verify` 校验哈希链

## 安全边界声明

系统仅对**给定拓扑与阀门模型**做枚举与核验*流程*演示：假设阀门与管段 1:1、
关阀即断边、无背压/泄漏/盲板/双阀双断等真实工况要素。证据由培训人员手工录入，
不接入现场仪表或执行机构。任何输出（包括“已确认”方案）**均不得**作为真实检修
隔离（LOTO）的安全依据。
