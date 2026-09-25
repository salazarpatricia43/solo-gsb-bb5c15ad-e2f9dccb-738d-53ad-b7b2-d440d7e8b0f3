"""只读归档导出测试：快照创建、NDJSON 分页游标续传、manifest 校验、
越界/未知任务错误、内容稳定性，以及导出不影响提交/缓冲/重复/告警数据。"""

import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import exports as export_mod
from app import db as db_mod


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(device, seq, key, value=1.0, sent_at=None):
    body = {
        "device_id": device,
        "seq": seq,
        "idempotency_key": key,
        "measurements": [{"metric": "temperature", "value": value, "unit": "C"}],
    }
    if sent_at is not None:
        body["sent_at"] = sent_at
    return body


def ingest_range(client, device, lo, hi, **kw):
    for i in range(lo, hi + 1):
        client.post(
            "/api/v1/packages",
            json=pkg(device, i, f"exp-{device}-{i}", value=float(i), **kw),
        )


def download_all(client, export_id, cursor=None):
    """按游标下载全部分页，返回 (拼接字节, 每页字节列表, 偏移列表)。"""
    pages, offsets, done_flags = [], [], []
    while True:
        params = {"cursor": cursor} if cursor else {}
        r = client.get(f"/api/v1/exports/{export_id}/pages", params=params)
        assert r.status_code == 200, r.text
        pages.append(r.content)
        offsets.append(int(r.headers["x-page-offset"]))
        done_flags.append(r.headers["x-export-done"])
        cursor = r.headers.get("x-next-cursor")
        if done_flags[-1] == "true":
            assert cursor is None
            break
        assert cursor is not None
    return b"".join(pages), pages, offsets, done_flags


def parse_ndjson(data):
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


def test_create_export_basic_manifest_and_digest(client):
    ingest_range(client, "exp-a", 1, 5)
    r = client.post("/api/v1/exports", json={"device_id": "exp-a", "page_size": 2})
    assert r.status_code == 201
    m = r.json()
    assert m["package_count"] == 5
    assert m["first_seq"] == 1 and m["last_seq"] == 5
    assert m["from_seq"] is None and m["to_seq"] is None
    assert m["complete"] is True
    assert m["next_cursor"]

    # manifest 查询接口内容一致
    gm = client.get(f"/api/v1/exports/{m['export_id']}/manifest")
    assert gm.status_code == 200
    assert gm.json() == {k: v for k, v in m.items() if k != "next_cursor"}

    data, pages, offsets, done_flags = download_all(client, m["export_id"])
    # page_size=2 -> 2,2,1 三页
    assert [len(p) > 0 for p in pages] == [True, True, True]
    assert offsets == [0, 2, 4]
    assert done_flags == ["false", "false", "true"]

    lines = parse_ndjson(data)
    assert [line["seq"] for line in lines] == [1, 2, 3, 4, 5]
    # 实际下发字节的摘要必须等于 manifest 摘要
    assert hashlib.sha256(data).hexdigest() == m["content_sha256"]


def test_cursor_resume_is_stable_and_repeatable(client):
    ingest_range(client, "exp-b", 1, 4)
    m = client.post(
        "/api/v1/exports", json={"device_id": "exp-b", "page_size": 3}
    ).json()
    eid = m["export_id"]

    first = client.get(f"/api/v1/exports/{eid}/pages")
    assert first.status_code == 200
    cursor = first.headers["x-next-cursor"]
    second = client.get(f"/api/v1/exports/{eid}/pages", params={"cursor": cursor})
    assert second.status_code == 200
    assert second.headers["x-export-done"] == "true"
    assert "x-next-cursor" not in second.headers

    # 同一游标任意重复续传：字节、摘要完全一致
    second_again = client.get(
        f"/api/v1/exports/{eid}/pages", params={"cursor": cursor}
    )
    assert second_again.content == second.content
    first_again = client.get(f"/api/v1/exports/{eid}/pages")
    assert first_again.content == first.content

    full = first.content + second.content
    full_replay = first_again.content + second_again.content
    assert full == full_replay
    assert hashlib.sha256(full).hexdigest() == m["content_sha256"]
    # 每页响应头都携带稳定摘要
    assert first.headers["x-content-sha256"] == m["content_sha256"]
    assert second.headers["x-content-sha256"] == m["content_sha256"]


