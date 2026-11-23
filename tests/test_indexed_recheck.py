"""索引化补改重查测试。

覆盖：
* 密集的「端点相接」窗口（恰在窗口起点/终点相接不算相交）；
* 跨年窗口（字典序索引键仍保持时间序）；
* 一个批次命中多个变更时段只追加一条修订；
* 老 SQLite 卷上的批次在启动迁移后直接纳入定位；
* 登记/补改失败时已提交的批次、索引、日志、修订原样保留；
* 查询计划只走覆盖索引范围搜索，不出现全表扫描；
* 上万条单边窗口下的规模表现与正文读取范围。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import repository as repo_module
from app.db import Database
from app.evaluation import check_window, window_intersects
from app.main import create_app
from app.timeutil import parse_utc, to_index_key, to_iso, utcnow

PLAN = {
    "seal_temperature_c": 180.0,
    "seal_pressure_kpa": 300.0,
    "dwell_time_s": 1.5,
}
OTHER = {
    "seal_temperature_c": 170.0,
    "seal_pressure_kpa": 280.0,
    "dwell_time_s": 1.2,
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "checks.db"))
    with TestClient(app) as test_client:
        yield test_client


def create_batch(client, batch_no, start, end, plan=None):
    return client.post(
        "/batches",
        json={
            "batch_no": batch_no,
            "plan_parameters": plan or PLAN,
            "sampling_start": start,
            "sampling_end": end,
        },
    )


def put_switch(client, record_no, at, params=None):
    return client.put(
        f"/switches/{record_no}",
        json={"switched_at": at, "parameters": params or PLAN},
    )


def with_count(database: Database, table: str) -> int:
    with database.connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _legacy_snapshot(batch_no: str, start: str, end: str, first_unknown: str) -> str:
    """构造旧卷 revisions.result_json 的最小合法快照（结构对齐 ResultPayload）。"""
    window_start = (
        parse_utc(start) - timedelta(minutes=5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps(
        {
            "batch_no": batch_no,
            "eligible": False,
            "status": "ineligible",
            "reason_code": "unknown_params",
            "reason": "检查窗口早于首条切换记录，参数未知，不能纳入分析。",
            "sampling_start": start,
            "sampling_end": end,
            "window_start": window_start,
            "window_end": end,
            "stabilization_minutes": 5,
            "plan_parameters": PLAN,
            "first_problem_at": first_unknown,
            "first_problem_kind": "unknown",
            "first_mismatch_at": None,
            "first_unknown_at": first_unknown,
            "first_problem_switch_record_no": None,
            "first_problem_switch_switched_at": None,
            "segments": [],
            "violations": [],
            "evaluated_at": "2026-11-01T00:00:00Z",
            "transition": "initial",
            "previous_eligible": None,
            "revision": 1,
            "trigger": "batch_created",
            "trigger_record_no": None,
        },
        ensure_ascii=False,
    )


# ------------------------------------------------------- 端点相接的密集窗口

def test_endpoint_touching_windows_are_not_intersections(client):
    """变更时段 [10:00, 11:00)：窗口恰在 10:00 结束或恰在 11:00 开始都不算相交。"""
    # 先放好 11:00 的开关，随后登记批次，最后插入 10:00 开关：
    # 只观察最后这次 PUT 产生的有界变更时段 [10:00, 11:00)。
    put_switch(client, "SW-LATER", "2026-11-23T11:00:00Z", OTHER)

    # 窗口 [09:30, 10:00)：window_end == interval_start → 端点相接
    create_batch(
        client, "LOT-ENDS-AT-1000",
        "2026-11-23T09:35:00Z", "2026-11-23T10:00:00Z",
    )
    # 窗口 [11:00, 11:30)（采样 11:05 起）：window_start == interval_end → 端点相接
    create_batch(
        client, "LOT-STARTS-AT-1100",
        "2026-11-23T11:05:00Z", "2026-11-23T11:30:00Z",
    )
    # 真正相交的两个窗口：一个压左端点内侧、一个压右端点内侧
    create_batch(
        client, "LOT-OVERLAP-LEFT",
        "2026-11-23T09:58:00Z", "2026-11-23T10:30:00Z",
    )
    create_batch(
        client, "LOT-OVERLAP-RIGHT",
        "2026-11-23T10:55:00Z", "2026-11-23T11:30:00Z",
    )

    response = put_switch(client, "SW-NEW", "2026-11-23T10:00:00Z", PLAN)
    assert response.status_code == 200, response.text
    assert response.json()["changed_intervals"] == [
        {
            "start": "2026-11-23T10:00:00Z",
            "end": "2026-11-23T11:00:00Z",
            "before": None,
            "after": PLAN,
        }
    ]
    names = sorted(item["batch_no"] for item in response.json()["rechecked_batches"])
    assert names == ["LOT-OVERLAP-LEFT", "LOT-OVERLAP-RIGHT"]

    # 端点相接的批次不重查，修订号停留在创建时的 1
    for untouched in ("LOT-ENDS-AT-1000", "LOT-STARTS-AT-1100"):
        detail = client.get(f"/batches/{untouched}").json()
        assert detail["revision"] == 1
        assert [r["trigger"] for r in detail["revision_history"]] == ["batch_created"]


def test_open_ended_interval_touching_window_end_is_excluded(client):
    """无界变更时段 [10:00, ∞)：恰在 10:00 结束的窗口不相交，晚一秒结束则相交。"""
    create_batch(
        client, "LOT-TOUCH",
        "2026-11-23T09:30:00Z", "2026-11-23T10:00:00Z",
    )
    create_batch(
        client, "LOT-HIT",
        "2026-11-23T09:30:01Z", "2026-11-23T10:00:01Z",
    )
    response = put_switch(client, "SW-BAD", "2026-11-23T10:00:00Z", OTHER)
    names = [item["batch_no"] for item in response.json()["rechecked_batches"]]
    assert names == ["LOT-HIT"]


def test_dense_boundary_windows_each_position_classified_once(client):
    """逐 5 分钟密集排布相接窗口，相交判定与 Python 半开语义逐批一致。"""
    starts = [
        parse_utc("2026-11-23T09:00:00Z") + timedelta(minutes=5 * i)
        for i in range(30)
    ]
    for i, start in enumerate(starts):
        create_batch(
            client, f"LOT-D{i:02d}", to_iso(start), to_iso(start + timedelta(minutes=5))
        )

    put_switch(client, "SW-BASE", "2026-11-23T08:00:00Z", PLAN)
    response = put_switch(client, "SW-BAD", "2026-11-23T10:00:00Z", OTHER)
    interval = {"start": parse_utc("2026-11-23T10:00:00Z"), "end": None}
    got = {item["batch_no"] for item in response.json()["rechecked_batches"]}

    expected = set()
    for i, start in enumerate(starts):
        ws, we = check_window(start, start + timedelta(minutes=5))
        if window_intersects(ws, we, interval):
            expected.add(f"LOT-D{i:02d}")
    assert got == expected
    # 10:00 恰好结束的窗口（LOT-D11：采样 09:55-10:00 → 窗口 09:50-10:00）不命中
    assert "LOT-D11" not in got


# --------------------------------------------------------------- 跨年窗口

def test_cross_year_windows_use_time_order_not_lexical_artifact(client):
    """2026/2027 跨年窗口：定宽字符串字典序与时间序一致，相交判定正确。"""
    # 窗口横跨年末：采样 2026-12-31 23:58 → 2027-01-01 00:30，
    # 窗口 [2026-12-31 23:53, 2027-01-01 00:30)
    create_batch(
        client, "LOT-NEWYEAR",
        "2026-12-31T23:58:00Z", "2027-01-01T00:30:00Z",
    )
    # 完全在 2027 年的窗口，与变更时段端点相接（窗口 00:30 开始 → 采样 00:35）
    create_batch(
        client, "LOT-NEXT-YEAR-TOUCH",
        "2027-01-01T00:35:00Z", "2027-01-01T01:00:00Z",
    )
    # 2026 年内、窗口在变更前结束
    create_batch(
        client, "LOT-OLD-YEAR",
        "2026-12-31T22:00:00Z", "2026-12-31T22:30:00Z",
    )

    # 构造有界变更时段 [2027-01-01 00:00, 00:30)：先有 00:30 的开关，
    # 再在其前面插入 00:00 的开关，差异被 00:30 的记录封住。
    put_switch(client, "SW-LATER", "2027-01-01T00:30:00Z", PLAN)
    response = put_switch(client, "SW-NEW", "2027-01-01T00:00:00Z", OTHER)
    assert response.status_code == 200, response.text
    assert response.json()["changed_intervals"] == [
        {
            "start": "2027-01-01T00:00:00Z",
            "end": "2027-01-01T00:30:00Z",
            "before": None,
            "after": OTHER,
        }
    ]
    names = sorted(item["batch_no"] for item in response.json()["rechecked_batches"])
    assert names == ["LOT-NEWYEAR"]

    detail = client.get("/batches/LOT-NEWYEAR").json()
    assert detail["current_result"]["window_start"] == "2026-12-31T23:53:00Z"
    assert detail["current_result"]["window_end"] == "2027-01-01T00:30:00Z"


def test_index_key_format_is_fixed_width_and_time_ordered(client):
    create_batch(client, "LOT-K", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    database = client.app.state.database
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT batch_id, window_start, window_end FROM batch_windows ORDER BY batch_id"
        ).fetchall()
    assert rows  # 登记提交后定位行立即可见
    for row in rows:
        # 定宽 'YYYY-MM-DD HH:MM:SS'，字典序可直接当时间序
        assert len(row["window_start"]) == 19
        assert len(row["window_end"]) == 19
        assert row["window_start"] < row["window_end"]


# ----------------------------------------------------- 多时段命中只产生一条修订

def test_batch_hit_by_several_intervals_gets_single_revision(client):
    # 批次窗口 [09:00, 12:00)（采样 09:05-12:00），覆盖多次参数反复
    create_batch(
        client, "LOT-LONG",
        "2026-11-23T09:05:00Z", "2026-11-23T12:00:00Z",
    )
    put_switch(client, "SW-1", "2026-11-23T08:00:00Z", PLAN)
    put_switch(client, "SW-2", "2026-11-23T10:00:00Z", OTHER)
    before_rev = client.get("/batches/LOT-LONG").json()["revision"]

    # 把 10:00 的错误切换更正到 11:00：新旧时间线差异可能切成多段，
    # 但该批次即使命中几段也只允许追加一条修订。
    response = put_switch(client, "SW-2", "2026-11-23T11:00:00Z", OTHER)
    assert response.status_code == 200
    rechecked = response.json()["rechecked_batches"]
    assert len(rechecked) == 1
    assert rechecked[0]["batch_no"] == "LOT-LONG"
    after = client.get("/batches/LOT-LONG").json()
    assert after["revision"] == before_rev + 1
    assert len(after["revision_history"]) == before_rev + 1


# ----------------------------------------------------------- 老 SQLite 卷迁移

def test_legacy_volume_batches_are_backfilled_into_window_index(tmp_path):
    """模拟旧版本 SQLite 卷：只有旧表、没有 batch_windows，启动后自动纳入定位。"""
    db_path = tmp_path / "legacy.db"
    legacy = Database(db_path)
    legacy.initialize()
    with legacy.connect() as conn:
        # 去掉新版本引入的定位表（级联删除其两个索引），模拟旧卷
        conn.execute("DROP TABLE batch_windows")
        conn.execute(
            """
            INSERT INTO batches (
                batch_no, plan_temperature_c, plan_pressure_kpa, plan_dwell_time_s,
                sampling_start, sampling_end, created_at, updated_at, current_revision
            ) VALUES
                ('OLD-1', 180, 300, 1.5, '2026-11-23T09:00:00Z',
                 '2026-11-23T09:30:00Z', 't', 't', 1),
                ('OLD-2', 180, 300, 1.5, '2027-01-01T00:00:00Z',
                 '2027-01-01T00:30:00Z', 't', 't', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO revisions (
                batch_id, sequence, trigger, trigger_record_no, status, eligible,
                reason_code, previous_eligible, first_problem_at, first_problem_kind,
                first_mismatch_at, first_unknown_at, result_json, created_at
            ) VALUES
                (1, 1, 'batch_created', NULL, 'ineligible', 0, 'unknown_params',
                 NULL, '2026-11-23T08:55:00Z', 'unknown', NULL,
                 '2026-11-23T08:55:00Z', ?, 't'),
                (2, 1, 'batch_created', NULL, 'ineligible', 0, 'unknown_params',
                 NULL, '2026-12-31T23:55:00Z', 'unknown', NULL,
                 '2026-12-31T23:55:00Z', ?, 't')
            """,
            (
                _legacy_snapshot(
                    "OLD-1", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z",
                    "2026-11-23T08:55:00Z",
                ),
                _legacy_snapshot(
                    "OLD-2", "2027-01-01T00:00:00Z", "2027-01-01T00:30:00Z",
                    "2026-12-31T23:55:00Z",
                ),
            ),
        )

    # 重新以应用方式启动（生命周期内执行迁移回填）
    app = create_app(str(db_path))
    with TestClient(app) as client:
        database = client.app.state.database
        with database.connect() as conn:
            windows = conn.execute(
                "SELECT batch_id, window_start, window_end "
                "FROM batch_windows ORDER BY batch_id"
            ).fetchall()
        assert [row["batch_id"] for row in windows] == [1, 2]
        assert windows[0]["window_start"] == "2026-11-23 08:55:00"
        assert windows[0]["window_end"] == "2026-11-23 09:30:00"
        assert windows[1]["window_start"] == "2026-12-31 23:55:00"
        assert windows[1]["window_end"] == "2027-01-01 00:30:00"

        # 第一次补录 08:50 开关：无前置记录，差异为无界时段 [08:50, ∞)，
        # 两个老批次窗口都在其后，都应被迁移后的索引直接定位、重查。
        response = put_switch(client, "SW-NEW", "2026-11-23T08:50:00Z", OTHER)
        assert response.status_code == 200, response.text
        names = sorted(
            item["batch_no"] for item in response.json()["rechecked_batches"]
        )
        assert names == ["OLD-1", "OLD-2"]
        assert client.get("/batches/OLD-1").json()["revision"] == 2
        assert client.get("/batches/OLD-2").json()["revision"] == 2

        # 在 OLD-2 窗口结束时刻（2027-01-01 00:30，半开端点）补录开关：
        # 新差异 [00:30, ∞) 与 OLD-2 窗口 [23:55, 00:30) 端点相接，不重查；
        # OLD-1 窗口更早结束，同样不重查。两个老批次修订号都停留在 2，
        # 迁移前的修订历史完整保留。
        response = put_switch(client, "SW-EDGE", "2027-01-01T00:30:00Z", PLAN)
        assert response.status_code == 200, response.text
        assert response.json()["rechecked_batches"] == []
        for batch_no in ("OLD-1", "OLD-2"):
            detail = client.get(f"/batches/{batch_no}").json()
            assert detail["revision"] == 2
            assert [r["trigger"] for r in detail["revision_history"]] == [
                "batch_created",
                "switch_created",
            ]


def test_repeated_initialize_is_idempotent_and_keeps_data(tmp_path):
    path = tmp_path / "repeat.db"
    db = Database(path)
    db.initialize()
    app = create_app(str(path))
    with TestClient(app) as client:
        create_batch(client, "LOT-1", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    # 再次初始化不重复回填、不报错、不丢数据
    db.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM batch_windows").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


# ------------------------------------------------------------- 失败时原样保留

def test_batch_registration_failure_rolls_back_window_row(client):
    create_batch(client, "LOT-1", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    database = client.app.state.database
    before_windows = with_count(database, "batch_windows")

    # 批号冲突 → 409，批次与窗口行都不得留下
    response = create_batch(
        client, "LOT-1", "2026-11-23T10:00:00Z", "2026-11-23T10:30:00Z"
    )
    assert response.status_code == 409
    assert with_count(database, "batch_windows") == before_windows
    assert with_count(database, "batches") == 1


def test_window_insert_failure_rolls_back_batch_and_keeps_committed_data(
    client, monkeypatch
):
    """窗口行写入失败时本批登记整体回滚；此前已提交的数据原样保留。"""
    create_batch(client, "LOT-KEPT", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    put_switch(client, "SW-KEPT", "2026-11-23T08:00:00Z", PLAN)

    def boom(conn, **kwargs):
        raise sqlite3.OperationalError("模拟窗口索引写入失败")

    monkeypatch.setattr(repo_module, "insert_batch_window", boom)
    with pytest.raises(sqlite3.OperationalError):
        create_batch(
            client, "LOT-FAIL", "2026-11-23T11:00:00Z", "2026-11-23T11:30:00Z"
        )
    monkeypatch.undo()

    database = client.app.state.database
    assert with_count(database, "batches") == 1
    assert with_count(database, "batch_windows") == 1
    assert with_count(database, "switches") == 1
    # 已提交批次的结论与修订历史完好
    detail = client.get("/batches/LOT-KEPT").json()
    assert detail["revision"] >= 1
    assert client.get("/switches/SW-KEPT").status_code == 200


def test_recheck_failure_keeps_committed_batches_indexes_logs_revisions(
    client, monkeypatch
):
    from app import service as service_module

    create_batch(client, "LOT-A", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    create_batch(client, "LOT-B", "2026-11-23T13:00:00Z", "2026-11-23T13:30:00Z")
    put_switch(client, "SW-1", "2026-11-23T08:00:00Z", PLAN)
    database = client.app.state.database
    counts_before = {
        table: with_count(database, table)
        for table in ("batches", "batch_windows", "switches", "revisions")
    }
    result_before = client.get("/batches/LOT-A/check").json()

    def boom(*args, **kwargs):
        raise RuntimeError("模拟重查写入失败")

    monkeypatch.setattr(service_module, "_persist_evaluation", boom)
    with pytest.raises(RuntimeError):
        put_switch(client, "SW-2", "2026-11-23T10:00:00Z", OTHER)
    monkeypatch.undo()

    for table, count in counts_before.items():
        assert with_count(database, table) == count, table
    assert client.get("/batches/LOT-A/check").json() == result_before
    assert client.get("/switches").json()["total"] == 1


# ------------------------------------------------------------- 查询计划与规模

def test_query_plan_uses_covering_index_ranges_without_scan(client):
    database = client.app.state.database
    with database.connect() as conn:
        bounded = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT batch_id FROM batch_windows WHERE window_start < ? "
            "INTERSECT "
            "SELECT batch_id FROM batch_windows WHERE window_end > ?",
            ("2026-11-23 10:00:00", "2026-11-23 10:00:00"),
        ).fetchall()
        open_ended = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT batch_id FROM batch_windows WHERE window_end > ?",
            ("2026-11-23 10:00:00",),
        ).fetchall()
    plan_text = " ".join(row[-1] for row in bounded)
    assert "SCAN" not in plan_text
    assert "COVERING INDEX idx_batch_windows_start" in plan_text
    assert "COVERING INDEX idx_batch_windows_end" in plan_text
    open_text = " ".join(row[-1] for row in open_ended)
    assert "SCAN" not in open_text
    assert "COVERING INDEX idx_batch_windows_end" in open_text


def _bulk_register(database: Database, specs: list[tuple[str, str, str]]) -> None:
    """在单事务内批量登记批次（含窗口定位行），用于规模测试铺底数据。"""
    now = to_iso(utcnow())
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for batch_no, start, end in specs:
                repo_module.insert_batch(
                    conn,
                    batch_no=batch_no,
                    plan=dict(PLAN),
                    sampling_start=parse_utc(start),
                    sampling_end=parse_utc(end),
                    now=now,
                )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


def test_scale_thousands_of_single_sided_windows_do_not_get_scanned(client):
    """两端各上万条只满足单边条件的窗口：命中集合仍由索引范围搜索精确给出。"""
    left_count = 12000
    right_count = 12000
    boundary_start = parse_utc("2026-03-15T10:00:00Z")
    boundary_end = parse_utc("2026-03-15T11:00:00Z")

    specs: list[tuple[str, str, str]] = []
    # 左侧：窗口结束时刻 <= 10:00（i=0 恰好端点相接），只满足 ws < ie 单边
    for i in range(left_count):
        end = boundary_start - timedelta(minutes=i)
        start = end - timedelta(minutes=30)
        specs.append((f"LOT-L{i:05d}", to_iso(start), to_iso(end)))
    # 右侧：窗口开始时刻 >= 11:00（i=0 恰好端点相接，采样 11:05 起），
    # 只满足 we > is 单边。
    right_origin = boundary_end + timedelta(minutes=5)
    for i in range(right_count):
        start = right_origin + timedelta(minutes=i)
        end = start + timedelta(minutes=30)
        specs.append((f"LOT-R{i:05d}", to_iso(start), to_iso(end)))

    import time

    database = client.app.state.database
    # 先放好 11:00 的开关（此时还没有批次，不产生重查）。
    put_switch(client, "SW-LATER", "2026-03-15T11:00:00Z", PLAN)

    t0 = time.perf_counter()
    _bulk_register(database, specs)
    register_seconds = time.perf_counter() - t0

    # 唯一真正相交的窗口，走 API 登记（提交后立即可被索引定位）
    response = create_batch(
        client, "LOT-HIT", "2026-03-15T09:57:00Z", "2026-03-15T10:30:00Z"
    )
    assert response.status_code == 201, response.text

    # 在 11:00 开关前面插入 10:00 开关：单次 PUT 的新旧时间线差异
    # 被 11:00 记录封住，形成有界时段 [10:00, 11:00)。
    t0 = time.perf_counter()
    response = put_switch(client, "SW-NEW", "2026-03-15T10:00:00Z", OTHER)
    recheck_seconds = time.perf_counter() - t0
    assert response.status_code == 200, response.text
    # 有界时段 [10:00, 11:00)：两侧各 12000 条单边窗口全部被索引排除
    assert [item["batch_no"] for item in response.json()["rechecked_batches"]] == [
        "LOT-HIT"
    ]
    assert response.json()["changed_intervals"] == [
        {
            "start": "2026-03-15T10:00:00Z",
            "end": "2026-03-15T11:00:00Z",
            "before": None,
            "after": OTHER,
        }
    ]

    with database.connect() as conn:
        total_windows = conn.execute(
            "SELECT COUNT(*) FROM batch_windows"
        ).fetchone()[0]
        # 每批固定 1 个定位行（主键 + 两个 B-Tree 各一项），窗口再长也不膨胀
        assert total_windows == left_count + right_count + 1
        index_names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'batch_windows' ORDER BY name"
            ).fetchall()
        ]
        assert "idx_batch_windows_start" in index_names
        assert "idx_batch_windows_end" in index_names
        # INTEGER PRIMARY KEY 与表共用 B-Tree（rowid 别名），不产生独立自动索引；
        # 定位表上恰好是这两个显式 B-Tree 索引。
        assert len(index_names) == 2
        # 两个显式索引各只索引单列，与窗口长度/内容无关
        for name in ("idx_batch_windows_start", "idx_batch_windows_end"):
            columns = conn.execute(f"PRAGMA index_info({name})").fetchall()
            assert len(columns) == 1

    # 宽松余量：核心保证是不逐批扫正文，而非硬性毫秒数
    assert recheck_seconds < 5.0
    assert register_seconds < 30.0


def test_only_hit_batch_rows_are_read_back(client, monkeypatch):
    """回表读取只针对命中批次：201 个批次中只 IN 回表 1 个。"""
    specs = []
    for i in range(200):
        day = 10 + i // 24
        hour = i % 24
        start = f"2026-01-{day:02d}T{hour:02d}:00:00Z"
        end = f"2026-01-{day:02d}T{hour:02d}:30:00Z"
        specs.append((f"LOT-{i:03d}", start, end))
    _bulk_register(client.app.state.database, specs)
    create_batch(
        client, "LOT-HIT", "2026-05-01T09:57:00Z", "2026-05-01T10:30:00Z"
    )
    database = client.app.state.database
    with database.connect() as conn:
        hit_id = int(
            conn.execute(
                "SELECT id FROM batches WHERE batch_no = 'LOT-HIT'"
            ).fetchone()[0]
        )

    captured: dict[str, list[int]] = {}
    original = repo_module.fetch_batches_by_ids

    def spy(conn, batch_ids):
        captured["ids"] = list(batch_ids)
        return original(conn, batch_ids)

    monkeypatch.setattr(repo_module, "fetch_batches_by_ids", spy)
    response = put_switch(client, "SW-BAD", "2026-05-01T10:00:00Z", OTHER)
    assert response.status_code == 200
    assert captured["ids"] == [hit_id]
    assert [item["batch_no"] for item in response.json()["rechecked_batches"]] == [
        "LOT-HIT"
    ]


def test_index_key_matches_check_window_semantics(client):
    create_batch(client, "LOT-K", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    database = client.app.state.database
    with database.connect() as conn:
        row = conn.execute(
            "SELECT window_start, window_end FROM batch_windows w "
            "JOIN batches b ON b.id = w.batch_id WHERE b.batch_no = 'LOT-K'"
        ).fetchone()
    ws, we = check_window(
        parse_utc("2026-11-23T09:00:00Z"), parse_utc("2026-11-23T09:30:00Z")
    )
    assert row["window_start"] == to_index_key(ws)
    assert row["window_end"] == to_index_key(we)
