"""缺包作废测试：登记约束、推进排水、告警沿用、幂等重试、冲突错误、
作废登记查询、迟到作废序号被拒、导出完整性与真实包内容。"""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(device, seq, key, value=1.0, metric="temperature"):
    return {
        "device_id": device,
        "seq": seq,
        "idempotency_key": key,
        "measurements": [{"metric": metric, "value": value, "unit": "C"}],
    }


def void_body(device, lo, hi, tag):
    # 幂等键全局命名空间（会话内各测试共用一个库文件），用设备号隔离
    return {
        "from_seq": lo,
        "to_seq": hi,
        "idempotency_key": f"void:{device}:{tag}",
    }


def parse_ndjson(data):
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


def test_void_basic_advances_and_drains_buffered(client):
    d = "v-a"
    # 1 已提交；2、3 缺口；4、5 已缓冲
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 4, f"{d}-4", value=4.0))
    client.post("/api/v1/packages", json=pkg(d, 5, f"{d}-5", value=5.0))
    assert client.get(f"/api/v1/devices/{d}/gaps").json()["gaps"] == [2, 3]

    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 3, "void-2-3"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "voided"
    assert body["voided_seqs"] == [2, 3]
    # 作废推进前沿后立即排水：4、5 真实提交
    assert body["committed_seqs"] == [4, 5]
    assert body["committed_upto"] == 5

    gaps = client.get(f"/api/v1/devices/{d}/gaps").json()
    assert gaps["gaps"] == [] and gaps["buffered_seqs"] == []
    assert gaps["committed_upto"] == 5 and gaps["next_expected"] == 6

    # committed 只有真实包，不含作废序号
    seqs = client.get(f"/api/v1/devices/{d}/committed").json()["seqs"]
    assert seqs == [1, 4, 5]


def test_void_start_must_be_next_expected(client):
    d = "v-b"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))
    # 2 是 next_expected；从 3（缓冲）或 1（已提交）开始都不行
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 3, 3, "w1"))
    assert r.status_code == 409 and "next_expected" in r.json()["detail"]
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 1, 1, "w2"))
    assert r.status_code == 409 and "next_expected" in r.json()["detail"]
    # 无任何登记
    assert client.get(f"/api/v1/devices/{d}/voids").json()["count"] == 0


def test_void_segment_must_be_actual_gaps(client):
    d = "v-c"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))  # 3 缓冲
    client.post("/api/v1/packages", json=pkg(d, 5, f"{d}-5"))  # 4 缺口、5 缓冲
    # 段内含缓冲序号 3 -> 409
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 3, "w1"))
    assert r.status_code == 409 and "缓冲区" in r.json()["detail"]
    # 合法：作废 2，排水到 3；之后 4 才是 next_expected，5 仍缓冲
    assert client.post(
        f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "w2")
    ).json()["committed_seqs"] == [3]
    # 从 next_expected=4 起作废，但段内含缓冲序号 5 -> 409
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 4, 5, "w3"))
    assert r.status_code == 409 and "缓冲区" in r.json()["detail"]
    # 4 单独作废合法（段尾 4 不超过缓冲最大序号 5），随后排水提交 5
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 4, 4, "w4"))
    assert r.status_code == 200
    assert r.json()["voided_seqs"] == [4]
    assert r.json()["committed_seqs"] == [5]
    assert r.json()["committed_upto"] == 5
    # 此时缓冲已清空，再作废无缓冲支撑 -> 409
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 6, 6, "w5"))
    assert r.status_code == 409 and "没有缓冲包" in r.json()["detail"]


def test_void_end_must_not_exceed_buffer_max(client):
    d = "v-d"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 4, f"{d}-4"))
    # 无缓冲时作废 -> 409
    fresh = "v-d2"
    client.post("/api/v1/packages", json=pkg(fresh, 1, f"{fresh}-1"))
    r = client.post(f"/api/v1/devices/{fresh}/voids", json=void_body(fresh, 2, 2, "x"))
    assert r.status_code == 409 and "没有缓冲包" in r.json()["detail"]
    # 段尾超过缓冲区最大序号（max=4，要作到 6）-> 409
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 6, "x"))
    assert r.status_code == 409 and "缓冲区最大序号" in r.json()["detail"]
    # 段尾恰等于缓冲区最大序号允许
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 3, "ok"))
    assert r.status_code == 200
    assert r.json()["committed_seqs"] == [4]


