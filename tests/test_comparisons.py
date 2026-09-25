"""GET /api/v1/comparisons 双设备同指标并行测量比对的测试。"""

import itertools

from fastapi.testclient import TestClient
import pytest

from app.main import app


# 数据库文件在测试进程内共享，逻辑设备名 ref/tgt 按用例加唯一后缀隔离
_suffix = {"v": 0}
_counter = itertools.count()


@pytest.fixture(autouse=True)
def _unique_device_suffix():
    _suffix["v"] = next(_counter)


def _dev(name):
    return f"{name}-{_suffix['v']}" if name in ("ref", "tgt") else name


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(device, seq, key, measurements, sent_at=None):
    device = _dev(device)
    p = {
        "device_id": device,
        "seq": seq,
        "idempotency_key": f"{device}-{key}",
        "measurements": measurements,
    }
    if sent_at is not None:
        p["sent_at"] = sent_at
    return p


def m(value, unit="C", ts=None, metric="temperature"):
    d = {"metric": metric, "value": value}
    if unit is not None:
        d["unit"] = unit
    if ts is not None:
        d["ts"] = ts
    return d


BASE = {
    "from": "2026-09-23T08:00:00Z",
    "to": "2026-09-23T09:00:00Z",
}


def compare(client, ref="ref", target="tgt", metric="temperature", **params):
    params.setdefault("metric", metric)
    params.setdefault("from", BASE["from"])
    params.setdefault("to", BASE["to"])
    params.setdefault("max_time_delta_seconds", 5)
    params.setdefault("allowed_value_diff", 0.5)
    return client.get("/api/v1/comparisons", params={
        "reference_device": _dev(ref), "target_device": _dev(target), **params
    })


# ---------------------------------------------------------------------------
# 基础配对
# ---------------------------------------------------------------------------

def test_basic_pairs_with_ts_time_and_value_diff(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(20.0, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(20.3, ts="2026-09-23T08:00:12Z")]))

    body = compare(client).json()
    assert body["reference_count"] == 1 and body["target_count"] == 1
    assert body["paired_count"] == 1
    assert body["reference_unpaired_count"] == 0
    assert body["target_unpaired_count"] == 0
    assert body["exceeded_count"] == 0
    pair = body["pairs"][0]
    assert pair["pair_time"] == "2026-09-23T08:00:10Z"
    assert pair["time_delta_seconds"] == pytest.approx(2.0)
    assert pair["abs_value_diff"] == pytest.approx(0.3)
    assert pair["exceeded"] is False
    assert pair["reference"]["seq"] == 1 and pair["reference"]["position"] == 0
    assert pair["target"]["seq"] == 1 and pair["target"]["position"] == 0
    assert pair["reference"]["time_source"] == "ts"


def test_exceeded_is_strict_greater_than_allowed_diff(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(20.0, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(20.5, ts="2026-09-23T08:00:10Z")]))
    # 绝对差恰好等于允许偏差：不超差
    body = compare(client, allowed_value_diff=0.5).json()
    assert body["pairs"][0]["exceeded"] is False
    assert body["exceeded_count"] == 0
    # 超出 0.5：超差
    client.post("/api/v1/packages", json=pkg(
        "ref", 2, "2", [m(20.0, ts="2026-09-23T08:10:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 2, "2", [m(20.51, ts="2026-09-23T08:10:10Z")]))
    body = compare(client, allowed_value_diff=0.5).json()
    assert body["exceeded_count"] == 1
    assert sorted(p["exceeded"] for p in body["pairs"]) == [False, True]


def test_unpaired_counts(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(20.0, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "ref", 2, "2", [m(21.0, ts="2026-09-23T08:01:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(20.1, ts="2026-09-23T08:00:11Z")]))
    body = compare(client).json()
    assert body["paired_count"] == 1
    assert body["reference_unpaired_count"] == 1
    assert body["target_unpaired_count"] == 0


# ---------------------------------------------------------------------------
# 有效时间：ts 优先、sent_at 回退、无时间忽略、排序
# ---------------------------------------------------------------------------

def test_falls_back_to_package_sent_at(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(10.0)], sent_at="2026-09-23T08:00:00Z"))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(10.2)], sent_at="2026-09-23T08:00:03Z"))
    body = compare(client).json()
    assert body["paired_count"] == 1
    p = body["pairs"][0]
    assert p["reference"]["time_source"] == "sent_at"
    assert p["target"]["time_source"] == "sent_at"
    assert p["pair_time"] == "2026-09-23T08:00:00Z"


def test_ignores_measurements_without_ts_or_sent_at(client):
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        m(10.0, ts="2026-09-23T08:00:10Z"),
        m(99.0, ts=None),  # 无 ts，包也无 sent_at：忽略
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        m(99.0, ts=None),
        m(10.1, ts="2026-09-23T08:00:11Z"),
    ]))
    body = compare(client).json()
    assert body["reference_count"] == 1
    assert body["target_count"] == 1
    assert body["pairs"][0]["reference"]["value"] == 10.0


