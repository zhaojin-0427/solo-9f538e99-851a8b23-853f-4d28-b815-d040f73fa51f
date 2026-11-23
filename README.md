# 包装线封口参数箱样核对 API

一条包装线做封口参数对照试验。切换后设备需要稳定，计划参数必须在**采样开始前 5 分钟**起持续生效到采样结束。本服务用于录入批次、补录/更正切换日志，并给出某批箱样能否纳入分析的结论。

技术栈：FastAPI + SQLite（标准库 `sqlite3`，无 ORM）。启动后打开 `/docs` 即可在线录入和查询。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

数据保存在命名卷 `check-data` 中；重建容器不会丢数据。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

环境变量 `CHECK_DB_PATH` 指定数据库文件，默认 `data/checks.db`。

### 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## 核对规则

| 规则 | 说明 |
| --- | --- |
| 检查窗口 | `[采样开始 − 5 分钟, 采样结束)`，左闭右开 |
| 生效参数 | 某时刻的有效参数 = `switched_at <= 该时刻` 的**最近一条**切换记录 |
| 未知 | 首条切换记录之前，参数视为未知 |
| 合格条件 | 检查窗口**全程**参数已知且等于计划参数 |
| 计划参数比较 | 三个参数分别比较，统一四舍五入到 3 位小数 |
| 切换时刻 | 全局唯一（同时写入 API 校验与数据库唯一约束） |
| 时间格式 | 必须带时区，按 UTC 存储；精确到秒。naive 时间返回 422 |

结果中同时给出：

* `status` / `eligible` / `reason_code`：`ok` / `parameter_mismatch` / `unknown_params`
* `first_problem_at` + `first_problem_kind`：窗口内**最早**的问题时刻（未知或不符取更早者）
* `first_mismatch_at`、`first_unknown_at`：分别给出首个不符、首个未知时刻
* `first_problem_switch_record_no` / `first_problem_switch_switched_at`：该问题对应的切换记录
* `segments`：窗口按切换记录切分的明细，便于人工复核
* `violations`：所有不合格时段

## 切换日志与增量重查

`PUT /switches/{record_no}` 在记录号不存在时补录、已存在时更正。写入后：

1. 比较**新旧两条有效参数时间线**，得到前后参数不同的时段 `changed_intervals`
   （`end = null` 表示一直延伸到无穷）。
2. 只重查检查窗口与这些时段**相交**的批次；不相交的批次保持原结论和原修订号。
3. 每个被重查的批次追加一条修订记录，`transition` 为
   `revoked`（资格撤下）/ `restored`（资格恢复）/ `unchanged`。
4. 切换日志、全部重查结果、批次修订号在**同一事务**内提交。

请求失败（参数校验 422、切换时刻冲突 409、重查中途异常）时整个事务回滚，
日志和核对结果都不改动，批次沿用原结果。

### 批次窗口定位索引（不全线扫表）

补改重查**不会**把全线批次读进内存，而是通过 `batch_windows` 定位表直接
找出检查窗口相交的批次：

* 每个批次登记时在**同一事务**写入一行定位数据
  （`window_start = 采样开始 − 5 分钟`、`window_end = 采样结束`），
  窗口再长，每批也只占固定的 1 行 + 2 个 B-Tree 索引项。
* 半开区间相交条件 `window_start < 变更结束 AND window_end > 变更开始`
  下推为 SQL：有界时段用两条**覆盖索引范围搜索 + `INTERSECT`**，
  无界时段（`end = null`）用单臂 `window_end > 变更开始`。
  两端各上万条只满足单边条件的窗口也只在紧凑索引页内被范围搜索，
  不会逐批读正文、不会逐条 Python 筛选；查询计划中不出现 `SCAN`。
* 一个批次即使被多个变更时段同时命中，也只在 ID 集合中去重后追加**一条**修订。
* 只有命中的批次会用主键 `IN` 回表读取正文。
* 端点相接（窗口结束恰为变更开始，或窗口开始恰为变更结束）不算相交，
  与核对规则的左闭右开语义一致。
* **老版本 SQLite 卷**：服务启动时自动创建定位表与索引，并把已有批次的
  检查窗口一次性回填（`INSERT ... SELECT ... WHERE NOT EXISTS`，幂等），
  老批次随即纳入定位；新登记的批次提交后立即可被重查。
* 建表、建索引、回填或补改任一步失败，已提交的批次、索引、日志与修订都保留，
  不会被清空或回退。

## 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/batches` | 录入批号、计划参数、UTC 采样起止 |
| `GET` | `/batches` | 批次列表（分页） |
| `GET` | `/batches/{batch_no}` | 当前结论 + 修订历史 |
| `GET` | `/batches/{batch_no}/check` | 能否纳入分析 + 首个不符/未知时刻 |
| `GET` | `/switches` | 全部切换记录 |
| `GET` | `/switches/{record_no}` | 单条切换记录 |
| `PUT` | `/switches/{record_no}` | 补录 / 更正切换记录并增量重查 |
| `GET` | `/health` | 健康检查 |

### 示例

```bash
# 录入批次（此时还没有切换日志 → 参数未知，不合格）
curl -X POST localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "batch_no": "LOT-2026-001",
  "plan_parameters": {"seal_temperature_c": 180, "seal_pressure_kpa": 300, "dwell_time_s": 1.5},
  "sampling_start": "2026-11-23T09:00:00Z",
  "sampling_end":   "2026-11-23T09:30:00Z"
}'

# 补录切换日志（08:50，早于窗口起点 08:55 → 恢复资格）
curl -X PUT localhost:8000/switches/SW-0001 -H 'Content-Type: application/json' -d '{
  "switched_at": "2026-11-23T08:50:00Z",
  "parameters": {"seal_temperature_c": 180, "seal_pressure_kpa": 300, "dwell_time_s": 1.5}
}'

# 按批号查结论
curl localhost:8000/batches/LOT-2026-001/check
```

## 目录结构

```
app/
  main.py         FastAPI 入口与路由
  schemas.py      请求/响应模型（含时区校验）
  service.py      事务编排、增量重查
  evaluation.py   纯函数核对逻辑（未知/不符/变更时段）
  repository.py   SQL 访问
  db.py           连接、schema、事务
  params.py       封口参数定义与规范化
  timeutil.py     UTC 时间处理
tests/
  test_evaluation.py  核对逻辑单测
  test_api.py         HTTP 端到端测试
```

## 已知限制

* 单包装线模型：切换日志是全局的，没有「产线」维度。
* 检查窗口结束点严格是采样结束时刻（不含）；恰在该时刻的切换不影响本批。
* 批次录入后不支持修改计划参数或采样时间，只能新建批次。
* 无鉴权、无限流；SQLite 单写者，适合单实例部署（compose 中即单实例）。
