"""超阈暴露量积分：对已提交读数曲线超过阈值的部分在 [from, to) 上积分。

纯只读查询：仅读取 committed 表中已提交包的原始 payload，不触碰缓冲包、
作废序号、重复、告警与校准数据，也不改写任何已存内容。

候选读数：
- 仅取 metric 相同且 unit 与请求 unit 逐字相同的 measurement（读数缺 unit
  视为不符，计入 excluded_unit_mismatch_count）；
- 有效时间优先用 measurement 自身 ts，否则回退到所属包的 sent_at；
  两者都没有的测量直接丢弃（计入 excluded_no_time_count）；
- 单位过滤先于时间过滤（与时间范围无关，全部计入单位不符计数）；
- 有效时间归一化为 UTC（naive 时间按 UTC），排序键为
  (有效时间, 包 seq, 包内原始位置)；同一有效时刻只保留排序后的最后一条；
- 不按 [from, to) 预过滤：范围外的相邻读数仍参与边界插值。

插值与积分：
- 相邻候选读数之间按时间线性插值；相邻间隔严格大于 max_gap_seconds 时
  该整段不参与（断裂），但断裂两侧的读数仍各自与更远的相邻读数配段；
- 每条有效线段先与 [from, to) 求交裁剪，再按阈值交点切分为若干子段，
  对严格高于阈值的子段积分：暴露秒数累加子段时长，暴露面积按梯形累加
  ∫ max(v(t)-threshold, 0) dt；恰在阈值上的部分贡献为零。

时间比较一律使用整数微秒运算，避免浮点秒比较误差；线段内的裁剪、
阈值交点切分与梯形积分使用相对首端点的微秒坐标，避免 UTC 纪元绝对
微秒（远期时间量级达 1e16）转浮点后丢失微秒分辨率。不足两条读数时
无法构成任何线段，暴露秒数与面积均为 0。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .schemas import ExposureResponse

_US_PER_SECOND = 1_000_000


class ExposureError(ValueError):
    """积分参数非法（时间范围倒置、间隔为负等）。"""


@dataclass(frozen=True)
class _Point:
    """一条去重后的候选读数。"""

    seq: int
    position: int
    time: datetime
    time_us: int  # 自 UTC epoch 的微秒数
    value: float
    unit: Optional[str]


def _as_utc(dt: datetime) -> datetime:
    """naive 时间一律视为 UTC，保证比较行为确定。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_time(raw: str) -> datetime:
    return _as_utc(datetime.fromisoformat(raw))


