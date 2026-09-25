"""缺包作废：把确定缺失的连续序号段登记作废，推进顺序前沿并立即排水。

作废仅允许针对**已有设备**的一段当前确为缺口的连续序号：

- 起点必须恰好是 next_expected（= committed_upto + 1），不能跳过任何
  尚未处理的序号，也不能作废已处理区间；
- 段尾不得超过当前缓冲区最大序号——缓冲区之外没有"已知边界"，
  作废延申到无穷远处没有意义；
- 段内每个序号当前都必须是缺口：已提交 / 已缓冲 / 已作废的序号
  一律不得作废；
- 作废与摄入共用 ingest_lock 与同一数据库事务：先在 void_records
  登记作废段，再通过与摄入相同的排水路径推进顺序前沿，随后连续的
  缓冲包立即按序提交；排水时沿用现有告警评估——作废序号不是测量包，
  绝不伪装成测量数据参与状态机；
- 同一作废请求按幂等键重试：返回首次结果，不重复登记、不重复推进；
  幂等键被不同载荷使用、或请求与当前设备状态冲突（起点不是
  next_expected、越过缓冲边界、段内含缓冲包等）返回 409。

作废之后迟到的"作废序号"原包：在摄入侧被判为 already_voided
重复（见 logic.ingest_package），不会复活，也不会写入 committed。
"""

import hashlib
import json
import sqlite3
from typing import Optional

from . import alerts
from .logic import drain_committed
from .schemas import VoidResponse


class VoidError(ValueError):
    """作废请求参数非法（无设备前提不成立时由路由返回 404）。"""


class VoidConflict(Exception):
    """作废请求与幂等记录或当前设备状态冲突。"""


def _canonical_request(device_id: str, from_seq: int, to_seq: int) -> str:
    return json.dumps(
        {"device_id": device_id, "from_seq": from_seq, "to_seq": to_seq},
        sort_keys=True,
        ensure_ascii=False,
    )


def _request_hash(device_id: str, from_seq: int, to_seq: int) -> str:
    return hashlib.sha256(
        _canonical_request(device_id, from_seq, to_seq).encode("utf-8")
    ).hexdigest()