def test_void_unknown_device_404_and_bad_range_422(client):
    assert client.post(
        "/api/v1/devices/ghost/voids",
        json={"from_seq": 1, "to_seq": 1, "idempotency_key": "void:ghost:k"},
    ).status_code == 404
    assert client.get("/api/v1/devices/ghost/voids").status_code == 404

    d = "v-e"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 2, f"{d}-2"))
    client.post("/api/v1/packages", json=pkg(d, 4, f"{d}-4"))  # 4 缓冲、3 缺口
    # from > to
    assert client.post(
        f"/api/v1/devices/{d}/voids", json=void_body(d, 3, 2, "badrange")
    ).status_code == 422
    # 缺幂等键
    body = void_body(d, 3, 3, "nokey")
    del body["idempotency_key"]
    assert client.post(
        f"/api/v1/devices/{d}/voids", json=body
    ).status_code == 422
    # 请求头幂等键可用：作废缺口 3，排水提交缓冲的 4
    r = client.post(
        f"/api/v1/devices/{d}/voids",
        json={"from_seq": 3, "to_seq": 3},
        headers={"Idempotency-Key": "void:v-e:hdr"},
    )
    assert r.status_code == 200
    assert r.json()["committed_seqs"] == [4]


def test_void_idempotent_retry_does_not_duplicate(client):
    d = "v-f"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3", value=9.0))
    body = void_body(d, 2, 2, "void-k")
    first = client.post(f"/api/v1/devices/{d}/voids", json=body)
    assert first.status_code == 200
    # 同键同体重试：原样返回，不重复推进
    retry = client.post(f"/api/v1/devices/{d}/voids", json=body)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    recs = client.get(f"/api/v1/devices/{d}/voids").json()
    assert recs["count"] == 1
    assert recs["voided_count"] == 1
    assert recs["records"][0]["voided_seqs"] == [2]
    # 排水只发生一次：committed 仍为 [1, 3]，无重复行
    assert client.get(f"/api/v1/devices/{d}/committed").json()["seqs"] == [1, 3]

    # 同键不同载荷 -> 409，且不产生新登记
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 3, "void-k"))
    assert r.status_code == 409
    assert client.get(f"/api/v1/devices/{d}/voids").json()["count"] == 1


def test_conflicting_void_after_advance_returns_conflict(client):
    d = "v-g"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))
    # 第一次作废 [2,2]，排水提交 3；前沿=3，缓冲区空
    r1 = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "k1"))
    assert r1.json()["committed_upto"] == 3
    assert r1.json()["committed_seqs"] == [3]
    # 无幂等键保护的"重复请求"：2 已越过前沿（next_expected 已变 4）-> 409
    r2 = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "k2"))
    assert r2.status_code == 409 and "next_expected" in r2.json()["detail"]
    # 缓冲已空：继续作废 4 也被拒绝（段尾不得超过缓冲区最大序号）
    r3 = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 4, 4, "k3"))
    assert r3.status_code == 409 and "没有缓冲包" in r3.json()["detail"]
    # 段非法
    r4 = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 5, 4, "k4"))
    assert r4.status_code == 422
    assert client.get(f"/api/v1/devices/{d}/voids").json()["count"] == 1


def test_void_multiple_segments_then_gap_close_by_ingest(client):
    d = "v-h"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))
    client.post("/api/v1/packages", json=pkg(d, 6, f"{d}-6"))
    # 作废 2 -> 排水提交 3；前沿=3，缓冲 [6]
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "v1"))
    assert r.json()["committed_seqs"] == [3]
    # 4、5 仍缺口，6 缓冲：作废 [4,5] -> 排水提交 6
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 4, 5, "v2"))
    assert r.json()["voided_seqs"] == [4, 5]
    assert r.json()["committed_seqs"] == [6]
    assert r.json()["committed_upto"] == 6
    recs = client.get(f"/api/v1/devices/{d}/voids").json()
    assert recs["voided_count"] == 3 and recs["count"] == 2


