"""SQLite 持久层：schema、连接与事务辅助。

一次核查（一轮）的所有数据都在同一个数据库文件里。
写入采用 WAL + 立即事务（BEGIN IMMEDIATE），配合重试处理 SQLITE_BUSY，
保证“通告和结论一起生效，失败后仍查原结果”。
"""
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot_at  INTEGER NOT NULL,   -- 快照 UTC 纪元秒
    check_at     INTEGER NOT NULL,   -- 核查 UTC 纪元秒
    created_at   INTEGER NOT NULL,
    CHECK (check_at > snapshot_at)
);

CREATE TABLE IF NOT EXISTS terminals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    snapshot_expiry INTEGER,         -- 快照时单个旧地址到期时刻；无地址为 NULL
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pio_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id    INTEGER NOT NULL REFERENCES terminals(id),
    seq            INTEGER NOT NULL,
    router         TEXT NOT NULL,
    received_at    INTEGER NOT NULL,       -- UTC 纪元秒
    valid_lifetime INTEGER NOT NULL,       -- 有限寿命 0 .. 2^32-2
    content_hash   TEXT NOT NULL,          -- 除 terminal/seq 外内容指纹，判重报/冲突
    created_at     INTEGER NOT NULL,
    UNIQUE (terminal_id, seq)
);

CREATE TABLE IF NOT EXISTS conclusions (
    terminal_id    INTEGER PRIMARY KEY REFERENCES terminals(id),
    expiry_at      INTEGER,                -- 核查时重算后的到期时刻，无地址为 NULL
    alive_at_check INTEGER NOT NULL,       -- 0/1：核查时刻是否仍持有旧地址
    source         TEXT NOT NULL,          -- 'snapshot' | 'pio'
    last_pio_id    INTEGER REFERENCES pio_records(id),
    recomputed_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pio_decisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    pio_id             INTEGER NOT NULL REFERENCES pio_records(id),
    rule               TEXT NOT NULL,      -- d / e1 / e2 / e3
    changed            INTEGER NOT NULL,
    expiry_before      INTEGER,
    expiry_after       INTEGER,
    remaining_before   INTEGER,
    detail             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pio_terminal ON pio_records(terminal_id, received_at, seq);
"""


def get_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: str) -> None:
    conn = get_db(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection, retries: int = 5, delay: float = 0.2):
    """BEGIN IMMEDIATE 事务，SQLITE_BUSY 时退避重试。

    事务内任何异常都会 ROLLBACK，已提交的旧结论保持可读。
    """
    attempt = 0
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < retries:
                attempt += 1
                time.sleep(delay * attempt)
                continue
            raise
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