def test_ts_preferred_over_sent_at(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(10.0, ts="2026-09-23T08:05:00Z")],
        sent_at="2026-09-23T08:00:00Z"))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(10.0, ts="2026-09-23T08:05:02Z")],
        sent_at="2026-09-23T08:00:05Z"))
    # 按 ts 相差 2s；3s 上限可配，若误用 sent_at（相差 5s）则不可配
    body = compare(client, max_time_delta_seconds=3).json()
    assert body["paired_count"] == 1
    assert body["pairs"][0]["time_delta_seconds"] == pytest.approx(2.0)


def test_sort_same_time_by_seq_then_position(client):
    # ref 同时间两点分属两个已提交包（seq 2、3），tgt 同时间两点在同包
    # 按位置区分；配对应体现同时间"先按包序号、再按包内位置"的排序。
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(9.0, ts="2026-09-23T07:59:00Z")]))
    client.post("/api/v1/packages", json=pkg(
        "ref", 2, "2", [m(30.0, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg("ref", 3, "3", [
        m(10.0, ts="2026-09-23T08:00:10Z"),
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        m(10.1, ts="2026-09-23T08:00:10Z"),
        m(30.1, ts="2026-09-23T08:00:10Z"),
    ]))
    body = compare(client, max_time_delta_seconds=0).json()
    assert body["paired_count"] == 2
    first, second = body["pairs"]
    # 同时间按包序号升序：ref seq 2 配 tgt position 0；seq 3 配 position 1
    assert (first["reference"]["seq"], first["target"]["position"]) == (2, 0)
    assert (second["reference"]["seq"], second["target"]["position"]) == (3, 1)


def test_position_is_index_in_full_measurements_list(client):
    # 包内混有其他指标，temperature 在原始 measurements 中的下标分别是 1 和 2
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        {"metric": "ph", "value": 7.0},
        m(20.0, ts="2026-09-23T08:00:10Z"),
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        {"metric": "ph", "value": 7.1},
        {"metric": "humidity", "value": 55.0},
        m(20.1, ts="2026-09-23T08:00:11Z"),
    ]))
    body = compare(client).json()
    assert body["pairs"][0]["reference"]["position"] == 1
    assert body["pairs"][0]["target"]["position"] == 2


# ---------------------------------------------------------------------------
# 时间范围过滤与最大时间偏差
# ---------------------------------------------------------------------------

def test_half_open_utc_time_window(client):
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        m(1.0, ts="2026-09-23T08:00:00Z"),  # == from：含
        m(2.0, ts="2026-09-23T08:09:00Z"),  # 内部
        m(3.0, ts="2026-09-23T09:00:00Z"),  # == to：不含
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        m(1.0, ts="2026-09-23T08:00:01Z"),
    ]))
    body = compare(client).json()
    assert body["reference_count"] == 2


def test_max_time_delta_boundary_inclusive(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(10.0, ts="2026-09-23T08:00:00Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(10.0, ts="2026-09-23T08:00:05Z")]))
    assert compare(client, max_time_delta_seconds=5).json()["paired_count"] == 1
    assert compare(client, max_time_delta_seconds=4).json()["paired_count"] == 0


def test_naive_datetimes_treated_as_utc(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(10.0, ts="2026-09-23T08:00:00")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(10.0, ts="2026-09-23T08:00:02Z")]))
    body = compare(client, **{"from": "2026-09-23T08:00:00",
                              "to": "2026-09-23T09:00:00"}).json()
    assert body["paired_count"] == 1


# ---------------------------------------------------------------------------
# 单位
# ---------------------------------------------------------------------------