def test_snapshot_immutable_after_more_ingest(client):
    ingest_range(client, "exp-c", 1, 2)
    m1 = client.post("/api/v1/exports", json={"device_id": "exp-c"}).json()
    # 导出后设备继续摄入
    ingest_range(client, "exp-c", 3, 6)
    data1, *_ = download_all(client, m1["export_id"])
    assert [p["seq"] for p in parse_ndjson(data1)] == [1, 2]
    assert hashlib.sha256(data1).hexdigest() == m1["content_sha256"]

    # 新建导出能看到全部数据；旧导出摘要不受影响
    m2 = client.post("/api/v1/exports", json={"device_id": "exp-c"}).json()
    assert m2["package_count"] == 6 and m2["first_seq"] == 1 and m2["last_seq"] == 6
    assert m2["content_sha256"] != m1["content_sha256"]


def test_seq_range_subset_and_complete_flag(client):
    ingest_range(client, "exp-d", 1, 10)
    r = client.post(
        "/api/v1/exports",
        json={"device_id": "exp-d", "from_seq": 3, "to_seq": 7, "page_size": 50},
    )
    assert r.status_code == 201
    m = r.json()
    assert m["package_count"] == 5
    assert m["first_seq"] == 3 and m["last_seq"] == 7
    assert m["complete"] is True
    data, *_ = download_all(client, m["export_id"])
    assert [p["seq"] for p in parse_ndjson(data)] == [3, 4, 5, 6, 7]

    # 范围超出已提交边界 -> 快照只含已有部分，complete=False
    r2 = client.post(
        "/api/v1/exports", json={"device_id": "exp-d", "from_seq": 8, "to_seq": 20}
    )
    m2 = r2.json()
    assert m2["package_count"] == 3 and m2["first_seq"] == 8 and m2["last_seq"] == 10
    assert m2["complete"] is False


def test_buffered_packages_excluded_and_export_is_readonly(client):
    # seq 1 提交；seq 3 先缓冲（缺 2）
    client.post("/api/v1/packages", json=pkg("exp-e", 1, "exp-e-1"))
    client.post("/api/v1/packages", json=pkg("exp-e", 3, "exp-e-3"))
    assert client.get("/api/v1/devices/exp-e/gaps").json()["buffered_seqs"] == [3]

    # 配一条告警规则（针对历史包不回溯，仅用于确认导出不产生事件）
    assert client.post(
        "/api/v1/rules",
        json={
            "device_id": "exp-e",
            "metric": "temperature",
            "upper_limit": 100,
        },
    ).status_code == 201

    m = client.post("/api/v1/exports", json={"device_id": "exp-e"}).json()
    assert m["package_count"] == 1 and m["first_seq"] == m["last_seq"] == 1
    assert m["complete"] is False  # 设备仍有缺口/缓冲
    data, pages, *_ = download_all(client, m["export_id"])
    assert [p["seq"] for p in parse_ndjson(data)] == [1]

    # 导出前后：缺口、缓冲、已提交、重复、告警事件均不变
    gaps = client.get("/api/v1/devices/exp-e/gaps").json()
    assert gaps["buffered_seqs"] == [3] and gaps["gaps"] == [2]
    committed = client.get("/api/v1/devices/exp-e/committed").json()
    assert committed["seqs"] == [1]
    dups = client.get("/api/v1/devices/exp-e/duplicates").json()
    assert dups["count"] == 0
    events = client.get("/api/v1/devices/exp-e/alerts/events").json()
    assert events["count"] == 0

    # 补齐缺口后旧导出仍然只有 seq 1
    client.post("/api/v1/packages", json=pkg("exp-e", 2, "exp-e-2"))
    data_after, *_ = download_all(client, m["export_id"])
    assert data_after == data
    assert client.get("/api/v1/devices/exp-e/gaps").json()["buffered_seqs"] == []


def test_empty_export(client):
    ingest_range(client, "exp-f", 1, 3)
    r = client.post(
        "/api/v1/exports",
        json={"device_id": "exp-f", "from_seq": 100, "to_seq": 200},
    )
    assert r.status_code == 201
    m = r.json()
    assert m["package_count"] == 0
    assert m["first_seq"] is None and m["last_seq"] is None
    assert m["complete"] is False
    assert m["content_sha256"] == hashlib.sha256(b"").hexdigest()

    page = client.get(f"/api/v1/exports/{m['export_id']}/pages")
    assert page.status_code == 200
    assert page.content == b""
    assert page.headers["x-export-done"] == "true"
    assert "x-next-cursor" not in page.headers


