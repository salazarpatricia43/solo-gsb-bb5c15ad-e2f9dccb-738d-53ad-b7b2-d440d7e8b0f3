"""双指标配比验收：按设备配置在已提交包内一对一配组并判定比例。

纯只读查询（配置写入除外）：验收只读取 committed 表中已提交包的原始
payload，不触碰缓冲包、作废序号、重复、告警与校准数据，也不改写任何
已存内容。

配组规则：

- 仅扫描请求的 seq 闭区间 [from_seq, to_seq] 内**真实已提交**的包，按
  seq 升序；缓冲包尚未提交、作废序号本就不在 committed 中，二者天然
  不参与计算；
- 在**每个包内**分别按出现顺序（measurements 的原始下标）收集分子指标
  与分母指标的全部读数，两侧按下标一对一配成第 0、1、2… 组，配组
  **绝不跨包**；某一侧读数较少时，多余侧残余读数各自成组并按
  "缺少对应读数"标为无效；
- 逐组序号从 1 起、按包序号与包内位置跨包连续编号；
- 无效原因（按判定优先级，一组只记首个命中的原因）：
  ① missing_numerator / missing_denominator：该组缺少对应一侧读数；
  ② unit_mismatch：任一侧读数单位与配置要求的单位不符（读数缺单位
    同样视为不符——验收要求两侧各自单位，未填即无法确认）；
  ③ zero_denominator：分母读数为零，比例不可计算；
- 有效组比例 = 分子读数 / 分母读数，比例落在配置的闭区间
  [ratio_lower, ratio_upper] 内（含边界）该组 pass，否则 fail；
- 整体验收 accepted 当且仅当有效组数 >= min_valid_groups 且不存在
  fail 的有效组（无效组不直接否决，但拉低有效组数）。
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from .schemas import (
    RatioAcceptanceOut,
    RatioAcceptanceResponse,
    RatioGroup,
    RatioReading,
)


class RatioAcceptanceError(ValueError):
    """配比验收参数非法（区间倒置、下限大于上限等）。"""


class RatioAcceptanceConflict(Exception):
    """该设备已存在配比验收配置（一台设备至多一条）。"""


class RatioAcceptanceNotFound(LookupError):
    """设备尚无配比验收配置。"""


@dataclass(frozen=True)
class _Reading:
    """包内某指标的一次读数；position 为其在 measurements 中的原始下标。"""

    seq: int
    position: int
    value: float
    unit: Optional[str]


def create_acceptance(
    conn: sqlite3.Connection,
    device_id: str,
    numerator_metric: str,
    denominator_metric: str,
    numerator_unit: str,
    denominator_unit: str,
    ratio_lower: float,
    ratio_upper: float,
    min_valid_groups: int,
) -> int:
    """为设备保存配比验收配置并返回新配置 id；每台设备至多一条。

    指标不同、下限不大于上限、最低有效组数为正由调用方（schema）校验，
    此处再在 SQL CHECK 兜底；重复配置抛 RatioAcceptanceConflict（409）。
    调用方持有 ingest_lock。
    """
    try:
        cur = conn.execute(
            """
            INSERT INTO ratio_acceptances
                (device_id, numerator_metric, denominator_metric,
                 numerator_unit, denominator_unit,
                 ratio_lower, ratio_upper, min_valid_groups)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                device_id,
                numerator_metric,
                denominator_metric,
                numerator_unit,
                denominator_unit,
                ratio_lower,
                ratio_upper,
                min_valid_groups,
            ),
        )
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        if "UNIQUE" in message:
            raise RatioAcceptanceConflict(
                f"设备 {device_id} 已存在双指标配比验收配置"
            ) from exc
        raise RatioAcceptanceError(
            "配比验收配置非法：分子/分母指标必须不同、下限不得大于上限、"
            "最低有效组数至少为 1"
        ) from exc
    return cur.lastrowid


def list_acceptances(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ratio_acceptances ORDER BY id"
    ).fetchall()


def get_acceptance(conn: sqlite3.Connection, device_id: str) -> Optional[sqlite3.Row]:
    """返回设备的配比验收配置行；无配置返回 None。"""
    return conn.execute(
        "SELECT * FROM ratio_acceptances WHERE device_id = ?", (device_id,)
    ).fetchone()


def _readings_in_package(
    payload: dict,
    seq: int,
    numerator_metric: str,
    denominator_metric: str,
) -> tuple:
    """按出现顺序收集一个包内分子 / 分母指标的全部读数（保留原始下标）。"""
    numerators: List[_Reading] = []
    denominators: List[_Reading] = []
    for position, measurement in enumerate(payload.get("measurements", [])):
        metric = measurement.get("metric")
        if metric == numerator_metric:
            numerators.append(
                _Reading(
                    seq=seq,
                    position=position,
                    value=float(measurement["value"]),
                    unit=measurement.get("unit"),
                )
            )
        elif metric == denominator_metric:
            denominators.append(
                _Reading(
                    seq=seq,
                    position=position,
                    value=float(measurement["value"]),
                    unit=measurement.get("unit"),
                )
            )
    return numerators, denominators


def _reading_out(reading: _Reading, required_unit: str) -> RatioReading:
    return RatioReading(
        seq=reading.seq,
        position=reading.position,
        value=reading.value,
        unit=reading.unit,
        unit_matches=reading.unit == required_unit,
    )