def test_pair_requires_same_unit(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(20.0, unit="C", ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(20.0, unit="K", ts="2026-09-23T08:00:10Z")]))
    body = compare(client).json()
    assert body["paired_count"] == 0
    assert body["reference_unpaired_count"] == 1
    assert body["target_unpaired_count"] == 1


def test_both_units_missing_is_same_unit(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(20.0, unit=None, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(20.0, unit=None, ts="2026-09-23T08:00:10Z")]))
    assert compare(client).json()["paired_count"] == 1


def test_one_side_missing_unit_does_not_pair(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(20.0, unit=None, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(20.0, unit="C", ts="2026-09-23T08:00:10Z")]))
    assert compare(client).json()["paired_count"] == 0


# ---------------------------------------------------------------------------
# 匹配优化目标：配对数 > 时间差总和 > 字典序；顺序保持 / 一对一
# ---------------------------------------------------------------------------

def test_matching_maximizes_pair_count_when_greedy_nearest_fails(client):
    # A=[0s, 5s]，B=[4s, 9s]，容差 4s：
    #   边：A0-B0(d4)；A1-B0(d1)、A1-B1(d4)
    # 按目标序"就近贪心"会把 B0 配给 A1（d1），随后 B1 无 A 可配 -> 仅 1 对；
    # 最优（且保序）为 A0-B0 + A1-B1，共 2 对。
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        m(10.0, ts="2026-09-23T08:00:00Z"),
        m(11.0, ts="2026-09-23T08:00:05Z"),
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        m(20.0, ts="2026-09-23T08:00:04Z"),
        m(21.0, ts="2026-09-23T08:00:09Z"),
    ]))
    body = compare(client, max_time_delta_seconds=4, allowed_value_diff=100).json()
    assert body["paired_count"] == 2
    p0, p1 = body["pairs"]
    assert p0["reference"]["value"] == 10.0 and p0["target"]["value"] == 20.0
    assert p1["reference"]["value"] == 11.0 and p1["target"]["value"] == 21.0


def test_matching_minimizes_total_time_delta_after_count(client):
    # A=[0s, 4s]，B=[4s]，容差 4s：最多 1 对，可选 A0-B0(d4) 或 A1-B0(d0)；
    # 配对数相同，取时间差总和最小的 A1-B0（d0），A0 未配对。
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        m(10.0, ts="2026-09-23T08:00:00Z"),
        m(10.0, ts="2026-09-23T08:00:04Z"),
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        m(10.0, ts="2026-09-23T08:00:04Z"),
    ]))
    body = compare(client, max_time_delta_seconds=4).json()
    assert body["paired_count"] == 1
    pair = body["pairs"][0]
    assert pair["reference"]["time"].endswith("08:00:04Z")
    assert pair["time_delta_seconds"] == 0.0
    assert body["reference_unpaired_count"] == 1


def test_matching_prefers_min_cost_multicase(client):
    # A=[0s, 10s]，B=[0s, 4s, 10s]，容差 10s：最大 2 对，
    # 最小总差方案为 (A0,B0)(d0)+(A1,B2)(d0)，中间 B1 未配对。
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        m(100.0, ts="2026-09-23T08:00:00Z"),
        m(110.0, ts="2026-09-23T08:00:10Z"),
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        m(200.0, ts="2026-09-23T08:00:00Z"),
        m(204.0, ts="2026-09-23T08:00:04Z"),
        m(210.0, ts="2026-09-23T08:00:10Z"),
    ]))
    body = compare(client, max_time_delta_seconds=10, allowed_value_diff=1000).json()
    assert body["paired_count"] == 2
    assert body["target_unpaired_count"] == 1
    p0, p1 = body["pairs"]
    assert (p0["reference"]["value"], p0["target"]["value"]) == (100.0, 200.0)
    assert p0["time_delta_seconds"] == 0.0
    assert (p1["reference"]["value"], p1["target"]["value"]) == (110.0, 210.0)
    assert p1["time_delta_seconds"] == 0.0


