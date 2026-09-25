"""按事件时间（sent_at）的测量聚合：半开窗口分桶 + 水位迟到判定。

语义约定：
- 仅统计已提交（committed）的包；缓冲区与重复包不参与聚合；
- 样本的事件时间取所属包的 sent_at（无 sent_at 的包无法分窗，跳过）；
- 窗口以 from 为锚点的半开区间
  [from + k*window_seconds, from + (k+1)*window_seconds)，覆盖 [from, to)；
- 水位 = 设备已提交包的最大 sent_at - allowed_lateness（设备级，
  与查询的 metric / 时间范围无关）；
- 迟到判定发生在包提交时刻：按提交顺序重放，若某包的 sent_at 早于
  此前已见过的最大 sent_at 所构成的水位，则其样本为迟到样本——
  计入 late_count，不得改写已关闭窗口的统计；
- 窗口按 start 升序稳定返回，空窗口 count=0、统计值为 null；
- calibrated=False（默认）时统计原始值；calibrated=True 时，对每个
  已提交包按其 sent_at 解析该 (设备, 指标) 当时唯一生效的校准区间，
  以 校准值 = 原始值 * scale + offset 参与统计；该时刻无生效配置则
  沿用原始值。校准只在本次查询中计算，不改写已存测量，也不影响导出。
"""

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional, Tuple

from .calibrations import (
    apply_calibration,
    load_calibration_index,
    normalize_instant,
)
from .schemas import RollupResponse, RollupWindow

# 单次查询允许的最大窗口数，防止时间跨度过大导致响应爆炸
MAX_WINDOWS = 10000

_US_PER_SECOND = 1_000_000


class RollupError(ValueError):
    """聚合参数非法（from 不早于 to、窗口粒度过小、窗口数超限等）。"""


def _as_utc(dt: datetime) -> datetime:
    """naive 时间一律视为 UTC，保证比较行为确定。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iter_packages(
    conn: sqlite3.Connection, device_id: str, metric: str
) -> Iterator[Tuple[Optional[datetime], List[float]]]:
    """按提交顺序（rowid）产出每个已提交包的 (sent_at, 该指标测量值列表)。"""
    rows = conn.execute(
        "SELECT payload_json FROM committed WHERE device_id = ? ORDER BY rowid",
        (device_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        raw = payload.get("sent_at")
        sent_at = _as_utc(datetime.fromisoformat(raw)) if raw else None
        values = [
            float(m["value"])
            for m in payload.get("measurements", [])
            if m.get("metric") == metric
        ]
        yield sent_at, values


def compute_rollup(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    from_: datetime,
    to: datetime,
    window_seconds: float,
    allowed_lateness: float,
    calibrated: bool = False,
) -> RollupResponse:
    """对单台设备的已提交测量按 sent_at 分窗聚合。调用方需保证设备存在。

    calibrated=True 时按每个已提交包的 sent_at 解析生效校准区间并计算
    校准值（无生效配置沿用原始值）；迟到样本只计入 late_count，校准与否
    都不改写窗口统计。
    """
    from_ = _as_utc(from_)
    to = _as_utc(to)
    if from_ >= to:
        raise RollupError("参数 from 必须早于 to")
    if allowed_lateness < 0:
        raise RollupError("allowed_lateness 不能为负数")

    # 校准模式下一次性载入该 (设备, 指标) 的全部区间，按包 sent_at 二分解析
    calibration_index = (
        load_calibration_index(conn, device_id, metric) if calibrated else None
    )

    # 用整数微秒计算窗口边界与归属，避免浮点误差把边界样本放错窗口
    window_us = round(window_seconds * _US_PER_SECOND)
    if window_us < 1:
        raise RollupError("window_seconds 必须为正数")
    range_us = round((to - from_).total_seconds() * _US_PER_SECOND)
    n_windows = math.ceil(range_us / window_us)
    if n_windows > MAX_WINDOWS:
        raise RollupError(
            f"窗口数 {n_windows} 超过上限 {MAX_WINDOWS}，"
            "请增大 window_seconds 或缩小时间范围"
        )

    lateness = timedelta(seconds=allowed_lateness)
    buckets: List[List[float]] = [[] for _ in range(n_windows)]
    late_count = 0
    max_sent_at: Optional[datetime] = None
    for sent_at, values in _iter_packages(conn, device_id, metric):
        if sent_at is None:
            continue  # 无事件时间的包无法分窗，跳过
        # 提交时刻的水位：此前已见过的最大 sent_at - allowed_lateness
        is_late = max_sent_at is not None and sent_at < max_sent_at - lateness
        if from_ <= sent_at < to:
            if is_late:
                # 水位之前到达：窗口已关闭，只计数不改写
                late_count += len(values)
            else:
                if calibration_index is not None:
                    interval = calibration_index.resolve(
                        normalize_instant(sent_at)
                    )
                    if interval is not None:
                        values = [
                            apply_calibration(v, interval.scale, interval.offset)
                            for v in values
                        ]
                offset_us = round((sent_at - from_).total_seconds() * _US_PER_SECOND)
                buckets[offset_us // window_us].extend(values)
        if max_sent_at is None or sent_at > max_sent_at:
            max_sent_at = sent_at

    watermark = max_sent_at - lateness if max_sent_at is not None else None

    windows: List[RollupWindow] = []
    for i, values in enumerate(buckets):
        start = from_ + timedelta(microseconds=i * window_us)
        end = from_ + timedelta(microseconds=(i + 1) * window_us)
        if values:
            windows.append(
                RollupWindow(
                    start=start,
                    end=end,
                    count=len(values),
                    min=min(values),
                    max=max(values),
                    mean=sum(values) / len(values),
                )
            )
        else:
            windows.append(RollupWindow(start=start, end=end, count=0))

    return RollupResponse(
        device_id=device_id,
        metric=metric,
        **{"from": from_},
        to=to,
        window_seconds=window_seconds,
        allowed_lateness=allowed_lateness,
        calibrated=calibrated,
        watermark=watermark,
        late_count=late_count,
        windows=windows,
    )
