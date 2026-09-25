"""双设备同指标并行测量比对：交接班时核对参考仪器与待检仪器的读数。

纯只读查询：仅读取 committed 表中已提交包的原始 payload，不触碰原始包、
缓冲、重复、作废、告警与校准数据，也不改写任何已存内容。

候选测量（两侧各自独立处理）：
- 仅取 metric 相同的 measurement；有效时间优先用 measurement 自身 ts，
  否则回退到所属包的 sent_at；两者都没有的测量直接忽略；
- 有效时间归一化为 UTC（naive 时间按 UTC），按半开区间 [from, to) 过滤；
- 排序键为 (有效时间, 包 seq, 包内原始位置)：同时间先按包序号、再按
  包内位置；
- 单侧候选数硬上限 MAX_SIDE_POINTS = 200，超限直接抛错拒绝（路由转 422），
  绝不截断。

配对（保序二分匹配，每条测量最多参与一次）：
- 仅当两侧单位相同（unit 均为 null 视为相同；仅一侧缺失视为不同）且
  有效时间差绝对值不超过 max_time_delta 时允许配对；
- 优化目标依次为：① 最大化配对数；② 最小化全部配对的时间差总和；
  ③ 仍平局时，按排序后的配对序号序列 ((ref_idx, target_idx), ...)
  字典序最小决胜，保证结果唯一确定；
- 序列对齐式动态规划在 O(nm) 时间、O(nm) 字节回溯表内求得最优解，
  状态转移只允许"跳过参考点 / 跳过待检点 / 对角配对"，天然保持两侧
  时间顺序、禁止交叉配对。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional, Tuple

from .schemas import ComparisonPair, ComparisonResponse, ComparisonSidePoint

# 单侧纳入比对的最大测量条数；超限明确拒绝，不做截断
MAX_SIDE_POINTS = 200

_US_PER_SECOND = 1_000_000

# 回溯标记：(i,j) 的最优前缀分别由 (i-1,j) / (i,j-1) / (i-1,j-1) 转移而来
_SKIP_REF = 0
_SKIP_TARGET = 1
_MATCH = 2


class ComparisonError(ValueError):
    """比对参数非法（时间范围倒置、两侧同设备、候选超限等）。"""


@dataclass(frozen=True)
class _Point:
    """一侧候选测量；下标即排序后的配对序号。"""

    seq: int
    position: int
    time: datetime
    time_source: str  # "ts" / "sent_at"
    value: float
    unit: Optional[str]


class _State(NamedTuple):
    """DP 单元格状态：配对数、时间差总和（微秒）、字典序决胜编码。"""

    pairs: int
    cost_us: int
    code: int


def _as_utc(dt: datetime) -> datetime:
    """naive 时间一律视为 UTC，保证比较行为确定。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_time(raw: str) -> datetime:
    return _as_utc(datetime.fromisoformat(raw))