def test_matching_lexicographic_tiebreak_symmetric(client):
    # 配对数与总差均相同的多方案平局：B 侧两个时刻各有两个同时间测量
    # （同时间按包内位置区分），容差 0s 时 A0(0s) 可配 B0/B1、A1(10s)
    # 可配 B2/B3，四个方案都是 2 对、总差 0；字典序最小为
    # (A0,B0)+(A1,B2)，即配对序号序列 (0,0)(1,2)。
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        m(100.0, ts="2026-09-23T08:00:00Z"),
        m(110.0, ts="2026-09-23T08:00:10Z"),
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        m(200.0, ts="2026-09-23T08:00:00Z"),
        m(201.0, ts="2026-09-23T08:00:00Z"),
        m(210.0, ts="2026-09-23T08:00:10Z"),
        m(211.0, ts="2026-09-23T08:00:10Z"),
    ]))
    body = compare(client, max_time_delta_seconds=0, allowed_value_diff=1000).json()
    assert body["paired_count"] == 2
    p0, p1 = body["pairs"]
    assert (p0["reference"]["position"], p0["target"]["position"]) == (0, 0)
    assert (p1["reference"]["position"], p1["target"]["position"]) == (1, 2)
    assert body["target_unpaired_count"] == 2
    assert [p["target"]["value"] for p in body["pairs"]] == [200.0, 210.0]


def test_pairs_returned_in_time_order(client):
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        m(10.0, ts="2026-09-23T08:00:30Z"),
        m(11.0, ts="2026-09-23T08:00:10Z"),
        m(12.0, ts="2026-09-23T08:00:20Z"),
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        m(20.0, ts="2026-09-23T08:00:31Z"),
        m(21.0, ts="2026-09-23T08:00:11Z"),
        m(22.0, ts="2026-09-23T08:00:21Z"),
    ]))
    body = compare(client).json()
    times = [p["pair_time"] for p in body["pairs"]]
    assert times == sorted(times)
    assert [p["reference"]["value"] for p in body["pairs"]] == [11.0, 12.0, 10.0]


# ---------------------------------------------------------------------------
# 数据源：仅已提交包
# ---------------------------------------------------------------------------

def test_only_committed_packages_participate(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(10.0, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(20.0, ts="2026-09-23T08:00:10Z")]))
    # tgt seq 3 先到 -> 缓冲不提交，不参与比对
    client.post("/api/v1/packages", json=pkg(
        "tgt", 3, "3", [m(22.0, ts="2026-09-23T08:00:20Z")]))
    body = compare(client).json()
    assert body["target_count"] == 1
    assert body["paired_count"] == 1
    # 补齐缺口后排水提交，随即参与比对
    client.post("/api/v1/packages", json=pkg(
        "tgt", 2, "2", [m(21.0, ts="2026-09-23T08:00:15Z")]))
    client.post("/api/v1/packages", json=pkg(
        "ref", 2, "2", [m(11.0, ts="2026-09-23T08:00:15Z")]))
    body = compare(client).json()
    assert body["reference_count"] == 2 and body["target_count"] == 3
    assert body["paired_count"] == 2


def test_metric_filter(client):
    client.post("/api/v1/packages", json=pkg("ref", 1, "1", [
        {"metric": "ph", "value": 7.0, "ts": "2026-09-23T08:00:10Z"},
        m(10.0, ts="2026-09-23T08:00:10Z"),
    ]))
    client.post("/api/v1/packages", json=pkg("tgt", 1, "1", [
        {"metric": "ph", "value": 7.1, "ts": "2026-09-23T08:00:10Z"},
    ]))
    body = compare(client, metric="ph").json()
    assert body["reference_count"] == 1 and body["target_count"] == 1
    assert body["pairs"][0]["reference"]["value"] == 7.0


# ---------------------------------------------------------------------------
# 200 条硬上限：拒绝而非截断
# ---------------------------------------------------------------------------

def test_side_limit_exactly_200_ok(client):
    # 无 ts 时回退包级 sent_at：200 条同时间，按包内位置排序
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(float(i)) for i in range(200)],
        sent_at="2026-09-23T08:00:00Z"))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(0.0, ts="2026-09-23T08:00:00Z")]))
    body = compare(client, allowed_value_diff=1000).json()
    assert body["reference_count"] == 200
    assert body["paired_count"] == 1
    # 同时间 (seq, position) 排序：position 0 最先，配 tgt 唯一点
    assert body["pairs"][0]["reference"]["position"] == 0


