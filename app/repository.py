"""SQL 访问与批次结果快照读写。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .db import dumps
from .evaluation import Switch, check_window
from .params import params_from_row
from .timeutil import parse_utc, to_index_key, to_iso

BATCH_SELECT = """
SELECT id, batch_no, plan_temperature_c, plan_pressure_kpa, plan_dwell_time_s,
       sampling_start, sampling_end, created_at, updated_at, current_revision
FROM batches
"""

SWITCH_SELECT = """
SELECT record_no, switched_at, temperature_c, pressure_kpa, dwell_time_s,
       created_at, updated_at
FROM switches
"""


def switches_from_rows(rows: list[sqlite3.Row]) -> list[Switch]:
    return [
        Switch(
            record_no=row["record_no"],
            at=parse_utc(row["switched_at"]),
            params=params_from_row(row),
        )
        for row in rows
    ]


def load_switches(conn: sqlite3.Connection) -> list[Switch]:
    rows = conn.execute(f"{SWITCH_SELECT} ORDER BY switched_at, record_no").fetchall()
    return switches_from_rows(rows)


def fetch_switch(conn: sqlite3.Connection, record_no: str) -> sqlite3.Row | None:
    return conn.execute(
        f"{SWITCH_SELECT} WHERE record_no = ?", (record_no,)
    ).fetchone()


def switch_by_time(conn: sqlite3.Connection, switched_at: str) -> sqlite3.Row | None:
    return conn.execute(
        f"{SWITCH_SELECT} WHERE switched_at = ?", (switched_at,)
    ).fetchone()


def fetch_batch(conn: sqlite3.Connection, batch_no: str) -> sqlite3.Row | None:
    return conn.execute(f"{BATCH_SELECT} WHERE batch_no = ?", (batch_no,)).fetchone()


def list_batches(
    conn: sqlite3.Connection, limit: int, offset: int
) -> list[sqlite3.Row]:
    return conn.execute(
        f"{BATCH_SELECT} ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()


def count_batches(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0])


def insert_batch_window(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    sampling_start: datetime,
    sampling_end: datetime,
) -> None:
    """登记批次的检查窗口定位行，须与批次插入处于同一事务。"""
    window_start, window_end = check_window(sampling_start, sampling_end)
    conn.execute(
        """
        INSERT INTO batch_windows (batch_id, window_start, window_end)
        VALUES (?, ?, ?)
        """,
        (batch_id, to_index_key(window_start), to_index_key(window_end)),
    )


# 单批 SQL 变量上限远低于 SQLite 的 999 限制，IN 回表分块执行。
_BATCH_ID_CHUNK = 500

#: 有界变更时段 [is, ie) 与半开窗口 [ws, we) 正长度相交
#: 等价于 ``ws < ie AND we > is``；两臂各走一个覆盖索引范围搜索，
#: INTERSECT 去重，不读批次正文，也不逐条 Python 筛选。
_INTERSECT_BOUNDED_SQL = """
SELECT batch_id FROM batch_windows WHERE window_start < ?
INTERSECT
SELECT batch_id FROM batch_windows WHERE window_end > ?
"""

#: 无界变更时段 [is, ∞)：只需 ``we > is`` 单臂索引搜索。
_INTERSECT_OPEN_ENDED_SQL = """
SELECT batch_id FROM batch_windows WHERE window_end > ?
"""


def find_intersecting_batch_ids(
    conn: sqlite3.Connection, intervals: list[dict]
) -> list[int]:
    """按变更时段直接从窗口索引找出检查窗口相交的批次 ID。

    端点相接（``ws == ie`` 或 ``we == is``）不算相交，与
    ``evaluation.window_intersects`` 的半开区间语义一致。
    一个批次即使命中多个时段也只返回一次。
    """
    ids: set[int] = set()
    for interval in intervals:
        start_key = to_index_key(interval["start"])
        if interval["end"] is None:
            rows = conn.execute(_INTERSECT_OPEN_ENDED_SQL, (start_key,)).fetchall()
        else:
            rows = conn.execute(
                _INTERSECT_BOUNDED_SQL,
                (to_index_key(interval["end"]), start_key),
            ).fetchall()
        ids.update(int(row[0]) for row in rows)
    return sorted(ids)


def fetch_batches_by_ids(
    conn: sqlite3.Connection, batch_ids: list[int]
) -> list[sqlite3.Row]:
    """只回表读取命中批次的正文，按批次登记顺序返回。"""
    batches: list[sqlite3.Row] = []
    for offset in range(0, len(batch_ids), _BATCH_ID_CHUNK):
        chunk = batch_ids[offset : offset + _BATCH_ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        batches.extend(
            conn.execute(
                f"{BATCH_SELECT} WHERE id IN ({placeholders}) ORDER BY id", chunk
            ).fetchall()
        )
    return batches


def insert_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    plan: dict[str, float],
    sampling_start: datetime,
    sampling_end: datetime,
    now: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO batches (
            batch_no, plan_temperature_c, plan_pressure_kpa, plan_dwell_time_s,
            sampling_start, sampling_end, created_at, updated_at, current_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            batch_no,
            plan["seal_temperature_c"],
            plan["seal_pressure_kpa"],
            plan["dwell_time_s"],
            to_iso(sampling_start),
            to_iso(sampling_end),
            now,
            now,
        ),
    )
    batch_id = int(cursor.lastrowid)
    # 窗口定位行与批次同事务写入：提交即可被补改重查的索引命中。
    insert_batch_window(
        conn,
        batch_id=batch_id,
        sampling_start=sampling_start,
        sampling_end=sampling_end,
    )
    return batch_id


def insert_switch(
    conn: sqlite3.Connection,
    *,
    record_no: str,
    switched_at: datetime,
    params: dict[str, float],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO switches (
            record_no, switched_at, temperature_c, pressure_kpa, dwell_time_s,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_no,
            to_iso(switched_at),
            params["seal_temperature_c"],
            params["seal_pressure_kpa"],
            params["dwell_time_s"],
            now,
            now,
        ),
    )


def update_switch(
    conn: sqlite3.Connection,
    *,
    record_no: str,
    switched_at: datetime,
    params: dict[str, float],
    now: str,
) -> None:
    conn.execute(
        """
        UPDATE switches
           SET switched_at = ?, temperature_c = ?, pressure_kpa = ?,
               dwell_time_s = ?, updated_at = ?
         WHERE record_no = ?
        """,
        (
            to_iso(switched_at),
            params["seal_temperature_c"],
            params["seal_pressure_kpa"],
            params["dwell_time_s"],
            now,
            record_no,
        ),
    )


def latest_revision(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM revisions
         WHERE batch_id = ?
         ORDER BY sequence DESC
         LIMIT 1
        """,
        (batch_id,),
    ).fetchone()