def _build_group(
    index: int,
    seq: int,
    position: int,
    numerator: Optional[_Reading],
    denominator: Optional[_Reading],
    numerator_unit: str,
    denominator_unit: str,
    ratio_lower: float,
    ratio_upper: float,
) -> RatioGroup:
    """根据两侧读数构造一组结果，按优先级判定无效原因与结论。"""
    num_out = (
        _reading_out(numerator, numerator_unit) if numerator is not None else None
    )
    den_out = (
        _reading_out(denominator, denominator_unit)
        if denominator is not None
        else None
    )

    invalid_reason = None
    detail = None
    if numerator is None:
        invalid_reason = "missing_numerator"
        detail = f"包 {seq} 的第 {position + 1} 组缺少分子指标读数，无法配组"
    elif denominator is None:
        invalid_reason = "missing_denominator"
        detail = f"包 {seq} 的第 {position + 1} 组缺少分母指标读数，无法配组"
    elif numerator.unit != numerator_unit:
        invalid_reason = "unit_mismatch"
        detail = (
            f"包 {seq} 第 {position + 1} 组分子读数单位 "
            f"{numerator.unit!r} 与配置要求 {numerator_unit!r} 不符"
        )
    elif denominator.unit != denominator_unit:
        invalid_reason = "unit_mismatch"
        detail = (
            f"包 {seq} 第 {position + 1} 组分母读数单位 "
            f"{denominator.unit!r} 与配置要求 {denominator_unit!r} 不符"
        )
    elif denominator.value == 0:
        invalid_reason = "zero_denominator"
        detail = f"包 {seq} 第 {position + 1} 组分母读数为零，比例不可计算"

    if invalid_reason is not None:
        return RatioGroup(
            index=index,
            seq=seq,
            position=position,
            numerator=num_out,
            denominator=den_out,
            valid=False,
            invalid_reason=invalid_reason,
            invalid_reason_detail=detail,
            ratio=None,
            conclusion="invalid",
        )

    ratio = numerator.value / denominator.value
    passed = ratio_lower <= ratio <= ratio_upper  # 闭区间，边界合格
    return RatioGroup(
        index=index,
        seq=seq,
        position=position,
        numerator=num_out,
        denominator=den_out,
        valid=True,
        invalid_reason=None,
        invalid_reason_detail=None,
        ratio=ratio,
        conclusion="pass" if passed else "fail",
    )


def evaluate(
    conn: sqlite3.Connection,
    device_id: str,
    from_seq: int,
    to_seq: int,
) -> RatioAcceptanceResponse:
    """对设备在 [from_seq, to_seq] 闭区间内的已提交包执行配比验收。

    调用方需保证设备存在；无配置抛 RatioAcceptanceNotFound。
    """
    if from_seq < 1 or to_seq < 1:
        raise RatioAcceptanceError("序号必须 >= 1")
    if from_seq > to_seq:
        raise RatioAcceptanceError("from_seq 不能大于 to_seq")

    row = get_acceptance(conn, device_id)
    if row is None:
        raise RatioAcceptanceNotFound(
            f"设备 {device_id} 尚无双指标配比验收配置"
        )
    config = RatioAcceptanceOut(**dict(row))

    groups: List[RatioGroup] = []
    package_count = 0
    next_index = 1

    committed_rows = conn.execute(
        """
        SELECT seq, payload_json FROM committed
         WHERE device_id = ? AND seq >= ? AND seq <= ?
         ORDER BY seq
        """,
        (device_id, from_seq, to_seq),
    ).fetchall()
    for crow in committed_rows:
        seq = crow["seq"]
        payload = json.loads(crow["payload_json"])
        numerators, denominators = _readings_in_package(
            payload,
            seq,
            config.numerator_metric,
            config.denominator_metric,
        )
        package_count += 1  # 区间内真实已提交包都参与扫描（缓冲/作废不计）
        if not numerators and not denominators:
            continue  # 包内两种指标都未出现：不产生组

        # 包内按两侧各自出现顺序一对一配组；残余读数各自成无效组，不跨包
        pair_count = max(len(numerators), len(denominators))
        for position in range(pair_count):
            numerator = numerators[position] if position < len(numerators) else None
            denominator = (
                denominators[position] if position < len(denominators) else None
            )
            groups.append(
                _build_group(
                    next_index,
                    seq,
                    position,
                    numerator,
                    denominator,
                    config.numerator_unit,
                    config.denominator_unit,
                    config.ratio_lower,
                    config.ratio_upper,
                )
            )
            next_index += 1

    valid_groups = [g for g in groups if g.valid]
    failed_groups = [g for g in valid_groups if g.conclusion == "fail"]
    invalid_count = len(groups) - len(valid_groups)

    not_accepted_reason = None
    if failed_groups:
        failed_indexes = [str(g.index) for g in failed_groups]
        not_accepted_reason = (
            f"有效组 {', '.join(failed_indexes)} 的比例超出闭区间 "
            f"[{config.ratio_lower}, {config.ratio_upper}]"
        )
    elif len(valid_groups) < config.min_valid_groups:
        not_accepted_reason = (
            f"有效组数 {len(valid_groups)} 未达到最低要求 "
            f"{config.min_valid_groups}"
        )

    return RatioAcceptanceResponse(
        device_id=device_id,
        from_seq=from_seq,
        to_seq=to_seq,
        config=config,
        package_count=package_count,
        group_count=len(groups),
        valid_group_count=len(valid_groups),
        invalid_group_count=invalid_count,
        failed_group_count=len(failed_groups),
        accepted=not_accepted_reason is None,
        not_accepted_reason=not_accepted_reason,
        groups=groups,
    )