def test_late_arrival_of_voided_seq_is_duplicate(client):
    d = "v-i"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))
    client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "v"))
    # 作废序号 2 的原包迟到：already_voided，不会复活
    r = client.post("/api/v1/packages", json=pkg(d, 2, f"{d}-2-late"))
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert r.json()["duplicate_reason"] == "already_voided"
    assert client.get(f"/api/v1/devices/{d}/committed").json()["seqs"] == [1, 3]
    dups = client.get(
        f"/api/v1/devices/{d}/duplicates", params={"reason": "already_voided"}
    ).json()
    assert dups["count"] == 1 and dups["records"][0]["seq"] == 2


def test_void_drain_runs_alerts_on_real_packages_only(client):
    d = "v-j"
    # 规则：温度 > 30 即触发，<= 30 即恢复
    rr = client.post(
        "/api/v1/rules",
        json={"device_id": d, "metric": "temperature", "upper_limit": 30},
    )
    assert rr.status_code == 201
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1", value=20.0))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3", value=33.0))
    # 作废 2（无测量值）并排水提交 3：应产生一条 seq=3 的触发事件，
    # 绝不能把作废序号 2 伪装成测量包
    client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "v"))
    events = client.get(f"/api/v1/devices/{d}/alerts/events").json()
    assert events["count"] == 1
    assert events["events"][0]["seq"] == 3
    assert events["events"][0]["event_type"] == "triggered"

    # 作废后缓冲包继续按序评估：4 回限内 -> 恢复事件 seq=4
    client.post("/api/v1/packages", json=pkg(d, 4, f"{d}-4", value=25.0))
    # 4 在作废排水时未缓冲，是后来摄入的，直接排水提交（next_expected=4）
    events = client.get(f"/api/v1/devices/{d}/alerts/events").json()
    assert [e["seq"] for e in events["events"]] == [3, 4]
    assert events["events"][1]["event_type"] == "recovered"


def test_void_records_list_pagination(client):
    d = "v-k"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    # 构造 3 段作废：1 提交后缓冲 3 -> 作废2排水3；缓冲5 -> 作废4；缓冲7 -> 作废6
    for s, gap in ((3, 2), (5, 4), (7, 6)):
        client.post("/api/v1/packages", json=pkg(d, s, f"{d}-{s}"))
        client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, gap, gap, f"v{gap}"))
    r = client.get(f"/api/v1/devices/{d}/voids", params={"limit": 2, "offset": 0})
    body = r.json()
    assert body["count"] == 3 and body["voided_count"] == 3
    assert [x["from_seq"] for x in body["records"]] == [2, 4]
    assert body["committed_upto"] == 7 and body["next_expected"] == 8
    r = client.get(f"/api/v1/devices/{d}/voids", params={"limit": 2, "offset": 2})
    assert [x["from_seq"] for x in r.json()["records"]] == [6]


def test_devices_list_includes_voided_count(client):
    d = "v-l"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 4, f"{d}-4"))
    client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 3, "v"))
    devs = client.get("/api/v1/devices").json()
    me = [x for x in devs if x["device_id"] == d][0]
    assert me["voided_count"] == 2
    assert me["committed_count"] == 2  # 只有 1、4
    assert me["committed_upto"] == 4 and me["next_expected"] == 5


def test_export_range_touching_void_is_incomplete_and_excludes_voids(client):
    d = "v-m"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1", value=1.0))
    client.post("/api/v1/packages", json=pkg(d, 2, f"{d}-2", value=2.0))
    client.post("/api/v1/packages", json=pkg(d, 4, f"{d}-4", value=4.0))
    client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 3, 3, "v"))
    # 全量导出：含作废序号 3 的范围 -> complete=false，但内容只有真实包
    m = client.post("/api/v1/exports", json={"device_id": d}).json()
    assert m["package_count"] == 3
    assert m["first_seq"] == 1 and m["last_seq"] == 4
    assert m["complete"] is False

    # 下载内容：仅真实已提交包，绝不包含伪造的 seq=3
    eid = m["export_id"]
    chunks = []
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        page = client.get(f"/api/v1/exports/{eid}/pages", params=params)
        chunks.append(page.content)
        cursor = page.headers.get("x-next-cursor")
        if not cursor:
            break
    lines = parse_ndjson(b"".join(chunks))
    assert [p["seq"] for p in lines] == [1, 2, 4]

    # 范围只覆盖作废之前的完整真实区间 -> complete=true
    m2 = client.post(
        "/api/v1/exports",
        json={"device_id": d, "from_seq": 1, "to_seq": 2},
    ).json()
    assert m2["complete"] is True and m2["package_count"] == 2

    # 范围与作废段相交（3..4）-> complete=false；只包含真实包 4
    m3 = client.post(
        "/api/v1/exports",
        json={"device_id": d, "from_seq": 3, "to_seq": 4},
    ).json()
    assert m3["complete"] is False
    assert m3["first_seq"] == 4 and m3["package_count"] == 1


