"""核心摄入逻辑：幂等去重、乱序缓冲、缺口补齐后按序提交。

提交规则（按设备独立维护）：
- 每台设备记录 committed_upto = 已连续处理（提交或作废）的最大序号；
- 设备首个到达的包用于自举：committed_upto 初始化为 seq - 1，即首包立即提交；
- 之后到达的包：seq <= committed_upto 视为重复（已提交 / 已作废）；
  已在缓冲区视为重复；
- 否则放入缓冲区，随后从 committed_upto + 1 起连续"排水"：
  缓冲中存在的包按序提交，已登记作废的序号直接跨过，直到遇到新缺口；
  作废登记（见 voids.py）与摄入共用本排水路径。
"""

import hashlib
import json
import sqlite3
from typing import Optional

from . import alerts
from .schemas import IngestResponse, PackageIn


class IdempotencyConflict(Exception):
    """同一幂等键携带了不同的载荷。"""


def _canonical_payload(pkg: PackageIn) -> str:
    return json.dumps(
        {
            "device_id": pkg.device_id,
            "seq": pkg.seq,
            "sent_at": pkg.sent_at.isoformat() if pkg.sent_at else None,
            "measurements": [m.model_dump(mode="json") for m in pkg.measurements],
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _payload_hash(pkg: PackageIn) -> str:
    return hashlib.sha256(_canonical_payload(pkg).encode("utf-8")).hexdigest()


def _record_duplicate(
    conn: sqlite3.Connection,
    device_id: str,
    seq: Optional[int],
    key: Optional[str],
    reason: str,
) -> None:
    conn.execute(
        "INSERT INTO duplicates (device_id, seq, idempotency_key, reason) VALUES (?,?,?,?)",
        (device_id, seq, key, reason),
    )


def _store_key(
    conn: sqlite3.Connection,
    key: str,
    pkg: PackageIn,
    payload_hash: str,
    response: IngestResponse,
) -> None:
    conn.execute(
        "INSERT INTO idempotency_keys (key, device_id, seq, payload_hash, response_json)"
        " VALUES (?,?,?,?,?)",
        (key, pkg.device_id, pkg.seq, payload_hash, response.model_dump_json()),
    )


def _is_voided(conn: sqlite3.Connection, device_id: str, seq: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM void_records WHERE device_id = ? AND from_seq <= ? AND to_seq >= ?",
        (device_id, seq, seq),
    ).fetchone() is not None


def drain_committed(conn: sqlite3.Connection, device_id: str, committed_upto: int) -> tuple:
    """从 committed_upto + 1 起按序推进顺序前沿（调用方持 ingest_lock）。

    缓冲区中存在的包移入 committed（返回其 payload，供告警评估）；已登记
    作废的序号不是测量包，直接跨过；直到遇到既未缓冲也未作废的缺口停止。
    返回 (新的 committed_upto, [(seq, payload_json), ...])。
    摄入排水与作废排水共用本函数，保证两条路径语义一致。
    """
    committed_payloads = []
    cur = committed_upto + 1
    while True:
        row = conn.execute(
            "SELECT payload_json FROM buffer WHERE device_id = ? AND seq = ?",
            (device_id, cur),
        ).fetchone()
        if row is None:
            if _is_voided(conn, device_id, cur):
                cur += 1  # 作废序号不是测量包：跨过但不提交
                continue
            break
        conn.execute(
            "INSERT INTO committed (device_id, seq, payload_json) VALUES (?,?,?)",
            (device_id, cur, row["payload_json"]),
        )
        conn.execute(
            "DELETE FROM buffer WHERE device_id = ? AND seq = ?", (device_id, cur)
        )
        committed_payloads.append((cur, row["payload_json"]))
        cur += 1
    return cur - 1, committed_payloads


def ingest_package(conn: sqlite3.Connection, pkg: PackageIn, key: str) -> IngestResponse:
    """在单个事务内完成一次摄入。调用方需持有 ingest_lock。"""
    payload_hash = _payload_hash(pkg)

    # 1) 幂等键重放：直接返回首次处理的结果
    row = conn.execute(
        "SELECT payload_hash, response_json FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    if row is not None:
        if row["payload_hash"] != payload_hash:
            raise IdempotencyConflict(
                f"idempotency key {key!r} 已被不同载荷使用"
            )
        _record_duplicate(conn, pkg.device_id, pkg.seq, key, "idempotent_replay")
        stored = IngestResponse.model_validate_json(row["response_json"])
        return stored.model_copy(update={"status": "duplicate", "duplicate_reason": "idempotent_replay"})

    # 2) 取设备状态；不存在则以首包自举
    dev = conn.execute(
        "SELECT committed_upto FROM devices WHERE device_id = ?", (pkg.device_id,)
    ).fetchone()
    if dev is None:
        committed_upto = pkg.seq - 1
        conn.execute(
            "INSERT INTO devices (device_id, committed_upto) VALUES (?,?)",
            (pkg.device_id, committed_upto),
        )
    else:
        committed_upto = dev["committed_upto"]

    # 3) 重复包判定
    duplicate_reason: Optional[str] = None
    if pkg.seq <= committed_upto:
        # 已越过顺序前沿：可能是已提交包，也可能是已作废序号（缺包迟到）
        duplicate_reason = (
            "already_voided" if _is_voided(conn, pkg.device_id, pkg.seq)
            else "already_committed"
        )
    else:
        in_buffer = conn.execute(
            "SELECT 1 FROM buffer WHERE device_id = ? AND seq = ?",
            (pkg.device_id, pkg.seq),
        ).fetchone()
        if in_buffer is not None:
            duplicate_reason = "already_buffered"

    if duplicate_reason is not None:
        _record_duplicate(conn, pkg.device_id, pkg.seq, key, duplicate_reason)
        response = IngestResponse(
            status="duplicate",
            device_id=pkg.device_id,
            seq=pkg.seq,
            committed_upto=committed_upto,
            committed_seqs=[],
            duplicate_reason=duplicate_reason,
        )
        _store_key(conn, key, pkg, payload_hash, response)
        return response

    # 4) 入缓冲区，随后从下一个期望序号起连续排水提交（作废序号自动跨过）
    payload_json = _canonical_payload(pkg)
    conn.execute(
        "INSERT INTO buffer (device_id, seq, payload_json) VALUES (?,?,?)",
        (pkg.device_id, pkg.seq, payload_json),
    )

    committed_upto, committed_payloads = drain_committed(
        conn, pkg.device_id, committed_upto
    )
    committed_seqs = [s for s, _ in committed_payloads]
    conn.execute(
        "UPDATE devices SET committed_upto = ? WHERE device_id = ?",
        (committed_upto, pkg.device_id),
    )

    # 阈值告警：仅对本次按序排水的包评估，每个包恰好一次。
    # 规则在包创建之后才建立时不回溯历史提交（历史已按当时规则集处理完毕）。
    alerts.evaluate_drain(conn, pkg.device_id, committed_payloads)

    response = IngestResponse(
        status="committed" if pkg.seq in committed_seqs else "buffered",
        device_id=pkg.device_id,
        seq=pkg.seq,
        committed_upto=committed_upto,
        committed_seqs=committed_seqs,
    )
    _store_key(conn, key, pkg, payload_hash, response)
    return response
