"""设备指标线性校准配置：按有效时间段管理，校准值 = 原始值 * scale + offset。

- 每条配置对 (设备, 指标) 声明一个半开有效区间 [valid_from, valid_to)，
  valid_to 为 null 表示无限期；
- 同一 (设备, 指标) 的有效区间不得重叠：创建时在同一事务内校验并写入
  （调用方持有 ingest_lock 串行化写入），保证任意时刻至多一条配置生效；
- 校准只在 resolve 查询时计算，已存储的原始测量不被改写。
"""

import bisect
import sqlite3
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional

# valid_to 为 null（无限期）时参与区间比较的远端哨兵时间；
# 与存储格式同为固定微秒宽度的 UTC ISO 字符串，字典序即时间序
_FAR_FUTURE = "9999-12-31T23:59:59.999999Z"


class CalibrationInterval(NamedTuple):
    """供批量时刻解析使用的一条校准区间（时间已归一化为固定宽度字符串）。"""

    valid_from: str
    valid_to: Optional[str]
    scale: float
    offset: float


class CalibrationIndex:
    """某 (设备, 指标) 校准区间的只读快照，支持按时刻批量解析。

    区间互不重叠，按 valid_from 排序后，at 时刻生效的配置至多一条，
    用二分定位即可，避免窗口聚合重放提交时逐包查询数据库。
    """

    def __init__(self, intervals: List[CalibrationInterval]):
        self._intervals = sorted(intervals, key=lambda i: i.valid_from)
        self._starts = [i.valid_from for i in self._intervals]

    def __len__(self) -> int:
        return len(self._intervals)

    def resolve(self, instant: str) -> Optional[CalibrationInterval]:
        """返回 instant（固定宽度 UTC 字符串）时刻生效的区间；无则 None。"""
        idx = bisect.bisect_right(self._starts, instant) - 1
        if idx < 0:
            return None
        interval = self._intervals[idx]
        if interval.valid_to is not None and instant >= interval.valid_to:
            return None
        return interval


class CalibrationError(ValueError):
    """校准配置参数非法。"""


class CalibrationConflict(Exception):
    """同一 (设备, 指标) 的有效时间段与已有配置重叠。"""


def normalize_instant(dt: datetime) -> str:
    """归一化为 UTC、固定微秒宽度、Z 结尾的 ISO 字符串；naive 一律按 UTC。

    固定宽度保证字符串字典序与时间序一致，区间比较可直接在 SQL 中完成。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="microseconds")
        + "Z"
    )


def create_calibration(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    valid_from: datetime,
    valid_to: Optional[datetime],
    offset: float,
    scale: float,
) -> int:
    """创建校准配置并返回新配置 id；区间重叠抛 CalibrationConflict。

    重叠校验与插入在同一事务中完成（调用方持有 ingest_lock）。
    """
    start = normalize_instant(valid_from)
    end = normalize_instant(valid_to) if valid_to is not None else None
    if end is not None and start >= end:
        raise CalibrationError("valid_from 必须早于 valid_to")
    # 半开区间重叠判定：existing.valid_from < new.end 且 existing.valid_to > new.start
    overlap = conn.execute(
        """
        SELECT id, valid_from, valid_to FROM calibrations
         WHERE device_id = ? AND metric = ?
           AND valid_from < ?
           AND COALESCE(valid_to, ?) > ?
         ORDER BY id LIMIT 1
        """,
        (device_id, metric, end or _FAR_FUTURE, _FAR_FUTURE, start),
    ).fetchone()
    if overlap is not None:
        raise CalibrationConflict(
            f"设备 {device_id} 指标 {metric} 的有效时间段与已有配置重叠: "
            f"id={overlap['id']} [{overlap['valid_from']}, "
            f"{overlap['valid_to'] or 'null'})"
        )
    cur = conn.execute(
        """
        INSERT INTO calibrations
            (device_id, metric, valid_from, valid_to, offset, scale)
        VALUES (?,?,?,?,?,?)
        """,
        (device_id, metric, start, end, offset, scale),
    )
    return cur.lastrowid


def resolve_calibration(
    conn: sqlite3.Connection, device_id: str, metric: str, at: datetime
) -> Optional[sqlite3.Row]:
    """返回 at 时刻生效的校准配置行；无生效配置返回 None。

    区间互不重叠，故至多命中一条；边界为 [valid_from, valid_to)。
    """
    instant = normalize_instant(at)
    return conn.execute(
        """
        SELECT * FROM calibrations
         WHERE device_id = ? AND metric = ?
           AND valid_from <= ?
           AND COALESCE(valid_to, ?) > ?
         ORDER BY id LIMIT 1
        """,
        (device_id, metric, instant, _FAR_FUTURE, instant),
    ).fetchone()


def load_calibration_index(
    conn: sqlite3.Connection, device_id: str, metric: str
) -> CalibrationIndex:
    """一次性载入某 (设备, 指标) 的全部校准区间，供窗口聚合按 sent_at 批量解析。

    区间在创建时已保证互不重叠；返回的索引只在当前查询内使用。
    """
    rows = conn.execute(
        """
        SELECT valid_from, valid_to, scale, offset FROM calibrations
         WHERE device_id = ? AND metric = ?
         ORDER BY valid_from
        """,
        (device_id, metric),
    ).fetchall()
    return CalibrationIndex(
        [
            CalibrationInterval(
                valid_from=r["valid_from"],
                valid_to=r["valid_to"],
                scale=r["scale"],
                offset=r["offset"],
            )
            for r in rows
        ]
    )


def apply_calibration(value: float, scale: float, offset: float) -> float:
    """线性校准：校准值 = 原始值 * scale + offset。"""
    return value * scale + offset