def test_verify_manifest_endpoint(client):
    ingest_range(client, "exp-g", 1, 3)
    m = client.post("/api/v1/exports", json={"device_id": "exp-g"}).json()
    data, *_ = download_all(client, m["export_id"])
    digest = hashlib.sha256(data).hexdigest()

    ok = client.post(
        f"/api/v1/exports/{m['export_id']}/verify",
        json={"content_sha256": digest},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["valid"] is True
    assert body["expected_sha256"] == digest
    assert body["manifest"]["export_id"] == m["export_id"]

    # 大写形式同样通过
    ok_upper = client.post(
        f"/api/v1/exports/{m['export_id']}/verify",
        json={"content_sha256": digest.upper()},
    )
    assert ok_upper.json()["valid"] is True

    bad = client.post(
        f"/api/v1/exports/{m['export_id']}/verify",
        json={"content_sha256": "0" * 64},
    )
    assert bad.status_code == 200
    assert bad.json()["valid"] is False
    assert bad.json()["expected_sha256"] == digest


def test_unknown_task_and_device_errors(client):
    assert client.get("/api/v1/exports/nope/manifest").status_code == 404
    assert client.get("/api/v1/exports/nope/pages").status_code == 404
    assert (
        client.post(
            "/api/v1/exports/nope/verify", json={"content_sha256": "a" * 64}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/v1/exports", json={"device_id": "ghost"}).status_code
        == 404
    )


def test_invalid_cursors(client):
    ingest_range(client, "exp-h", 1, 3)
    m = client.post(
        "/api/v1/exports", json={"device_id": "exp-h", "page_size": 2}
    ).json()
    eid = m["export_id"]

    # 损坏游标
    r = client.get(f"/api/v1/exports/{eid}/pages", params={"cursor": "@@@bad"})
    assert r.status_code == 400

    # 伪造的 base64 JSON 但字段缺失
    fake = base64.urlsafe_b64encode(b'{"x":1}').decode().rstrip("=")
    r = client.get(f"/api/v1/exports/{eid}/pages", params={"cursor": fake})
    assert r.status_code == 400

    # 偏移越界：服务端从不签发 offset==package_count 的游标
    over = export_mod._encode_cursor(eid, 3, 2, m["content_sha256"][:12])
    r = client.get(f"/api/v1/exports/{eid}/pages", params={"cursor": over})
    assert r.status_code == 400
    assert "越界" in r.json()["detail"]

    # 负偏移
    neg = export_mod._encode_cursor(eid, -1, 2, m["content_sha256"][:12])
    assert client.get(
        f"/api/v1/exports/{eid}/pages", params={"cursor": neg}
    ).status_code == 400

    # 摘要前缀被篡改
    tampered = export_mod._encode_cursor(eid, 0, 2, "deadbeefdead")
    assert client.get(
        f"/api/v1/exports/{eid}/pages", params={"cursor": tampered}
    ).status_code == 400

    # 游标属于另一个任务
    other = client.post("/api/v1/exports", json={"device_id": "exp-h"}).json()
    cross = other["next_cursor"]
    r = client.get(f"/api/v1/exports/{eid}/pages", params={"cursor": cross})
    assert r.status_code == 400
    assert "不属于" in r.json()["detail"]


def test_invalid_range_and_page_size(client):
    ingest_range(client, "exp-i", 1, 3)
    r = client.post(
        "/api/v1/exports",
        json={"device_id": "exp-i", "from_seq": 5, "to_seq": 2},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/exports", json={"device_id": "exp-i", "page_size": 0}
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/exports", json={"device_id": "exp-i", "page_size": 1001}
    )
    assert r.status_code == 422


class _StaticCursor:
    """把预取的行伪装成仅支持 fetchall 的游标（用于批计数代理连接）。"""

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _BatchCountingConn:
    """委托真实连接，仅对 committed 分批 SELECT 预取并记录每批行数。"""

    def __init__(self, conn, batch_sizes):
        self._conn = conn
        self._batch_sizes = batch_sizes

    def execute(self, sql, params=()):
        if " FROM committed " in sql and " LIMIT ?" in sql:
            rows = self._conn.execute(sql, params).fetchall()
            self._batch_sizes.append(len(rows))
            return _StaticCursor(rows)
        return self._conn.execute(sql, params)

    def executemany(self, sql, params):
        return self._conn.executemany(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_snapshot_streamed_in_seq_batches_unbounded(client, monkeypatch):
    """无界范围跨多批 keyset 流式复制：结果与一次性装载完全一致。"""
    monkeypatch.setattr(export_mod, "SNAPSHOT_BATCH_SIZE", 10)
    ingest_range(client, "exp-batch", 1, 23)

    batch_sizes = []
    with db_mod.ingest_lock, db_mod.get_conn() as conn:
        task = export_mod.create_export(
            _BatchCountingConn(conn, batch_sizes), "exp-batch", None, None
        )
    eid = task.export_id

    # 23 行按批大小 10 推进：两批满批 + 一批短批，绝不一次 fetchall 全部行
    nonempty = [s for s in batch_sizes if s > 0]
    assert nonempty == [10, 10, 3]
    assert sum(batch_sizes) == 23

    assert task.package_count == 23
    assert task.first_seq == 1 and task.last_seq == 23
    assert task.complete is True

    data, *_ = download_all(client, eid)
    assert [p["seq"] for p in parse_ndjson(data)] == list(range(1, 24))
    assert hashlib.sha256(data).hexdigest() == task.content_sha256


def test_snapshot_streamed_in_seq_batches_bounded(client, monkeypatch):
    """有界子范围同样按批推进：批间以 seq keyset 续接，不漏不重。"""
    monkeypatch.setattr(export_mod, "SNAPSHOT_BATCH_SIZE", 4)
    ingest_range(client, "exp-batch2", 1, 30)

    batch_sizes = []
    with db_mod.ingest_lock, db_mod.get_conn() as conn:
        task = export_mod.create_export(
            _BatchCountingConn(conn, batch_sizes),
            "exp-batch2", 5, 28,
        )
    expect = list(range(5, 29))  # 24 行，恰好 6 个满批
    assert sum(batch_sizes) == len(expect)
    assert all(s <= 4 for s in batch_sizes)
    assert task.package_count == len(expect)
    assert task.first_seq == 5 and task.last_seq == 28

    data, *_ = download_all(client, task.export_id)
    assert [p["seq"] for p in parse_ndjson(data)] == expect
    assert hashlib.sha256(data).hexdigest() == task.content_sha256


def test_create_failure_mid_batch_rolls_back_entire_export(client, monkeypatch):
    """扫描复制途中失败：父表占位行与已写快照行随事务整体回滚。"""
    ingest_range(client, "exp-rollback", 1, 20)

    real_canonical = export_mod.canonical_line
    calls = {"n": 0}

    def boom_after_ten(payload_json):
        calls["n"] += 1
        if calls["n"] == 11:  # 第二批（批大小调到 10）途中
            raise RuntimeError("simulated mid-batch failure")
        return real_canonical(payload_json)

    monkeypatch.setattr(export_mod, "SNAPSHOT_BATCH_SIZE", 10)
    monkeypatch.setattr(export_mod, "canonical_line", boom_after_ten)
    # 异常必须穿透 get_conn() 上下文才会触发事务回滚
    with pytest.raises(RuntimeError, match="simulated mid-batch failure"):
        with db_mod.ingest_lock, db_mod.get_conn() as conn:
            export_mod.create_export(conn, "exp-rollback", None, None)
    monkeypatch.undo()

    # 没有任何任务 / 快照行残留（失败的占位父行也应一并回滚）
    with db_mod.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM export_tasks WHERE device_id = ?",
            ("exp-rollback",),
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM export_items"
            " WHERE export_id IN ("
            "     SELECT export_id FROM export_tasks WHERE device_id = ?)",
            ("exp-rollback",),
        ).fetchone()["n"] == 0
        # 业务表不受影响
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM committed WHERE device_id = ?",
            ("exp-rollback",),
        ).fetchone()["n"] == 20

    # 设备可重新创建导出，且内容完整正确
    m = client.post("/api/v1/exports", json={"device_id": "exp-rollback"}).json()
    assert m["package_count"] == 20 and m["complete"] is True
    data, *_ = download_all(client, m["export_id"])
    assert hashlib.sha256(data).hexdigest() == m["content_sha256"]
