"""SQLite 持久化层。

写操作统一使用 ``BEGIN IMMEDIATE``，保证「切换日志 + 核对结果 + 修订号」
在同一事务中生效：任一步失败都会整体回滚，批次沿用原来的核对结果。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .timeutil import STABILIZATION_SQL_MODIFIER

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS batches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no            TEXT    NOT NULL UNIQUE,
    plan_temperature_c  REAL    NOT NULL,
    plan_pressure_kpa   REAL    NOT NULL,
    plan_dwell_time_s   REAL    NOT NULL,
    sampling_start      TEXT    NOT NULL,
    sampling_end        TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    current_revision    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS switches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_no       TEXT    NOT NULL UNIQUE,
    switched_at     TEXT    NOT NULL UNIQUE,
    temperature_c   REAL    NOT NULL,
    pressure_kpa    REAL    NOT NULL,
    dwell_time_s    REAL    NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_switches_time ON switches (switched_at);

CREATE TABLE IF NOT EXISTS revisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id           INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    sequence           INTEGER NOT NULL,
    trigger            TEXT    NOT NULL,
    trigger_record_no  TEXT,
    status             TEXT    NOT NULL,
    eligible           INTEGER NOT NULL,
    reason_code        TEXT    NOT NULL,
    previous_eligible  INTEGER,
    first_problem_at   TEXT,
    first_problem_kind TEXT,
    first_mismatch_at  TEXT,
    first_unknown_at   TEXT,
    result_json        TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    UNIQUE (batch_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_revisions_batch ON revisions (batch_id, sequence);

-- 批次检查窗口定位表：每个批次固定一行，窗口再长也只占固定数量的索引项。
-- 键为 UTC 定宽字符串（与 SQLite datetime() 输出同格式），字典序即时间序。
CREATE TABLE IF NOT EXISTS batch_windows (
    batch_id     INTEGER PRIMARY KEY REFERENCES batches(id) ON DELETE CASCADE,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_windows_start ON batch_windows (window_start);
CREATE INDEX IF NOT EXISTS idx_batch_windows_end ON batch_windows (window_end);
"""

#: 老卷迁移：为已登记但没有定位行的批次回填检查窗口
#: （窗口 = [采样开始 − 5 分钟, 采样结束)，与应用层 check_window 同源）。
WINDOW_BACKFILL_SQL = f"""
INSERT INTO batch_windows (batch_id, window_start, window_end)
SELECT b.id,
       datetime(b.sampling_start, '{STABILIZATION_SQL_MODIFIER}'),
       datetime(b.sampling_end)
  FROM batches b
 WHERE NOT EXISTS (SELECT 1 FROM batch_windows w WHERE w.batch_id = b.id)
"""


class Database:
    """按请求创建连接，避免跨线程共享 sqlite3 连接。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            # 建表/建索引全部带 IF NOT EXISTS：失败也不会破坏此前已提交的
            # 批次、索引、日志与修订。
            conn.executescript(SCHEMA)
            # 老 SQLite 卷上的批次直接纳入定位索引；回填在单事务内完成，
            # 失败则整体回滚，已提交数据原样保留。
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(WINDOW_BACKFILL_SQL)
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即取得写锁；正常结束提交，异常整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads(text: str) -> Any:
    return json.loads(text)
