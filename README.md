# 管网隔离方案培训演示（Isolation Training Demo）

面向工艺培训的**纯演示系统**：在一张固定的模拟管网上，计算隔离目标设备所需关闭的
阀门候选集合，并演示“阀门核验证据”工作流。**不连接任何真实控制系统，计算结果、
核验证据与方案确认均不代表真实检修已满足安全隔离条件。**

- 前端：Angular 19 + Cytoscape.js（拓扑、管段方向、旁路、阀门锁定、方案与残余路径高亮、
  核验证据状态着色与矛盾影响的残余路径标记）
- 后端：FastAPI + NetworkX（无向物理连通图上的候选阀门集合枚举与约束校验、
  证据状态推导、方案确认门禁）
- 存储：PostgreSQL（节点连接、阀门开闭/锁定、必要供给点、持久化事件链、
  隔离方案版本、核验证据原始记录；本地无 PG 时自动回退 SQLite）

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

> 仅关 T 一侧阀门时隔离不成立：例如只关 `V_TIN`，介质仍可经旁路绕回 N3 再回到 T
> （`N2→BP→N3→T`），或经环网绕回。这正是样例 1 必须两侧同关的原因。

## 阀门核验证据工作流

培训人员可记录模拟阀门的开闭核验观察。每条证据保存：阀门、观察值（开/关/未知）、
发生时刻、提交时的拓扑快照、关联的隔离方案与提交序号（事件链 seq）。

- **状态推导**（与当前模型阀态及锁定对比，原始记录永不改写）：
  已核验（一致）/ 待核验（观察为未知）/ 存在矛盾（不一致）/
  已过期（拓扑或阀态后续改变）/ 未生效（迟到、重复或被更新的证据取代）。
- **矛盾不自动采信任一方**：矛盾证据不会偷偷改写模型阀态，而是建立“待处置”状态，
  阻止依赖该阀（候选关闭集合或模型已关闭假设）的隔离方案被确认，
  并标出“若观察属实”仍连通的受影响残余路径；
  只有显式的复核/纠正事件（采纳观察并纠正模型 / 驳回观察）才能解除阻塞。
- **迟到/重复观察**（发生时刻不晚于该阀当前有效证据）只存档、不生效，
  当前状态不倒退；拓扑或阀态改变使旧证据过期，但原始记录保留可重放。
- **方案版本化**：每次计算持久化一个方案（含拓扑版本），模型变化后旧方案变为
  “已过期”仅供回放；事件链（`GET /api/events`）完整记录全部变更，可逐步重放。

## 本地运行

### 后端（无 PostgreSQL 时自动用 SQLite 文件）

```bash
cd backend
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
# API: http://127.0.0.1:8000/api/topology, /api/isolation, /api/valves/{id}/lock, /api/reset
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

- `GET /api/topology`：节点 / 有向管段 / 阀门（含 `is_open`、`locked`、`is_bypass`、
  当前核验状态 `verification`）、拓扑版本、待处置矛盾 `dispositions`
- `POST /api/isolation`：body `{ "target_id": "T", "locks": {"V_TIN": true} }`
  - 可行：`best_solution`（最少阀门）、等价方案、方案后每个必要供给点的来源路径
  - 不可行：`residual_path`（仍连通的一条残余路径）、`locked_witness_path`（经锁定阀的见证路径）、
    `unconstrained_best.unavoidable_essentials`（任何切法都无法保住的供给点）
  - 每次计算持久化方案并返回 `plan_id`、`confirmable`、逐方案 `blocked_by`
- `POST /api/valves/{valve_id}/lock`：持久化单只阀门锁定状态
- `POST /api/valves/{valve_id}/state`：模拟现场操作，改变模型阀态（锁定阀拒绝）
- `POST /api/evidence`：提交核验证据 `{valve_id, observed, observed_at, plan_id?, note?}`
- `GET /api/evidence`：全部证据历史（含过期/被取代的原始记录）
- `POST /api/valves/{valve_id}/review`：显式复核 `{action: correct_model|dismiss}`
- `GET /api/plans` / `GET /api/plans/{id}`：方案列表 / 版本化详情与关联证据
- `POST /api/plans/{id}/confirm`：确认方案（矛盾阻塞或已过期返回 409）
- `GET /api/events`：持久化事件链（重放）
- `POST /api/reset`：全部阀门恢复打开、未锁定（证据与方案历史保留）

## 安全边界声明

系统仅对**给定拓扑与阀门模型**做枚举演示：假设阀门与管段 1:1、关阀即断边、
无背压/泄漏/盲板/双阀双断等真实工况要素。核验证据、矛盾处置与方案“确认”
仅为演示工作流状态。任何输出**不得**作为真实检修隔离（LOTO）的安全依据。
