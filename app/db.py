"""SQLite 连接与建表。

数据库文件路径由环境变量 DB_PATH 决定，默认 ./lab.db。
应用启动时调用 init_db() 自动创建数据库与表结构。
"""

import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "lab.db"))

# 摄入操作使用进程级锁串行化，保证"缓冲-排水-提交"在同一事务中原子完成。
ingest_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id      TEXT PRIMARY KEY,
    committed_upto INTEGER NOT NULL,          -- 已连续处理（提交或作废）的最大序号
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    payload_hash  TEXT NOT NULL,              -- 请求体规范化后的 sha256，用于检测同键不同载荷
    response_json TEXT NOT NULL,              -- 首次处理结果，重放时原样返回
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS buffer (
    device_id    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (device_id, seq)
);

CREATE TABLE IF NOT EXISTS committed (
    device_id    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    committed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (device_id, seq)
);

CREATE TABLE IF NOT EXISTS duplicates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,
    seq             INTEGER,
    idempotency_key TEXT,
    reason          TEXT NOT NULL,            -- idempotent_replay / already_committed / already_buffered / already_voided
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_duplicates_device ON duplicates (device_id, id);

-- 持久化阈值告警规则：按 (设备, 指标) 唯一，上下限至少配置一个
CREATE TABLE IF NOT EXISTS alert_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,
    metric          TEXT NOT NULL,
    upper_limit     REAL,                     -- 上限；value > upper_limit 视为越界，null 表示不限
    lower_limit     REAL,                     -- 下限；value < lower_limit 视为越界，null 表示不限
    trigger_count   INTEGER NOT NULL,         -- 连续越界多少次触发告警
    recovery_count  INTEGER NOT NULL,         -- 告警后连续不越界多少次恢复
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (device_id, metric),
    CHECK (upper_limit IS NOT NULL OR lower_limit IS NOT NULL),
    CHECK (trigger_count >= 1),
    CHECK (recovery_count >= 1),
    CHECK (lower_limit IS NULL OR upper_limit IS NULL OR lower_limit <= upper_limit)
);

-- 规则的迟滞状态机状态：与规则一对一
CREATE TABLE IF NOT EXISTS alert_rule_states (
    rule_id          INTEGER PRIMARY KEY REFERENCES alert_rules (id) ON DELETE CASCADE,
    state            TEXT NOT NULL DEFAULT 'normal',  -- normal / triggered
    breach_streak    INTEGER NOT NULL DEFAULT 0,      -- 连续越界计数
    recovery_streak  INTEGER NOT NULL DEFAULT 0,      -- 告警后连续不越界计数
    last_value       REAL,
    last_seq         INTEGER,
    last_evaluated_at TEXT
);

-- 告警 / 恢复事件，按提交顺序单调写入（id 即提交先后序号）
CREATE TABLE IF NOT EXISTS alert_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         INTEGER REFERENCES alert_rules (id) ON DELETE SET NULL,
                    -- 规则删除后置 null，事件本身保留可查
    device_id       TEXT NOT NULL,
    metric          TEXT NOT NULL,
    seq             INTEGER NOT NULL,         -- 触发 / 恢复时所处理的包序号
    event_type      TEXT NOT NULL,            -- triggered / recovered
    value           REAL NOT NULL,            -- 触发或恢复的那个测量值
    event_time      TEXT,                     -- 测量值 ts；无 ts 时取包 sent_at；再无则提交时刻
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_alert_events_device_metric
    ON alert_events (device_id, metric, id);
CREATE INDEX IF NOT EXISTS idx_alert_events_rule ON alert_events (rule_id, id);

