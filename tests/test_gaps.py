"""缺口查询分页测试：连续缺口区间计算、深翻页不展开缺失序号全集。"""

import pytest
from fastapi.testclient import TestClient

from app.gaps import gap_page
from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(device, seq, key):
    return {
        "device_id": device,
        "seq": seq,
        "idempotency_key": key,
        "measurements": [{"metric": "temperature", "value": 1.0, "unit": "C"}],
    }


def test_gap_page_unit_intervals():
    # 缓冲 4、5、9，next_expected=2 → 缺口区间 [2,3]、[6,8]，跨区间翻页
    assert gap_page(2, [4, 5, 9], 1000, 0) == (5, [2, 3, 6, 7, 8])
    assert gap_page(2, [4, 5, 9], 2, 0) == (5, [2, 3])
    assert gap_page(2, [4, 5, 9], 2, 2) == (5, [6, 7])
    assert gap_page(2, [4, 5, 9], 3, 1) == (5, [3, 6, 7])
    assert gap_page(2, [4, 5, 9], 10, 4) == (5, [8])
    assert gap_page(2, [4, 5, 9], 10, 5) == (5, [])
    assert gap_page(2, [], 10, 0) == (0, [])


def test_gap_page_unit_deep_offset_single_interval():
    # 单个巨大缺口区间：深翻页按区间长度算术定位，不逐个遍历被跳过的缺口
    count, page = gap_page(2, [100_000_000], 3, 99_999_995)
    assert count == 99_999_998
    assert page == [99_999_997, 99_999_998, 99_999_999]


def test_huge_gap_first_page(client):
    # 先提交 seq=1、再缓冲 seq=100000000：不再全量构造约 1 亿个缺失序号
    assert client.post("/api/v1/packages", json=pkg("dev-gap", 1, "gap-1")).json()["status"] == "committed"
    assert client.post("/api/v1/packages", json=pkg("dev-gap", 100_000_000, "gap-big")).json()["status"] == "buffered"
    r = client.get("/api/v1/devices/dev-gap/gaps")
    assert r.status_code == 200
    body = r.json()
    assert body["committed_upto"] == 1
    assert body["next_expected"] == 2
    assert body["buffered_seqs"] == [100_000_000]
    assert body["gap_count"] == 99_999_998
    assert body["limit"] == 1000 and body["offset"] == 0
    assert body["gaps"] == list(range(2, 1002))  # 默认每页 1000 条，升序


def test_huge_gap_deep_offset_page(client):
    client.post("/api/v1/packages", json=pkg("dev-deep", 1, "deep-1"))
    client.post("/api/v1/packages", json=pkg("dev-deep", 100_000_000, "deep-big"))
    # 翻到最后一页：offset 落在巨大缺口区间深处，按算术定位
    # 缺口为 2..99_999_999，第 99_999_995 个（0 起）即 99_999_997，只剩 3 条
    r = client.get(
        "/api/v1/devices/dev-deep/gaps",
        params={"limit": 10, "offset": 99_999_995},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["gap_count"] == 99_999_998
    assert body["limit"] == 10 and body["offset"] == 99_999_995
    assert body["gaps"] == [99_999_997, 99_999_998, 99_999_999]
    # offset 越过缺口总数：本页为空，gap_count 仍准确
    r = client.get(
        "/api/v1/devices/dev-deep/gaps",
        params={"limit": 10, "offset": 99_999_998},
    )
    assert r.status_code == 200
    assert r.json()["gaps"] == []
    assert r.json()["gap_count"] == 99_999_998


def test_multiple_intervals_pagination(client):
    # 提交 1；缓冲 4、5、9 → 缺口区间 [2,3] 与 [6,8]
    client.post("/api/v1/packages", json=pkg("dev-multi", 1, "m-1"))
    for seq in (4, 5, 9):
        client.post("/api/v1/packages", json=pkg("dev-multi", seq, f"m-{seq}"))
    r = client.get("/api/v1/devices/dev-multi/gaps").json()
    assert r["gap_count"] == 5
    assert r["buffered_seqs"] == [4, 5, 9]
    assert r["gaps"] == [2, 3, 6, 7, 8]  # 小范围保持原有升序
    # 跨区间翻页
    page = client.get(
        "/api/v1/devices/dev-multi/gaps", params={"limit": 3, "offset": 1}
    ).json()
    assert page["gaps"] == [3, 6, 7]
    page = client.get(
        "/api/v1/devices/dev-multi/gaps", params={"limit": 2, "offset": 4}
    ).json()
    assert page["gaps"] == [8]


def test_small_range_defaults_and_empty_buffer(client):
    for i in range(1, 4):
        client.post("/api/v1/packages", json=pkg("dev-small", i, f"s-{i}"))
    body = client.get("/api/v1/devices/dev-small/gaps").json()
    assert body["gap_count"] == 0
    assert body["gaps"] == [] and body["buffered_seqs"] == []
    # 小缺口：默认分页下结果与原行为一致
    client.post("/api/v1/packages", json=pkg("dev-small", 5, "s-5"))
    body = client.get("/api/v1/devices/dev-small/gaps").json()
    assert body["gap_count"] == 1
    assert body["gaps"] == [4]
    assert body["buffered_seqs"] == [5]


def test_gaps_pagination_validation(client):
    client.post("/api/v1/packages", json=pkg("dev-val", 1, "v-1"))
    assert client.get("/api/v1/devices/dev-val/gaps", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/devices/dev-val/gaps", params={"limit": 10001}).status_code == 422
    assert client.get("/api/v1/devices/dev-val/gaps", params={"offset": -1}).status_code == 422
    ok = client.get("/api/v1/devices/dev-val/gaps", params={"limit": 10000, "offset": 0})
    assert ok.status_code == 200


def test_gaps_unknown_device_404(client):
    assert client.get("/api/v1/devices/nope/gaps").status_code == 404
    assert (
        client.get("/api/v1/devices/nope/gaps", params={"limit": 10, "offset": 5}).status_code
        == 404
    )