def load_points(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    from_: datetime,
    to: datetime,
) -> List[_Point]:
    """读取一台设备在 [from, to) 内的同指标候选测量，按时间序排序。

    仅扫描已提交包；测量时间优先取 ts、否则取包 sent_at，均无则忽略。
    """
    points: List[_Point] = []
    rows = conn.execute(
        "SELECT seq, payload_json FROM committed WHERE device_id = ? ORDER BY seq",
        (device_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        raw_sent_at = payload.get("sent_at")
        sent_at = _parse_time(raw_sent_at) if raw_sent_at else None
        for position, measurement in enumerate(payload.get("measurements", [])):
            if measurement.get("metric") != metric:
                continue
            raw_ts = measurement.get("ts")
            if raw_ts:
                time = _parse_time(raw_ts)
                time_source = "ts"
            elif sent_at is not None:
                time = sent_at
                time_source = "sent_at"
            else:
                continue  # 无 ts 且包无 sent_at：忽略
            if from_ <= time < to:
                points.append(
                    _Point(
                        seq=row["seq"],
                        position=position,
                        time=time,
                        time_source=time_source,
                        value=float(measurement["value"]),
                        unit=measurement.get("unit"),
                    )
                )
    points.sort(key=lambda p: (p.time, p.seq, p.position))
    return points


def _delta_us(a: _Point, b: _Point) -> int:
    """两个有效测量时间的绝对差（整数微秒，规避浮点误差）。"""
    delta = a.time - b.time
    if delta.days < 0:
        delta = -delta
    return (delta.days * 86400 + delta.seconds) * _US_PER_SECOND + delta.microseconds


def _better(x: _State, y: _State) -> bool:
    """配对数多者优；其次时间差总和小者；再其次配对序号序列字典序小者。"""
    return (-x.pairs, x.cost_us, x.code) < (-y.pairs, y.cost_us, y.code)


def match_points(
    ref: List[_Point],
    target: List[_Point],
    max_delta_us: int,
) -> List[Tuple[int, int]]:
    """保序二分匹配主过程，返回排序后的 (ref_idx, target_idx) 配对列表。

    每个点最多出现一次；只允许单位相同且时间差不超过上限的对角边。
    平局决胜编码把配对 (i, j) 记作 base = n*m 进制的一位数字
    （i*m + j，取值 [0, n*m)），按配对顺序拼接为整数；配对数相同时
    整数大小即配对序号序列的字典序。
    """
    n, m = len(ref), len(target)
    base = n * m or 1
    zero = _State(0, 0, 0)

    prev = [zero] * (m + 1)
    choices = bytearray(n * m)
    for i in range(1, n + 1):
        cur: List[_State] = [zero] * (m + 1)  # j=0：参考点只能全部跳过
        if m:
            choices[(i - 1) * m] = _SKIP_REF
        a = ref[i - 1]
        for j in range(1, m + 1):
            skip_ref = prev[j]
            skip_target = cur[j - 1]
            if _better(skip_target, skip_ref):
                best, choice = skip_target, _SKIP_TARGET
            else:
                best, choice = skip_ref, _SKIP_REF

            b = target[j - 1]
            if a.unit == b.unit:  # None == None 视为单位相同；仅一侧缺失则不同
                delta_us = _delta_us(a, b)
                if delta_us <= max_delta_us:
                    p = prev[j - 1]
                    matched = _State(
                        p.pairs + 1,
                        p.cost_us + delta_us,
                        p.code * base + (i - 1) * m + (j - 1),
                    )
                    if _better(matched, best):
                        best, choice = matched, _MATCH

            cur[j] = best
            choices[(i - 1) * m + (j - 1)] = choice
        prev = cur

    pairs: List[Tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        choice = choices[(i - 1) * m + (j - 1)]
        if choice == _MATCH:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif choice == _SKIP_TARGET:
            j -= 1
        else:
            i -= 1
    pairs.reverse()  # 回溯得到的是逆序，恢复为参考侧（两侧一致的）时间顺序
    return pairs


def _side_point(device_id: str, p: _Point) -> ComparisonSidePoint:
    return ComparisonSidePoint(
        device_id=device_id,
        seq=p.seq,
        position=p.position,
        time=p.time,
        time_source=p.time_source,
        value=p.value,
        unit=p.unit,
    )


def compare_devices(
    conn: sqlite3.Connection,
    reference_device: str,
    target_device: str,
    metric: str,
    from_: datetime,
    to: datetime,
    max_time_delta_seconds: float,
    allowed_value_diff: float,
) -> ComparisonResponse:
    """执行双设备同指标并行比对。调用方需保证两台设备均存在。"""
    from_ = _as_utc(from_)
    to = _as_utc(to)
    if reference_device == target_device:
        raise ComparisonError("参考设备与待检设备不能为同一台设备")
    if from_ >= to:
        raise ComparisonError("参数 from 必须早于 to")
    if max_time_delta_seconds < 0:
        raise ComparisonError("max_time_delta_seconds 不能为负数")
    if allowed_value_diff < 0:
        raise ComparisonError("allowed_value_diff 不能为负数")

    max_delta_us = round(max_time_delta_seconds * _US_PER_SECOND)

    ref_points = load_points(conn, reference_device, metric, from_, to)
    if len(ref_points) > MAX_SIDE_POINTS:
        raise ComparisonError(
            f"参考设备 {reference_device} 在时间段 [{from_.isoformat()}, "
            f"{to.isoformat()}) 内有 {len(ref_points)} 条指标 {metric} 的测量，"
            f"超过单侧上限 {MAX_SIDE_POINTS}；请缩小 UTC 时间范围后重试"
            "（服务端不截断）"
        )
    target_points = load_points(conn, target_device, metric, from_, to)
    if len(target_points) > MAX_SIDE_POINTS:
        raise ComparisonError(
            f"待检设备 {target_device} 在时间段 [{from_.isoformat()}, "
            f"{to.isoformat()}) 内有 {len(target_points)} 条指标 {metric} 的测量，"
            f"超过单侧上限 {MAX_SIDE_POINTS}；请缩小 UTC 时间范围后重试"
            "（服务端不截断）"
        )

    matched = match_points(ref_points, target_points, max_delta_us)

    pairs: List[ComparisonPair] = []
    exceeded_count = 0
    for ref_idx, target_idx in matched:
        a = ref_points[ref_idx]
        b = target_points[target_idx]
        delta_us = _delta_us(a, b)
        abs_diff = abs(a.value - b.value)
        exceeded = abs_diff > allowed_value_diff  # 等于允许偏差不算超差
        exceeded_count += int(exceeded)
        pairs.append(
            ComparisonPair(
                pair_time=a.time,  # 配对时间取参考侧有效测量时间
                reference=_side_point(reference_device, a),
                target=_side_point(target_device, b),
                time_delta_seconds=delta_us / _US_PER_SECOND,
                abs_value_diff=abs_diff,
                exceeded=exceeded,
            )
        )

    return ComparisonResponse(
        reference_device=reference_device,
        target_device=target_device,
        metric=metric,
        **{"from": from_},
        to=to,
        max_time_delta_seconds=max_time_delta_seconds,
        allowed_value_diff=allowed_value_diff,
        reference_count=len(ref_points),
        target_count=len(target_points),
        paired_count=len(pairs),
        reference_unpaired_count=len(ref_points) - len(pairs),
        target_unpaired_count=len(target_points) - len(pairs),
        exceeded_count=exceeded_count,
        pairs=pairs,
    )