def _to_us(dt: datetime) -> int:
    delta = dt - datetime.fromtimestamp(0, tz=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * _US_PER_SECOND + delta.microseconds


def load_readings(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    unit: str,
):
    """读取设备全部同指标、单位相符的已提交读数，按有效时间排序去重。

    测量时间优先取 ts、否则取包 sent_at，均无则丢弃。范围外读数同样
    返回（边界插值需要）。返回 (去重后点列表, 无时间丢弃数, 单位不符丢弃数)。
    """
    points: List[_Point] = []
    no_time = 0
    unit_mismatch = 0
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
            if measurement.get("unit") != unit:
                unit_mismatch += 1  # 单位不符（含缺单位）：丢弃
                continue
            raw_ts = measurement.get("ts")
            if raw_ts:
                time = _parse_time(raw_ts)
            elif sent_at is not None:
                time = sent_at
            else:
                no_time += 1  # 无 ts 且包无 sent_at：丢弃
                continue
            points.append(
                _Point(
                    seq=row["seq"],
                    position=position,
                    time=time,
                    time_us=_to_us(time),
                    value=float(measurement["value"]),
                    unit=measurement.get("unit"),
                )
            )
    # 排序后同一时刻只保留最后一条（同时间决胜：seq 大者、再包内位置大者）
    points.sort(key=lambda p: (p.time_us, p.seq, p.position))
    deduped: List[_Point] = []
    for point in points:
        if deduped and point.time_us == deduped[-1].time_us:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped, no_time, unit_mismatch


def _integrate_segment(
    t0_us: int,
    v0: float,
    t1_us: int,
    v1: float,
    lo_us: int,
    hi_us: int,
    threshold: float,
):
    """对线段 v(t)（端点 (t0,v0)-(t1,v1)）在裁剪区间 [lo, hi) 上积分。

    返回 (暴露微秒数, 暴露面积)；面积单位为 读数单位·微秒，由调用方换算。
    线段须已与 [lo, hi) 求交，区间内严格线性。恰在阈值上的部分不计。

    线段内部一律使用相对首端点的微秒坐标 x = t - t0（0..span），裁剪端点
    也先换算成相对坐标再转浮点：这样切分与求交只涉及线段时间跨度量级的数，
    不携带 UTC 纪元绝对微秒（2300 年附近约 1e16，相邻 1 微秒转 float64
    会被 ULP 吞没），保证微秒分辨率不随绝对时刻退化。
    """
    lo = max(lo_us, t0_us)
    hi = min(hi_us, t1_us)
    if hi <= lo:
        return 0, 0.0

    span_us = t1_us - t0_us
    slope = (v1 - v0) / span_us

    # 裁剪端点先换算为相对首端点的整数微秒，再转浮点；阈值交点同样在
    # 相对坐标上求出，全程不出现绝对纪元微秒的浮点表示
    cuts = [float(lo - t0_us), float(hi - t0_us)]
    if (v0 - threshold) * (v1 - threshold) < 0:
        cross_x = (threshold - v0) / slope
        if (lo - t0_us) < cross_x < (hi - t0_us):
            cuts.append(cross_x)
    cuts.sort()

    over_us = 0.0
    area = 0.0
    for a, b in zip(cuts, cuts[1:]):
        mid = (a + b) / 2.0
        v_mid = v0 + slope * mid
        if v_mid <= threshold:
            continue  # 子段在阈值下方（恰在阈值上贡献亦为零）
        va = v0 + slope * a - threshold
        vb = v0 + slope * b - threshold
        # 数值误差保护：阈值交点处可能出现 -1e-15 量级的负值
        if va < 0 and abs(va) < 1e-9:
            va = 0.0
        if vb < 0 and abs(vb) < 1e-9:
            vb = 0.0
        over_us += b - a
        area += (va + vb) * (b - a) / 2.0
    return over_us, area


def compute_exposure(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    unit: str,
    from_: datetime,
    to: datetime,
    threshold: float,
    max_gap_seconds: float,
) -> ExposureResponse:
    """执行超阈暴露量积分。调用方需保证设备存在。"""
    from_ = _as_utc(from_)
    to = _as_utc(to)
    if from_ >= to:
        raise ExposureError("参数 from 必须早于 to")
    if max_gap_seconds < 0:
        raise ExposureError("max_gap_seconds 不能为负数")

    points, no_time, unit_mismatch = load_readings(conn, device_id, metric, unit)

    lo_us = _to_us(from_)
    hi_us = _to_us(to)
    max_gap_us = round(max_gap_seconds * _US_PER_SECOND)

    over_us = 0.0
    area_us = 0.0
    segment_count = 0
    for prev, cur in zip(points, points[1:]):
        if cur.time_us - prev.time_us > max_gap_us:
            continue  # 间隔超限：该整段插值不计
        if cur.time_us <= lo_us or prev.time_us >= hi_us:
            continue  # 与 [from, to) 无交集
        seg_us, seg_area = _integrate_segment(
            prev.time_us,
            prev.value,
            cur.time_us,
            cur.value,
            lo_us,
            hi_us,
            threshold,
        )
        segment_count += 1
        over_us += seg_us
        area_us += seg_area

    return ExposureResponse(
        device_id=device_id,
        metric=metric,
        unit=unit,
        **{"from": from_},
        to=to,
        threshold=threshold,
        max_gap_seconds=max_gap_seconds,
        sample_count=len(points),
        segment_count=segment_count,
        excluded_no_time_count=no_time,
        excluded_unit_mismatch_count=unit_mismatch,
        exposure_seconds=over_us / _US_PER_SECOND,
        exposure_area=area_us / _US_PER_SECOND,
    )
