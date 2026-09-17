# League Points Ledger

本地运行的联赛积分账本 API：Python + FastAPI + SQLite。

- **不可变事件**：积分以事件形式追加写入，写后不可改。
- **幂等写入**：每个 `event_id` 只生效一次；相同载荷重放返回原结果（200），相同 `event_id` 不同载荷返回 **409**。
- **更正链**：更正事件通过 `correction_of` 引用已有事件，更正本身可被再次更正（链式）。任一 revision 上，每条更正链只计**当时的末端事件**。
- **递增 revision**：每次成功写入获得严格递增的 revision；可查询当前或任一历史 revision 的排行榜与生效明细。
- **并发安全**：写入经 `BEGIN IMMEDIATE` 串行化，revision 在写锁内分配——并发写入不会产生重复 revision 或中间态。

## 快速启动

```bash
docker compose up --build
```

服务启动后访问 <http://localhost:8000>，交互式文档见 <http://localhost:8000/docs>。

数据持久化在命名卷 `ledger-data` 中（SQLite 文件 `/data/ledger.db`）。

## API

### 写入事件 `POST /events`

```json
{
  "event_id": "match-001-alice",
  "participant_id": "alice",
  "points": 10,
  "correction_of": null
}
```

| 场景 | 状态码 | 说明 |
|---|---|---|
| 首次写入 | `201` | 返回事件及分配的 `revision` |
| 相同 `event_id` + 相同载荷重放 | `200` | 返回原结果，不产生新 revision |
| 相同 `event_id` + 不同载荷 | `409` | 冲突，拒绝写入 |
| `correction_of` 引用不存在的事件 / 自引用 | `422` | 校验失败 |

### 更正事件

```bash
# 原事件
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e1","participant_id":"alice","points":10}'

# 第一次更正（引用 e1）
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e2","participant_id":"alice","points":12,"correction_of":"e1"}'

# 再次更正（引用 e2，形成更正链 e1 -> e2 -> e3）
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e3","participant_id":"alice","points":15,"correction_of":"e2"}'
```

链 `e1 → e2 → e3` 中：revision ≥ e3 时只有 `e3` 计入；在 e3 写入之前、e2 写入之后，只有 `e2` 计入。

### 排行榜 `GET /leaderboard`

```bash
curl localhost:8000/leaderboard                # 当前 revision
curl 'localhost:8000/leaderboard?revision=3'   # 指定历史 revision
```

返回按**总分降序、`participant_id` 升序**排列，并含每个参赛者的生效事件明细：

```json
{
  "revision": 3,
  "entries": [
    {
      "rank": 1,
      "participant_id": "alice",
      "total_points": 15,
      "effective_events": [
        {"event_id": "e3", "points": 15, "revision": 3, "correction_of": "e2"}
      ]
    }
  ]
}
```

查询不存在的 revision（`< 1` 或大于当前最大 revision）返回 `404`。

### 其他端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `GET` | `/events` | 全部事件（按 revision 升序） |
| `GET` | `/events/{event_id}` | 单个事件 |

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
LEDGER_DB_PATH=./ledger.db uvicorn app.main:app --port 8000
```

## 测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

测试覆盖：幂等重放、409 冲突、更正链末端计数、历史 revision 查询、排行榜排序，以及 50 路并发写入下 revision 唯一且连续。

## 设计要点

- **存储**：单表 `events`（`event_id` 主键，`revision` 唯一），WAL 模式。
- **幂等**：请求载荷序列化为规范化 JSON（键排序）随事件存储；重放时比对，一致返回原结果，不一致返回 409。
- **生效判定**：revision R 上事件 e 生效 ⟺ `e.revision ≤ R` 且不存在 `c.revision ≤ R` 的更正事件 `c.correction_of = e.event_id`。
- **并发**：所有写操作在 `BEGIN IMMEDIATE` 事务内先取写锁、再计算 `MAX(revision)+1` 并插入，配合 `busy_timeout`，写者完全串行；读者走 WAL 快照，互不阻塞。
