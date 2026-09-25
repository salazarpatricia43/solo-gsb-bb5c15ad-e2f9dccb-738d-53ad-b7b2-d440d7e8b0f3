"""只读归档导出：按设备 + seq 范围创建不可变快照任务，分页 NDJSON 续传。

设计要点：

- 创建任务时在单事务内把该设备 [from_seq, to_seq] 范围内**当时已提交**的包
  原样复制进 export_items（只读快照），并计算 manifest。之后即使设备继续
  摄入、排水，导出内容与摘要也完全不变——导出不触碰 committed / buffer /
  duplicates / alert_* 任何业务表。
- 快照复制按 seq 升序**分批流式**进行：以 keyset（seq > 上批末序号）逐批
  读取 committed、逐批 executemany 写入 export_items，摘要逐行增量计算、
  包数与首尾序号逐行累计，任何时刻内存中只保留一批行——即使命中范围（含
  无界范围）覆盖海量包，创建请求的内存占用也只与批大小有关；任何一步失败
  由外层事务整体回滚，不会残留半成品任务。
- 快照行按 seq 升序存储；每个包一行规范化 NDJSON（JSON 键排序 + 行尾
  "\n"，UTF-8），整体 sha256 按行增量计算，与逐页拼接结果一致，
  因此客户端可边下载边自行校验。
- 续传游标为 base64url(JSON)，内含任务 id、下一行偏移量、页大小与任务摘要
  前缀：任务不存在返回 404；游标损坏/属主不符/页大小不符/偏移越界返回 400。
"""

import base64
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .voids import range_has_void


class ExportError(ValueError):
    """导出参数非法（范围非法、页大小非法等）。"""


class InvalidCursor(Exception):
    """续传游标损坏、与任务不匹配或偏移越界。"""


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000

# 创建快照时每批从 committed 读取 / 写入 export_items 的行数。
# 流式批处理使创建请求的内存占用只取决于批大小，而与命中包总量无关。
SNAPSHOT_BATCH_SIZE = 1000


@dataclass(frozen=True)
class ExportTask:
    export_id: str
    device_id: str
    from_seq: Optional[int]
    to_seq: Optional[int]
    page_size: int
    package_count: int
    first_seq: Optional[int]
    last_seq: Optional[int]
    complete: bool
    content_sha256: str
    created_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def canonical_line(payload_json: str) -> bytes:
    """把已存储的规范化包载荷转成导出用的一行 NDJSON（含行尾 \\n）。

    再次 sort_keys 输出，保证即使存储侧格式演进，导出字节流仍确定。
    """
    line = json.dumps(
        json.loads(payload_json), sort_keys=True, ensure_ascii=False
    )
    return line.encode("utf-8") + b"\n"


