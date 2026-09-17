# League Points Ledger

本地运行的联赛积分账本 API：Python + FastAPI + SQLite。

- **不可变事件**：积分以事件形式追加写入，写后不可改。
- **幂等写入**：每个 `event_id` 只生效一次；相同载荷重放返回原结果（200），相同 `event_id` 不同载荷返回 **409**。
- **更正链**：更正事件通过 `correction_of` 引用已有事件，更正本身可被再次更正（链式）。任一 revision 上，每条更正链只计**当时的末端事件**。
- **递增 revision**：每次成功写入获得严格递增的 revision；可查询当前或任一历史 revision 的排行榜与生效明细。
- **游标分页**：`GET /leaderboard/page` 以 HMAC 签名游标分页读取排行榜，游标绑定固定 revision，可并发、可续读。
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

### 排行榜分页 `GET /leaderboard/page`

排序规则与 `/leaderboard` 相同（**总分降序、`participant_id` 升序**），`rank` 跨页连续：

```bash
# 第一页：可选 revision 固定快照（默认当前值）
curl 'localhost:8000/leaderboard/page?limit=2'

# 后续页：携带上一页返回的 next_cursor，无需再传 revision
curl 'localhost:8000/leaderboard/page?limit=2&cursor=<next_cursor>'
```

```json
{
  "revision": 3,
  "entries": [
    {"rank": 1, "participant_id": "alice", "total_points": 15, "effective_events": [...]},
    {"rank": 2, "participant_id": "bob", "total_points": 7, "effective_events": [...]}
  ],
  "next_cursor": "eyJ2IjoxLC...<snip>...9zfQ.5t4k...<snip>...Yw"
}
```

| 参数 | 说明 |
|---|---|
| `limit` | 每页条数，`1–500`，默认 `50` |
| `revision` | 可选；仅用于**首次请求**固定快照（默认当前 revision）。与 cursor 同时提供时必须与游标绑定的 revision 一致 |
| `cursor` | 可选；上一页响应的 `next_cursor`。携带后**始终读取游标绑定的 revision**，并从上一页末条排序键之后继续 |

行为约定：

- 游标为不透明的签名令牌（base64url，格式 `<payload>.<hmac>`），内含版本号、revision、末条总分与 `participant_id`；客户端不应解析或构造。
- 末页 `next_cursor` 为 `null`；空榜返回 `{"revision": 0, "entries": [], "next_cursor": null}`。
- 游标被**篡改**、**格式错误**，或与显式 `revision` **冲突** → `422`；revision 不存在 → `404`。
- 分页期间发生的新写入不影响已在进行的翻页：所有后续页都读游标固定的同一快照。

### 其他端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `GET` | `/events` | 全部事件（按 revision 升序） |
| `GET` | `/events/{event_id}` | 单个事件 |

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LEDGER_DB_PATH` | `/data/ledger.db` | SQLite 数据库文件路径 |
| `LEADERBOARD_CURSOR_SECRET` | `insecure-dev-cursor-secret`（仅开发用） | `/leaderboard/page` 游标的 HMAC-SHA256 签名密钥。**生产环境必须显式设置**；更换密钥后旧游标全部失效（返回 `422`）。docker-compose 中可用 shell 同名变量覆盖 |

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

测试覆盖：幂等重放、409 冲突、更正链末端计数、历史 revision 查询、排行榜排序，50 路并发写入下 revision 唯一且连续，以及分页游标的续读正确性、快照固定、篡改/格式错误/冲突返回 422、末页与空榜行为。

## 设计要点

- **存储**：单表 `events`（`event_id` 主键，`revision` 唯一），WAL 模式。
- **幂等**：请求载荷序列化为规范化 JSON（键排序）随事件存储；重放时比对，一致返回原结果，不一致返回 409。
- **生效判定**：revision R 上事件 e 生效 ⟺ `e.revision ≤ R` 且不存在 `c.revision ≤ R` 的更正事件 `c.correction_of = e.event_id`。
- **并发**：所有写操作在 `BEGIN IMMEDIATE` 事务内先取写锁、再计算 `MAX(revision)+1` 并插入，配合 `busy_timeout`，写者完全串行；读者走 WAL 快照，互不阻塞。
- **分页游标**：游标载荷（版本、revision、末条排序键）以 `LEADERBOARD_CURSOR_SECRET` 为键做 HMAC-SHA256 签名，base64url 拼接为 `<payload>.<sig>`；校验失败一律 422。续页通过排序键 `(total_points, participant_id)` 定位，快照由游标内的 revision 固定。