def void_missing(
    conn: sqlite3.Connection,
    device_id: str,
    from_seq: int,
    to_seq: int,
    key: str,
) -> VoidResponse:
    """在单个事务内登记一段缺包作废并排水。调用方需持有 ingest_lock。

    设备必须已存在（由路由层保证，未知设备返回 404）。
    """
    if from_seq < 1 or to_seq < from_seq:
        raise VoidError("作废段非法：要求 1 <= from_seq <= to_seq")
    payload_hash = _request_hash(device_id, from_seq, to_seq)

    # 1) 幂等键重放：直接返回首次处理的结果，不重复登记、不重复推进
    row = conn.execute(
        "SELECT payload_hash, response_json FROM void_idempotency_keys WHERE key = ?",
        (key,),
    ).fetchone()
    if row is not None:
        if row["payload_hash"] != payload_hash:
            raise VoidConflict(f"idempotency key {key!r} 已被不同载荷使用")
        return VoidResponse.model_validate_json(row["response_json"])

    dev = conn.execute(
        "SELECT committed_upto FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if dev is None:  # 路由已校验，此处防御
        raise VoidError(f"未知设备: {device_id}")
    committed_upto = dev["committed_upto"]
    next_expected = committed_upto + 1

    # 2) 段约束校验
    if from_seq != next_expected:
        raise VoidConflict(
            f"作废起点必须是当前 next_expected={next_expected}，收到 from_seq={from_seq}"
        )

    max_buffered_row = conn.execute(
        "SELECT MAX(seq) AS m FROM buffer WHERE device_id = ?", (device_id,)
    ).fetchone()
    max_buffered = max_buffered_row["m"]
    if max_buffered is None:
        raise VoidConflict(
            "当前没有缓冲包：作废段尾不得超过缓冲区最大序号，无缓冲时不存在可作废缺口"
        )
    if to_seq > max_buffered:
        raise VoidConflict(
            f"作废段尾 to_seq={to_seq} 超过当前缓冲区最大序号 {max_buffered}"
        )

    buffered_in_range = conn.execute(
        "SELECT seq FROM buffer WHERE device_id = ? AND seq BETWEEN ? AND ? ORDER BY seq",
        (device_id, from_seq, to_seq),
    ).fetchall()
    if buffered_in_range:
        seqs = [r["seq"] for r in buffered_in_range]
        raise VoidConflict(
            f"序号 {seqs} 已在缓冲区：已缓冲序号不得作废，请等待其按序提交"
        )

    overlap = conn.execute(
        "SELECT 1 FROM void_records WHERE device_id = ? AND from_seq <= ? AND to_seq >= ?",
        (device_id, to_seq, from_seq),
    ).fetchone()
    if overlap is not None:
        raise VoidConflict(
            f"序号段 [{from_seq}, {to_seq}] 与已有作废记录重叠，不得重复作废"
        )

    # 3) 登记作废段（随后缓冲排水会把它作为缺口跨过）
    conn.execute(
        "INSERT INTO void_records (device_id, from_seq, to_seq) VALUES (?,?,?)",
        (device_id, from_seq, to_seq),
    )

    # 4) 与摄入共用的排水：跨过作废序号，提交其后连续的缓冲包。
    #    仅真实提交的包进入 committed_payloads，告警评估不会看到作废序号。
    new_upto, committed_payloads = drain_committed(
        conn, device_id, committed_upto
    )
    conn.execute(
        "UPDATE devices SET committed_upto = ? WHERE device_id = ?",
        (new_upto, device_id),
    )
    alerts.evaluate_drain(conn, device_id, committed_payloads)

    response = VoidResponse(
        status="voided",
        device_id=device_id,
        from_seq=from_seq,
        to_seq=to_seq,
        voided_seqs=list(range(from_seq, to_seq + 1)),
        committed_upto=new_upto,
        committed_seqs=[s for s, _ in committed_payloads],
    )
    conn.execute(
        "INSERT INTO void_idempotency_keys"
        " (key, device_id, from_seq, to_seq, payload_hash, response_json)"
        " VALUES (?,?,?,?,?,?)",
        (
            key,
            device_id,
            from_seq,
            to_seq,
            payload_hash,
            response.model_dump_json(),
        ),
    )
    return response


def list_void_records(
    conn: sqlite3.Connection,
    device_id: str,
    limit: int,
    offset: int,
) -> tuple:
    """分页返回设备的作废登记（按登记先后升序），返回 (总数, 行列表)。"""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM void_records WHERE device_id = ?",
        (device_id,),
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT * FROM void_records WHERE device_id = ?"
        " ORDER BY id LIMIT ? OFFSET ?",
        (device_id, limit, offset),
    ).fetchall()
    return total, rows


def voided_count(conn: sqlite3.Connection, device_id: str) -> int:
    """设备已作废序号总数（段长之和）。"""
    return conn.execute(
        "SELECT COALESCE(SUM(to_seq - from_seq + 1), 0) AS n"
        " FROM void_records WHERE device_id = ?",
        (device_id,),
    ).fetchone()["n"]


def range_has_void(
    conn: sqlite3.Connection,
    device_id: str,
    lo: Optional[int],
    hi: Optional[int],
) -> bool:
    """[lo, hi]（任一端 None 表示无界）是否与任何作废段相交。"""
    where = ["device_id = ?"]
    params: list = [device_id]
    if hi is not None:
        where.append("from_seq <= ?")
        params.append(hi)
    if lo is not None:
        where.append("to_seq >= ?")
        params.append(lo)
    return conn.execute(
        f"SELECT 1 FROM void_records WHERE {' AND '.join(where)} LIMIT 1",
        params,
    ).fetchone() is not None
