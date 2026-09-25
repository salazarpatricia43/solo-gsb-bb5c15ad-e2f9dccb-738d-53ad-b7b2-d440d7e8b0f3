"""设备时钟漂移诊断：用已提交包的 (sent_at, ts) 偏移序列估计时钟漂移。

纯只读查询：仅读取 committed 表中已提交包的原始 payload，不触碰缓冲包、
作废序号、重复、告警与校准数据，也不改写任何已存内容。

样本提取（逐包处理，只按包发送时间 sent_at 圈定范围）：
- 仅扫描已提交包；包缺少 sent_at 时整包跳过
  （计入 excluded_no_sent_at_count）；
- sent_at 归一化为 UTC（naive 时间按 UTC），仅保留落在半开区间
  [from, to) 内的包；范围内包数硬上限 MAX_PACKAGES = 200，
  超限直接抛错拒绝（路由转 422），绝不截断；
- 每个范围内的包只取该 metric **第一条带 ts 的 measurement**（按包内
  原始顺序）；不存在时整包跳过（计入 excluded_no_ts_count）；
- 样本偏移 offset = ts - sent_at（秒，UTC 统一后求差，整数微秒运算
  规避浮点误差）；同一发送时刻（归一化后时刻相同）只保留**序号最大**
  的一包。

漂移估计（Theil–Sen 式成对斜率中位数，对异常点稳健）：
- 对不同发送时刻的全部样本两两配对，斜率 = (offset_j - offset_i)
  / (sent_at_j - sent_at_i)（秒/秒），取全部斜率的中位数作为每小时
  漂移 drift（秒/小时）；
- 再以该斜率把每个样本的偏移折算到末次发送时刻：
  residual_k = offset_k - slope · (sent_at_k - last_sent_at)，
  末次偏移 last_offset = 这些残差的中位数（斜率取中位数时的稳健
  截距估计）；
- 不同发送时刻的有效样本不足三包（< 3）时证据不足，不输出估计值。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .schemas import ClockDriftResponse, ClockDriftSample

# 单次诊断最多分析的已提交包数（按 sent_at 落入范围的包计）；超限拒绝
MAX_PACKAGES = 200

_US_PER_SECOND = 1_000_000
_SECONDS_PER_HOUR = 3600.0
_MIN_SAMPLES = 3


class ClockDriftError(ValueError):
    """诊断参数非法（时间范围倒置、分析包数超限等）。"""


@dataclass(frozen=True)
class _Sample:
    """一个不同发送时刻的有效样本（同刻已按序号去重）。"""

    seq: int
    position: int  # 该指标首条带 ts 测量在包 measurements 中的原始下标
    sent_at: datetime
    sent_us: int  # sent_at 自 UTC epoch 的微秒数
    ts: datetime
    offset_us: int  # ts - sent_at（微秒）


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


def _median(values: List[float]) -> float:
    """偶数个时取中间两数的平均值（与 statistics.median 一致）。"""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def load_samples(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    from_: datetime,
    to: datetime,
):
    """扫描设备已提交包，提取范围内按发送时刻去重后的样本与排除计数。

    返回 (样本列表, 范围内包数, 无 sent_at 跳过数, 无 ts 跳过数)。
    样本按 sent_at 升序；同一发送时刻只保留序号最大的一包。
    """
    lo_us = _to_us(from_)
    hi_us = _to_us(to)

    in_range_packages = 0
    no_sent_at = 0
    no_ts = 0
    # sent_us -> 该时刻保留的样本；同刻以序号最大者覆盖
    by_sent: Dict[int, _Sample] = {}

    rows = conn.execute(
        "SELECT seq, payload_json FROM committed WHERE device_id = ? ORDER BY seq",
        (device_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        raw_sent_at = payload.get("sent_at")
        if not raw_sent_at:
            no_sent_at += 1  # 缺 sent_at：整包跳过，不占分析上限
            continue
        sent_at = _parse_time(raw_sent_at)
        sent_us = _to_us(sent_at)
        if not (lo_us <= sent_us < hi_us):
            continue  # 发送时间在范围外：不参与，也不计任何排除数
        in_range_packages += 1

        first_ts: Optional[str] = None
        position = -1
        for idx, measurement in enumerate(payload.get("measurements", [])):
            if measurement.get("metric") != metric:
                continue
            raw_ts = measurement.get("ts")
            if raw_ts:
                first_ts = raw_ts
                position = idx
                break  # 每包只取该指标首条带 ts 的测量
        if first_ts is None:
            no_ts += 1  # 范围内但包内无该指标带 ts 测量：整包跳过
            continue

        ts = _parse_time(first_ts)
        sample = _Sample(
            seq=row["seq"],
            position=position,
            sent_at=sent_at,
            sent_us=sent_us,
            ts=ts,
            offset_us=_to_us(ts) - sent_us,
        )
        existing = by_sent.get(sent_us)
        if existing is None or sample.seq > existing.seq:
            by_sent[sent_us] = sample  # 同发送时刻保留序号最大的一包

    if in_range_packages > MAX_PACKAGES:
        raise ClockDriftError(
            f"设备 {device_id} 在发送时间范围 [{from_.isoformat()}, "
            f"{to.isoformat()}) 内有 {in_range_packages} 个已提交包，"
            f"超过单次诊断上限 {MAX_PACKAGES}；请缩小 UTC 时间范围后重试"
            "（服务端不截断）"
        )

    samples = [by_sent[k] for k in sorted(by_sent)]
    return samples, in_range_packages, no_sent_at, no_ts


def _pairwise_slopes(samples: List[_Sample]) -> List[float]:
    """全部不同发送时刻样本对的偏移斜率（秒/秒）。"""
    slopes: List[float] = []
    for i in range(len(samples)):
        for j in range(i + 1, len(samples)):
            dt_us = samples[j].sent_us - samples[i].sent_us
            if dt_us <= 0:
                continue  # 理论上不会发生（已按时刻去重），防御性跳过
            slopes.append(
                (samples[j].offset_us - samples[i].offset_us) / dt_us
            )
    return slopes


def diagnose_clock_drift(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    from_: datetime,
    to: datetime,
    max_abs_drift_per_hour: float,
) -> ClockDriftResponse:
    """执行设备时钟漂移诊断。调用方需保证设备存在。"""
    from_ = _as_utc(from_)
    to = _as_utc(to)
    if from_ >= to:
        raise ClockDriftError("参数 from 必须早于 to")
    if max_abs_drift_per_hour < 0:
        raise ClockDriftError("max_abs_drift_per_hour 不能为负数")

    samples, package_count, no_sent_at, no_ts = load_samples(
        conn, device_id, metric, from_, to
    )

    base = dict(
        device_id=device_id,
        metric=metric,
        **{"from": from_},
        to=to,
        max_abs_drift_per_hour=max_abs_drift_per_hour,
        package_count=package_count,
        excluded_no_sent_at_count=no_sent_at,
        excluded_no_ts_count=no_ts,
        samples=[
            ClockDriftSample(
                seq=s.seq,
                position=s.position,
                sent_at=s.sent_at,
                ts=s.ts,
                offset_seconds=s.offset_us / _US_PER_SECOND,
            )
            for s in samples
        ],
        last_sent_at=samples[-1].sent_at if samples else None,
    )

    if len(samples) < _MIN_SAMPLES:
        return ClockDriftResponse(
            **base,
            sample_count=len(samples),
            sufficient_evidence=False,
            drift_per_hour=None,
            last_offset_seconds=None,
            drift_exceeded=None,
            conclusion="insufficient_evidence",
            conclusion_detail=(
                f"发送时间范围内仅有 {len(samples)} 个不同发送时刻的有效样本，"
                f"不足 {_MIN_SAMPLES} 包，证据不足，无法估计时钟漂移"
            ),
        )

    # 成对斜率中位数 = Theil–Sen 斜率（秒/秒），换算为每小时漂移
    slope_per_second = _median(_pairwise_slopes(samples))
    drift_per_hour = slope_per_second * _SECONDS_PER_HOUR

    # 以中位数斜率把各样本偏移折算到末次发送时刻，残差中位数即稳健截距
    last_sent_us = samples[-1].sent_us
    residuals = [
        s.offset_us - slope_per_second * (s.sent_us - last_sent_us)
        for s in samples
    ]
    last_offset_seconds = _median(residuals) / _US_PER_SECOND

    # 严格大于上限才算超限（边界合格，与其余判定接口口径一致）
    drift_exceeded = abs(drift_per_hour) > max_abs_drift_per_hour
    if drift_exceeded:
        conclusion = "drift_exceeded"
        conclusion_detail = (
            f"估计每小时漂移 {drift_per_hour:.6g} 秒，绝对值超过请求上限 "
            f"{max_abs_drift_per_hour:g} 秒/小时"
        )
    else:
        conclusion = "within_limit"
        conclusion_detail = (
            f"估计每小时漂移 {drift_per_hour:.6g} 秒，绝对值未超过请求上限 "
            f"{max_abs_drift_per_hour:g} 秒/小时"
        )

    return ClockDriftResponse(
        **base,
        sample_count=len(samples),
        sufficient_evidence=True,
        drift_per_hour=drift_per_hour,
        last_offset_seconds=last_offset_seconds,
        drift_exceeded=drift_exceeded,
        conclusion=conclusion,
        conclusion_detail=conclusion_detail,
    )