-- 只读归档导出：任务为创建时刻已提交包的不可变快照，续传与校验都只访问这两张表
CREATE TABLE IF NOT EXISTS export_tasks (
    export_id     TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL,
    from_seq      INTEGER,                    -- null 表示从首包开始
    to_seq        INTEGER,                    -- null 表示到创建时刻已提交的末包为止
    page_size     INTEGER NOT NULL,
    package_count INTEGER NOT NULL,           -- 快照包数
    first_seq     INTEGER,                    -- 快照首序号；空导出为 null
    last_seq      INTEGER,                    -- 快照末序号；空导出为 null
    complete      INTEGER NOT NULL DEFAULT 0, -- 请求范围是否被已提交数据连续完整覆盖
    content_sha256 TEXT NOT NULL,             -- 全量 NDJSON（按 seq 升序）的 sha256
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_export_tasks_device ON export_tasks (device_id, created_at);

CREATE TABLE IF NOT EXISTS export_items (
    export_id  TEXT NOT NULL REFERENCES export_tasks (export_id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL,             -- 0 起，按 seq 升序
    seq        INTEGER NOT NULL,
    line       BLOB NOT NULL,                -- 规范化 NDJSON 行（含行尾换行符）
    PRIMARY KEY (export_id, item_index)
);

-- 设备指标线性校准配置：同一 (设备, 指标) 的有效时间段互不重叠（创建时事务内校验）。
-- 时间以固定微秒宽度的 UTC ISO 字符串存储，字典序即时间序，区间比较可直接在 SQL 完成。
CREATE TABLE IF NOT EXISTS calibrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    metric      TEXT NOT NULL,
    valid_from  TEXT NOT NULL,              -- 生效起点（含）
    valid_to    TEXT,                       -- 生效终点（不含）；null 表示无限期
    offset      REAL NOT NULL DEFAULT 0,    -- 线性偏移：校准值 = 原始值 * scale + offset
    scale       REAL NOT NULL DEFAULT 1,    -- 线性增益
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_calibrations_device_metric
    ON calibrations (device_id, metric, valid_from);

-- 缺包作废登记：每行一个作废的连续序号段 [from_seq, to_seq]。
-- 作废段一经登记即推进设备的顺序前沿（committed_upto），作废序号
-- 永远不会进入 committed，也不参与告警评估。(设备, from, to) 唯一，
-- 作为"同段重复登记"的最终防线（正常重试由幂等表短路）。
CREATE TABLE IF NOT EXISTS void_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    from_seq    INTEGER NOT NULL,              -- 作废段起点（含），登记时恰为 next_expected
    to_seq      INTEGER NOT NULL,              -- 作废段终点（含），不超过当时缓冲区最大序号
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (device_id, from_seq, to_seq),
    CHECK (from_seq >= 1 AND to_seq >= from_seq)
);

CREATE INDEX IF NOT EXISTS idx_void_records_device ON void_records (device_id, id);

-- 作废请求的幂等键：与摄入幂等表结构对应但独立存放，重放不产生重复登记。
CREATE TABLE IF NOT EXISTS void_idempotency_keys (
    key           TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL,
    from_seq      INTEGER NOT NULL,
    to_seq        INTEGER NOT NULL,
    payload_hash  TEXT NOT NULL,
    response_json TEXT NOT NULL,               -- 首次处理结果，重放时原样返回
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 双指标配比验收配置：每台设备至多一条。验收时在每个已提交包内把分子指标
-- 与分母指标的读数按各自出现顺序一对一配组（禁止跨包），组内单位不符、
-- 分母为零或缺少对应读数的组无效；有效组比例须落在 [ratio_lower,
-- ratio_upper] 闭区间内，整体有效组数还须达到 min_valid_groups。
CREATE TABLE IF NOT EXISTS ratio_acceptances (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id          TEXT NOT NULL UNIQUE,
    numerator_metric   TEXT NOT NULL,            -- 分子指标名（必须与分母指标不同）
    denominator_metric TEXT NOT NULL,            -- 分母指标名
    numerator_unit     TEXT NOT NULL,            -- 分子读数要求的单位
    denominator_unit   TEXT NOT NULL,            -- 分母读数要求的单位
    ratio_lower        REAL NOT NULL,            -- 合格比例下限（含）
    ratio_upper        REAL NOT NULL,            -- 合格比例上限（含）
    min_valid_groups   INTEGER NOT NULL,         -- 整体验收要求的最低有效组数
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (length(numerator_metric) >= 1 AND length(denominator_metric) >= 1),
    CHECK (numerator_metric <> denominator_metric),
    CHECK (length(numerator_unit) >= 1 AND length(denominator_unit) >= 1),
    CHECK (ratio_lower <= ratio_upper),
    CHECK (min_valid_groups >= 1)
);
"""


def init_db() -> None:
    """创建数据库文件与全部表（幂等）。"""
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