def test_export_complete_when_void_outside_range(client):
    d = "v-n"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))
    client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "v"))
    # 作废之前的 [1,1] 完整；作废之后单独取 [3,3] 也完整
    assert client.post(
        "/api/v1/exports", json={"device_id": d, "from_seq": 1, "to_seq": 1}
    ).json()["complete"] is True
    assert client.post(
        "/api/v1/exports", json={"device_id": d, "from_seq": 3, "to_seq": 3}
    ).json()["complete"] is True
    # 跨作废段的全量 -> 不完整
    assert client.post("/api/v1/exports", json={"device_id": d}).json()["complete"] is False


def test_void_then_more_void_chained_with_buffered_run(client):
    d = "v-o"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    for s in range(4, 8):  # 4,5,6,7 缓冲
        client.post("/api/v1/packages", json=pkg(d, s, f"{d}-{s}"))
    # 作废 [2,3]：排水 4..7 全部提交
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 3, "v"))
    assert r.status_code == 200
    assert r.json()["committed_seqs"] == [4, 5, 6, 7]
    assert r.json()["committed_upto"] == 7
    assert client.get(f"/api/v1/devices/{d}/gaps").json()["gaps"] == []
    # 此时再作废无缓冲支撑 -> 409
    assert client.post(
        f"/api/v1/devices/{d}/voids", json=void_body(d, 8, 8, "v2")
    ).status_code == 409


def test_void_digest_of_export_contains_only_real_packages(client):
    d = "v-p"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))
    client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "v"))
    m = client.post("/api/v1/exports", json={"device_id": d}).json()
    eid = m["export_id"]
    page = client.get(f"/api/v1/exports/{eid}/pages")
    assert hashlib.sha256(page.content).hexdigest() == m["content_sha256"]
    assert [p["seq"] for p in parse_ndjson(page.content)] == [1, 3]


def test_void_key_namespace_independent_from_ingest(client):
    # 作废幂等键与摄入幂等键分表存放：同一字符串在两侧互不影响
    d = "v-q"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))
    same_key = "shared-key-name"
    r = client.post(
        f"/api/v1/devices/{d}/voids",
        json={"from_seq": 2, "to_seq": 2, "idempotency_key": same_key},
    )
    assert r.status_code == 200
    # 摄入侧仍可用同名键发送新包（next_expected=4，已无缓冲 -> 缓冲态）
    r = client.post("/api/v1/packages", json=pkg(d, 4, same_key))
    assert r.status_code == 200
    assert r.json()["status"] == "committed"
    assert r.json()["committed_seqs"] == [4]


def test_failed_conflict_request_is_not_stored_and_retryable(client):
    d = "v-r"
    client.post("/api/v1/packages", json=pkg(d, 1, f"{d}-1"))
    client.post("/api/v1/packages", json=pkg(d, 3, f"{d}-3"))
    # 起点不是 next_expected 的冲突请求（段尾越过缓冲边界）：409，不落幂等记录
    bad = {"from_seq": 2, "to_seq": 5, "idempotency_key": "void:v-r:first-try"}
    r = client.post(f"/api/v1/devices/{d}/voids", json=bad)
    assert r.status_code == 409
    # 同一键随后以正确载荷重试：正常生效（失败未占用该键）
    r = client.post(f"/api/v1/devices/{d}/voids", json=void_body(d, 2, 2, "first-try"))
    assert r.status_code == 200
    assert r.json()["voided_seqs"] == [2]
    assert r.json()["committed_seqs"] == [3]