def revision_sequence(conn: sqlite3.Connection, batch_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) FROM revisions WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    return int(row[0])


def list_revisions(
    conn: sqlite3.Connection, batch_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM revisions WHERE batch_id = ? ORDER BY sequence",
        (batch_id,),
    ).fetchall()


def record_revision(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    sequence: int,
    trigger: str,
    trigger_record_no: str | None,
    previous_eligible: bool | None,
    result: dict,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO revisions (
            batch_id, sequence, trigger, trigger_record_no, status, eligible,
            reason_code, previous_eligible, first_problem_at, first_problem_kind,
            first_mismatch_at, first_unknown_at, result_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            sequence,
            trigger,
            trigger_record_no,
            result["status"],
            1 if result["eligible"] else 0,
            result["reason_code"],
            None if previous_eligible is None else (1 if previous_eligible else 0),
            result["first_problem_at"],
            result["first_problem_kind"],
            result["first_mismatch_at"],
            result["first_unknown_at"],
            dumps(result),
            now,
        ),
    )


def set_current_revision(
    conn: sqlite3.Connection, batch_id: int, sequence: int, now: str
) -> None:
    conn.execute(
        "UPDATE batches SET current_revision = ?, updated_at = ? WHERE id = ?",
        (sequence, now, batch_id),
    )