def test_side_limit_201_rejected_not_truncated(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(1.0) for _ in range(201)],
        sent_at="2026-09-23T08:00:00Z"))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(1.0, ts="2026-09-23T08:00:00Z")]))
    r = compare(client)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "201" in detail and "200" in detail
    assert "参考设备" in detail


def test_target_side_limit_201_rejected(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(1.0, ts="2026-09-23T08:00:00Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(1.0) for _ in range(201)],
        sent_at="2026-09-23T08:00:00Z"))
    r = compare(client)
    assert r.status_code == 422
    assert "201" in r.json()["detail"]
    assert "待检设备" in r.json()["detail"]


def test_limit_counts_only_metric_and_window(client):
    # 201 条同指标全部落在时间窗外：不计入上限
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(1.0) for _ in range(201)],
        sent_at="2026-09-23T07:00:00Z"))
    client.post("/api/v1/packages", json=pkg("ref", 2, "2", [
        m(9.0, ts="2026-09-23T08:30:00Z"),
        {"metric": "ph", "value": 7.0, "ts": "2026-09-23T08:30:00Z"},
    ]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(9.0, ts="2026-09-23T08:30:01Z")]))
    body = compare(client).json()
    assert body["reference_count"] == 1
    assert body["paired_count"] == 1


# ---------------------------------------------------------------------------
# 参数校验与错误
# ---------------------------------------------------------------------------

def test_unknown_devices_404(client):
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(1.0, ts="2026-09-23T08:00:10Z")]))
    r = compare(client, ref="ghost")
    assert r.status_code == 404
    assert "参考设备" in r.json()["detail"]

    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(1.0, ts="2026-09-23T08:00:10Z")]))
    r = compare(client, target="specter")
    assert r.status_code == 404
    assert "待检设备" in r.json()["detail"]


def test_same_device_rejected(client):
    client.post("/api/v1/packages", json=pkg(
        "solo", 1, "1", [m(1.0, ts="2026-09-23T08:00:10Z")]))
    r = compare(client, ref="solo", target="solo")
    assert r.status_code == 422


def test_invalid_time_range(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(1.0, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(1.0, ts="2026-09-23T08:00:10Z")]))
    r = compare(client, **{"from": BASE["to"], "to": BASE["from"]})
    assert r.status_code == 422


def test_negative_params_and_missing_params_rejected(client):
    base_params = {
        "reference_device": "ref", "target_device": "tgt",
        "metric": "temperature", **BASE,
        "max_time_delta_seconds": 5, "allowed_value_diff": 0.5,
    }
    assert client.get("/api/v1/comparisons", params={
        **base_params, "max_time_delta_seconds": -1,
    }).status_code == 422
    assert client.get("/api/v1/comparisons", params={
        **base_params, "allowed_value_diff": -0.1,
    }).status_code == 422
    for missing in ("reference_device", "target_device", "metric",
                    "from", "to", "max_time_delta_seconds", "allowed_value_diff"):
        params = {**base_params}
        del params[missing]
        assert client.get("/api/v1/comparisons", params=params).status_code == 422


def test_empty_sides_return_zero_pairs(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(1.0, ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(1.0, ts="2026-09-23T08:00:10Z")]))
    body = compare(client, metric="nonexistent").json()
    assert body["paired_count"] == 0
    assert body["reference_count"] == 0 and body["target_count"] == 0
    assert body["reference_unpaired_count"] == 0
    assert body["target_unpaired_count"] == 0
    assert body["pairs"] == []


def test_read_only_does_not_mutate_packages(client):
    client.post("/api/v1/packages", json=pkg(
        "ref", 1, "1", [m(20.0, unit="C", ts="2026-09-23T08:00:10Z")]))
    client.post("/api/v1/packages", json=pkg(
        "tgt", 1, "1", [m(68.5, unit="F", ts="2026-09-23T08:00:10Z")]))
    # 单位不同：不配对
    assert compare(client).json()["paired_count"] == 0
    # 重复执行结果一致（纯只读），原始读数与单位未被改写
    body1 = compare(client).json()
    body2 = compare(client).json()
    assert body1 == body2
    assert client.get(
        f"/api/v1/devices/{_dev('ref')}/committed"
    ).json()["seqs"] == [1]
    assert client.get(f"/api/v1/devices/{_dev('ref')}/gaps").json()["gaps"] == []