def _encode_cursor(
    export_id: str, offset: int, page_size: int, digest_prefix: str
) -> str:
    body = json.dumps(
        {"id": export_id, "offset": offset, "page_size": page_size,
         "chk": digest_prefix},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(body)
    except Exception as exc:
        raise InvalidCursor("游标无法解析") from exc
    if not isinstance(data, dict) or not all(
        k in data for k in ("id", "offset", "page_size", "chk")
    ):
        raise InvalidCursor("游标字段不完整")
    if not isinstance(data["id"], str) or not isinstance(data["chk"], str):
        raise InvalidCursor("游标字段类型错误")
    if not isinstance(data["offset"], int) or isinstance(data["offset"], bool):
        raise InvalidCursor("游标字段类型错误")
    if not isinstance(data["page_size"], int) or isinstance(data["page_size"], bool):
        raise InvalidCursor("游标字段类型错误")
    return data


def _row_to_task(row: sqlite3.Row) -> ExportTask:
    return ExportTask(
        export_id=row["export_id"],
        device_id=row["device_id"],
        from_seq=row["from_seq"],
        to_seq=row["to_seq"],
        page_size=row["page_size"],
        package_count=row["package_count"],
        first_seq=row["first_seq"],
        last_seq=row["last_seq"],
        complete=bool(row["complete"]),
        content_sha256=row["content_sha256"],
        created_at=row["created_at"],
    )


def get_task(conn: sqlite3.Connection, export_id: str) -> Optional[ExportTask]:
    row = conn.execute(
        "SELECT * FROM export_tasks WHERE export_id = ?", (export_id,)
    ).fetchone()
    return _row_to_task(row) if row is not None else None


def manifest_dict(task: ExportTask) -> dict:
    return {
        "export_id": task.export_id,
        "device_id": task.device_id,
        "from_seq": task.from_seq,
        "to_seq": task.to_seq,
        "package_count": task.package_count,
        "first_seq": task.first_seq,
        "last_seq": task.last_seq,
        "complete": task.complete,
        "page_size": task.page_size,
        "content_sha256": task.content_sha256,
        "created_at": task.created_at,
    }


def first_cursor(task: ExportTask) -> str:
    return _encode_cursor(
        task.export_id, 0, task.page_size, task.content_sha256[:12]
    )


def create_export(
    conn: sqlite3.Connection,
    device_id: str,
    from_seq: Optional[int],
    to_seq: Optional[int],
    page_size: int = DEFAULT_PAGE_SIZE,
) -> ExportTask:
    """创建一次只读快照导出。调用方需持有 ingest_lock 并保证设备存在。

    快照范围为创建时刻 committed 表中 from_seq <= seq <= to_seq 的全部包。

    全程在调用方的同一事务内完成：先插入占位 export_tasks（满足
    export_items 外键），再按 seq 升序 keyset 分批读取已提交包，逐批写入
    快照行并逐行累计摘要 / 包数 / 首尾序号，最后回填 manifest 各列。
    任何一步抛错都由外层事务整体回滚，不留半成品任务。命中范围（含两端
    无界）再大，内存中也只保留一批行。
    """
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ExportError(f"page_size 必须在 1..{MAX_PAGE_SIZE} 之间")
    if from_seq is not None and to_seq is not None and from_seq > to_seq:
        raise ExportError("from_seq 不能大于 to_seq")

    where = ["device_id = ?"]
    params: list = [device_id]
    if from_seq is not None:
        where.append("seq >= ?")
        params.append(from_seq)
    if to_seq is not None:
        where.append("seq <= ?")
        params.append(to_seq)
    clause = " AND ".join(where)

    export_id = uuid.uuid4().hex
    created_at = _utc_now()

    # 先写父表 export_tasks 占位行（外键约束要求），manifest 统计列扫描
    # 完成后再回填；中途失败随事务整体回滚，占位行不会残留。
    conn.execute(
        """
        INSERT INTO export_tasks
            (export_id, device_id, from_seq, to_seq, page_size, package_count,
             first_seq, last_seq, complete, content_sha256, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            export_id, device_id, from_seq, to_seq, page_size, 0,
            None, None, 0, hashlib.sha256(b"").hexdigest(), created_at,
        ),
    )

    # 分批 keyset 扫描：seq > ? 沿主键序推进，不随总量增长内存占用。
    # committed 在本事务持锁期间不会被摄入 / 排水修改，各批拼起来即创建
    # 时刻范围内的完整快照，与一次性 SELECT ... ORDER BY seq 的结果一致。
    digest = hashlib.sha256()
    first_seq: Optional[int] = None
    last_seq: Optional[int] = None
    package_count = 0
    batch_params = list(params)
    while True:
        rows = conn.execute(
            f"SELECT seq, payload_json FROM committed WHERE {clause}"
            " AND seq > ? ORDER BY seq LIMIT ?",
            (*batch_params, last_seq if last_seq is not None else 0,
             SNAPSHOT_BATCH_SIZE),
        ).fetchall()
        if not rows:
            break
        items: List[tuple] = []
        for row in rows:
            chunk = canonical_line(row["payload_json"])
            digest.update(chunk)
            if package_count == 0:
                first_seq = row["seq"]
            last_seq = row["seq"]
            items.append((export_id, package_count, row["seq"], chunk))
            package_count += 1
        conn.executemany(
            "INSERT INTO export_items (export_id, item_index, seq, line)"
            " VALUES (?,?,?,?)",
            items,
        )
        if len(rows) < SNAPSHOT_BATCH_SIZE:
            break

    # 请求范围是否被已提交数据连续完整覆盖。作废序号不是真实测量包，
    # 因此除"范围与 committed 完全重合且无未排水缓冲"外，还要求范围内
    # 不含任何作废段（committed 在作废之后仍按 seq 连续存储，无法仅靠
    # committed 自身发现缺口，必须显式排查 void_records）。
    dev_row = conn.execute(
        "SELECT committed_upto FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    committed_upto = dev_row["committed_upto"]
    want_hi = to_seq if to_seq is not None else committed_upto
    want_lo = from_seq if from_seq is not None else first_seq
    buffered_beyond = 0
    if to_seq is None and package_count > 0:
        buffered_beyond = conn.execute(
            "SELECT COUNT(*) AS n FROM buffer WHERE device_id = ? AND seq > ?",
            (device_id, committed_upto),
        ).fetchone()["n"]
    range_lo = from_seq if from_seq is not None else first_seq
    range_hi = to_seq if to_seq is not None else last_seq
    void_in_range = (
        range_has_void(conn, device_id, range_lo, range_hi)
        if range_lo is not None and range_hi is not None
        else False
    )
    complete = (
        package_count > 0
        and want_lo is not None
        and want_lo == first_seq
        and want_hi == last_seq
        and buffered_beyond == 0
        and not void_in_range
    )

    # 回填 manifest 统计列；至此快照行与摘要均已落库（仍在同一事务内）
    conn.execute(
        """
        UPDATE export_tasks
           SET package_count = ?, first_seq = ?, last_seq = ?,
               complete = ?, content_sha256 = ?
         WHERE export_id = ?
        """,
        (
            package_count, first_seq, last_seq, 1 if complete else 0,
            digest.hexdigest(), export_id,
        ),
    )
    task = get_task(conn, export_id)
    assert task is not None
    return task


def fetch_page(
    conn: sqlite3.Connection, task: ExportTask, cursor: str
) -> dict:
    """按游标取出一页 NDJSON 行并生成下一个游标。游标越界 / 不匹配抛 InvalidCursor。"""
    data = _decode_cursor(cursor)
    if data["id"] != task.export_id:
        raise InvalidCursor("游标不属于该导出任务")
    if data["chk"] != task.content_sha256[:12]:
        raise InvalidCursor("游标与导出摘要不匹配")
    if data["page_size"] != task.page_size:
        raise InvalidCursor("游标中的分页大小与任务不一致")
    offset = data["offset"]
    if offset < 0 or offset > task.package_count:
        raise InvalidCursor(
            f"游标偏移越界：offset={offset}，包总数={task.package_count}"
        )
    # 服务端永远不会签发 offset == package_count 的游标（末页 done=true 且无后续
    # 游标），因此收到该偏移说明客户端重放了终点，按越界处理；空导出的首页除外。
    if offset == task.package_count and task.package_count > 0:
        raise InvalidCursor(
            f"游标偏移越界：导出已在 offset={offset} 处结束，没有更多页"
        )

    rows = conn.execute(
        "SELECT seq, line FROM export_items WHERE export_id = ?"
        " ORDER BY item_index LIMIT ? OFFSET ?",
        (task.export_id, task.page_size, offset),
    ).fetchall()
    lines: List[bytes] = [r["line"] for r in rows]
    next_offset = offset + len(rows)
    done = next_offset >= task.package_count
    next_cursor = (
        None
        if done
        else _encode_cursor(
            task.export_id, next_offset, task.page_size,
            task.content_sha256[:12],
        )
    )
    return {
        "lines": lines,
        "seqs": [r["seq"] for r in rows],
        "offset": offset,
        "next_offset": next_offset,
        "next_cursor": next_cursor,
        "done": done,
    }


def verify_manifest(task: ExportTask, content_sha256: str) -> bool:
    """校验客户端提交的导出内容摘要（小写十六进制，大小写不敏感）。"""
    return (
        content_sha256.strip().lower() == task.content_sha256.lower()
    )
