"""GET /api/v1/devices/{id}/metrics/warmup-release 仪器预热稳定放行测试。"""

import itertools

from fastapi.testclient import TestClient
import pytest

from app.main import app


# 逻辑设备名统一加按用例唯一后缀隔离（数据库文件在测试进程内共享）
_suffix = {"v": 0}
_counter = itertools.count()


@pytest.fixture(autouse=True)
def _unique_device_suffix():
    _suffix["v"] = next(_counter)


def _dev(name="dev"):
    return f"warm-{name}-{_suffix['v']}"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(seq, key, measurements, device="dev", sent_at=None):
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
# 宽松默认：波动 1.0、持续 20s、最少 3 条、最大间隔 20s
DEFAULTS = {
    "max_fluctuation": 1.0,
    "min_duration_seconds": 20,
    "min_readings": 3,
    "max_gap_seconds": 20,
}


def release(client, **params):
    params = {"metric": "temperature", "unit": "C", **BASE, **DEFAULTS, **params}
    return client.get(
        f"/api/v1/devices/{_dev()}/metrics/warmup-release", params=params
    )


def stable_readings(values, start="2026-09-23T08:00:00Z", step=10, unit="C"):
    """每 10s 一条、从 start 起的读数包序列（各占一个包）。"""
    base_h = int(start[11:13])
    base_m = int(start[14:16])
    base_s = int(start[17:19])
    out = []
    for i, v in enumerate(values):
        total = base_h * 3600 + base_m * 60 + base_s + i * step
        h, rem = divmod(total, 3600)
        mi, s = divmod(rem, 60)
        ts = f"2026-09-23T{h:02d}:{mi:02d}:{s:02d}Z"
        out.append(pkg(i + 1, str(i + 1), [m(v, unit=unit, ts=ts)]))
    return out


# ---------------------------------------------------------------------------
# 基础放行
# ---------------------------------------------------------------------------

def test_basic_release_returns_earliest_segment(client):
    for p in stable_readings([20.0, 20.2, 20.1, 20.05]):
        client.post("/api/v1/packages", json=p)
    body = release(client).json()
    assert body["released"] is True
    assert body["not_released_reason"] is None
    seg = body["segment"]
    assert seg["start_time"] == "2026-09-23T08:00:00Z"
    assert seg["end_time"] == "2026-09-23T08:00:20Z"
    assert seg["reading_count"] == 3
    assert seg["duration_seconds"] == pytest.approx(20.0)
    assert seg["min_value"] == pytest.approx(20.0)
    assert seg["max_value"] == pytest.approx(20.2)
    assert seg["fluctuation"] == pytest.approx(0.2)
    assert seg["max_adjacent_gap_seconds"] == pytest.approx(10.0)
    assert [r["index"] for r in seg["readings"]] == [1, 2, 3]
    assert [(r["seq"], r["position"]) for r in seg["readings"]] == [
        (1, 0), (2, 0), (3, 0)
    ]
    assert {r["time_source"] for r in seg["readings"]} == {"ts"}


def test_segment_completes_at_earliest_time_not_returns_full_window(client):
    # 前 3 条即可稳定；随后出现剧烈波动也不影响已完成的最早放行结论
    for p in stable_readings([20.0, 20.1, 20.2, 99.0, 100.0]):
        client.post("/api/v1/packages", json=p)
    body = release(client).json()
    assert body["released"] is True
    assert body["segment"]["end_time"] == "2026-09-23T08:00:20Z"
    assert body["segment"]["reading_count"] == 3


def test_fluctuation_too_large_not_released(client):
    for p in stable_readings([20.0, 20.2, 21.5, 21.6]):
        client.post("/api/v1/packages", json=p)
    body = release(client, max_fluctuation=0.3).json()
    assert body["released"] is False
    assert body["segment"] is None
    assert "不予放行" in body["not_released_reason"]
    # 放宽波动后放行：最早完成于 t=20s，段 [20.0, 20.2, 21.5] 波动 1.5
    body = release(client, max_fluctuation=1.6).json()
    assert body["released"] is True
    assert body["segment"]["fluctuation"] == pytest.approx(1.5)
    assert body["segment"]["end_time"] == "2026-09-23T08:00:20Z"
    # 足够宽松时全段亦可稳定，但完成更晚，不会被优先返回
    body = release(client, max_fluctuation=1.6, min_readings=4).json()
    assert body["segment"]["fluctuation"] == pytest.approx(1.6)


def test_fluctuation_boundary_inclusive(client):
    # 最大 - 最小恰好等于 max_fluctuation：合格
    for p in stable_readings([20.0, 20.25, 19.75]):
        client.post("/api/v1/packages", json=p)
    assert release(client, max_fluctuation=0.5).json()["released"] is True
    assert release(client, max_fluctuation=0.499).json()["released"] is False


# ---------------------------------------------------------------------------
# 最短持续时长 / 最少读数
# ---------------------------------------------------------------------------

def test_min_duration_boundary_inclusive(client):
    for p in stable_readings([20.0, 20.1, 20.2]):  # 跨度 20s
        client.post("/api/v1/packages", json=p)
    assert release(client, min_duration_seconds=20).json()["released"] is True
    assert release(client, min_duration_seconds=20.1).json()["released"] is False


def test_min_readings_enforced(client):
    for p in stable_readings([20.0, 20.1, 20.2, 20.3]):
        client.post("/api/v1/packages", json=p)
    body = release(client, min_readings=4, min_duration_seconds=10).json()
    assert body["released"] is True
    assert body["segment"]["reading_count"] == 4
    body = release(client, min_readings=5, min_duration_seconds=10).json()
    assert body["released"] is False


def test_earliest_start_tie_break_uses_longest_segment(client):
    # 前 4 条全部稳定：min_readings=3、min_duration=20s 时，
    # 在 t=20s 完成的合格段有 [0,2]、[1,2]（20s、10s，仅前者满足时长）。
    # 最早起点是 [0,2]；另构造一个"最短起点方案会误选"的场景：
    # t=30s 处 [1,3] 也合格，但完成更晚，不应选。
    for p in stable_readings([20.0, 20.2, 20.1, 20.05, 20.15]):
        client.post("/api/v1/packages", json=p)
    body = release(client, min_duration_seconds=20, min_readings=3).json()
    seg = body["segment"]
    assert (seg["start_time"], seg["end_time"]) == (
        "2026-09-23T08:00:00Z",
        "2026-09-23T08:00:20Z",
    )


def test_same_completion_time_prefers_earliest_start(client):
    # t=0、20、20（同时间按 seq、position 排序）、30；min_readings=3、
    # min_duration=10s、max_gap=30s 时最早完成时间为 t=20：
    #   段 [1..2]（后两条同时间读数）时长 0、仅 2 条 -> 不合格；
    #   段 [0..2] 时长 20、3 条 -> 合格，起点 t=0 即"同一完成时间最早起点"。
    # 若错误地优先段 [1..3]（完成于 t=30）或从 t=20 起算，结论都会不同。
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(20.0, ts="2026-09-23T08:00:00Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(2, "2", [
        m(20.1, ts="2026-09-23T08:00:20Z"),
        m(20.05, ts="2026-09-23T08:00:20Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(3, "3", [
        m(20.2, ts="2026-09-23T08:00:30Z"),
    ]))
    body = release(client, min_duration_seconds=10, min_readings=3,
                   max_gap_seconds=30).json()
    seg = body["segment"]
    assert seg["start_time"] == "2026-09-23T08:00:00Z"
    assert seg["end_time"] == "2026-09-23T08:00:20Z"
    assert seg["reading_count"] == 3
    assert [(r["seq"], r["position"]) for r in seg["readings"]] == [
        (1, 0), (2, 0), (2, 1)
    ]


# ---------------------------------------------------------------------------
# 相邻采样间隔：断裂、边界、不跨断裂
# ---------------------------------------------------------------------------

def test_gap_break_prevents_segment_spanning(client):
    # t=0、10、40、50、60：10s 与 40s 之间间隔 30s > 20s，序列断裂；
    # 断裂前块仅 2 条、断裂后块仅 3 条，要求最少 4 条时均不成段，不放行。
    # （放宽间隔后断裂消失，全序列才可用。）
    times_vals = [
        ("2026-09-23T08:00:00Z", 20.0),
        ("2026-09-23T08:00:10Z", 20.1),
        ("2026-09-23T08:00:40Z", 20.0),
        ("2026-09-23T08:00:50Z", 20.1),
        ("2026-09-23T08:01:00Z", 20.0),
    ]
    for i, (ts, v) in enumerate(times_vals):
        client.post("/api/v1/packages", json=pkg(i + 1, str(i + 1), [m(v, ts=ts)]))
    body = release(client, min_readings=4).json()
    assert body["released"] is False
    # 放宽间隔到 30s（边界含）：断裂消失，最早在 t=50s 集齐 4 条、跨度 50s
    body = release(client, min_readings=4, max_gap_seconds=30).json()
    assert body["released"] is True
    seg = body["segment"]
    assert seg["start_time"] == "2026-09-23T08:00:00Z"
    assert seg["end_time"] == "2026-09-23T08:00:50Z"
    assert seg["reading_count"] == 4
    assert seg["max_adjacent_gap_seconds"] == pytest.approx(30.0)
    assert seg["duration_seconds"] == pytest.approx(50.0)


def test_segment_can_start_after_recovered_from_outlier(client):
    # t=0 离群 50.0；之后 t=10、20、30 稳定 -> 最早完成 t=30s
    for p in stable_readings([50.0, 20.0, 20.1, 20.2]):
        client.post("/api/v1/packages", json=p)
    body = release(client, max_fluctuation=0.5, min_duration_seconds=20).json()
    assert body["released"] is True
    seg = body["segment"]
    assert seg["start_time"] == "2026-09-23T08:00:10Z"
    assert seg["end_time"] == "2026-09-23T08:00:30Z"
    assert seg["reading_count"] == 3
    assert seg["fluctuation"] == pytest.approx(0.2)


def test_outlier_then_stable_window_slides(client):
    # 稳定段中途插入一个离群点，滑动窗口必须能越过它，从其后重新起段
    for p in stable_readings([20.0, 20.1, 80.0, 20.2, 20.0, 20.1]):
        client.post("/api/v1/packages", json=p)
    body = release(client, max_fluctuation=0.5, min_duration_seconds=20).json()
    assert body["released"] is True
    seg = body["segment"]
    assert seg["start_time"] == "2026-09-23T08:00:30Z"  # 离群点 t=20 之后
    assert seg["end_time"] == "2026-09-23T08:00:50Z"
    assert [r["value"] for r in seg["readings"]] == [20.2, 20.0, 20.1]


# ---------------------------------------------------------------------------
# 时间来源：ts 优先、sent_at 回退、无时间忽略
# ---------------------------------------------------------------------------

def test_sent_at_fallback_and_time_source(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0)], sent_at="2026-09-23T08:00:00Z"))
    client.post("/api/v1/packages", json=pkg(
        2, "2", [m(20.1)], sent_at="2026-09-23T08:00:10Z"))
    client.post("/api/v1/packages", json=pkg(
        3, "3", [m(20.2)], sent_at="2026-09-23T08:00:20Z"))
    body = release(client).json()
    assert body["released"] is True
    assert {r["time_source"] for r in body["segment"]["readings"]} == {"sent_at"}


def test_ts_preferred_over_sent_at(client):
    # ts 间隔 10s（合格），sent_at 间隔 100s（不合格）：必须按 ts 判定
    specs = [
        # (seq, ts, sent_at, value)
        (1, "2026-09-23T08:00:00Z", "2026-09-23T07:00:00Z", 20.0),
        (2, "2026-09-23T08:00:10Z", "2026-09-23T07:01:40Z", 20.1),
        (3, "2026-09-23T08:00:20Z", "2026-09-23T07:03:20Z", 20.2),
    ]
    for seq, ts, sent, v in specs:
        client.post("/api/v1/packages", json=pkg(
            seq, str(seq), [m(v, ts=ts)], sent_at=sent))
    body = release(client).json()
    assert body["candidate_count"] == 3
    assert body["released"] is True
    assert body["segment"]["start_time"] == "2026-09-23T08:00:00Z"


def test_no_time_readings_excluded_and_reported(client):
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(20.0, ts=None),
        m(20.1, ts=None),
        m(20.2, ts=None),
    ]))  # 包无 sent_at：三条同指标读数全部无有效时间
    body = release(client).json()
    assert body["released"] is False
    assert body["candidate_count"] == 0
    assert body["excluded_no_time_count"] == 3
    assert body["excluded_unit_mismatch_count"] == 0


def test_half_open_time_window(client):
    for p in stable_readings([20.0, 20.1, 20.2, 20.3]):
        client.post("/api/v1/packages", json=p)
    body = release(
        client,
        **{"from": "2026-09-23T08:00:10Z", "to": "2026-09-23T08:00:30Z"},
        min_duration_seconds=10, min_readings=2,
    ).json()
    # 含 t=10、t=20；不含 t=0 与 t=30
    assert body["candidate_count"] == 2
    assert body["released"] is True
    seg = body["segment"]
    assert seg["start_time"] == "2026-09-23T08:00:10Z"
    assert seg["end_time"] == "2026-09-23T08:00:20Z"


def test_naive_datetimes_treated_as_utc(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T08:00:00")]))
    client.post("/api/v1/packages", json=pkg(
        2, "2", [m(20.1, ts="2026-09-23T08:00:10")]))
    client.post("/api/v1/packages", json=pkg(
        3, "3", [m(20.2, ts="2026-09-23T08:00:20")]))
    body = release(
        client, **{"from": "2026-09-23T08:00:00", "to": "2026-09-23T09:00:00"}
    ).json()
    assert body["released"] is True
    assert body["segment"]["start_time"].endswith("Z")


# ---------------------------------------------------------------------------
# 单位
# ---------------------------------------------------------------------------

def test_unit_mismatch_excluded_and_reported(client):
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(20.0, unit="C", ts="2026-09-23T08:00:00Z"),
        m(20.1, unit="F", ts="2026-09-23T08:00:10Z"),
        m(20.2, unit=None, ts="2026-09-23T08:00:20Z"),
        m(20.0, unit="C", ts="2026-09-23T08:00:30Z"),
        m(20.1, unit="C", ts="2026-09-23T08:00:40Z"),
    ]))
    body = release(client, min_duration_seconds=10, min_readings=3).json()
    # 单位不符与缺单位的读数不参与：C 单位读数间隔为 30s、10s
    assert body["excluded_unit_mismatch_count"] == 2
    assert body["candidate_count"] == 3
    # C 读数在 t=0 与 t=30 之间间隔 30s > 20s 断裂；后两条跨度仅 10s
    assert body["released"] is False
    body = release(client, min_duration_seconds=10, min_readings=2,
                   max_gap_seconds=30).json()
    assert body["released"] is True
    # 最早完成于 t=30s：段 [t0, t30]（2 条 C 读数，跨度 30s；取最早起点）
    assert len(body["segment"]["readings"]) == 2
    assert [r["unit"] for r in body["segment"]["readings"]] == ["C", "C"]
    assert body["segment"]["start_time"] == "2026-09-23T08:00:00Z"
    assert body["segment"]["end_time"] == "2026-09-23T08:00:30Z"


def test_unit_filter_exact_string(client):
    for p in stable_readings([20.0, 20.1, 20.2], unit="degC"):
        client.post("/api/v1/packages", json=p)
    assert release(client, unit="C").json()["candidate_count"] == 0
    body = release(client, unit="degC").json()
    assert body["candidate_count"] == 3
    assert body["released"] is True


def test_mismatch_outside_time_window_not_counted(client):
    # 单位不符但在时间窗口外的读数不计入排除数
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(20.0, unit="F", ts="2026-09-23T07:59:59Z"),
        m(20.0, unit="C", ts="2026-09-23T08:00:00Z"),
        m(20.1, unit="C", ts="2026-09-23T08:00:10Z"),
        m(20.2, unit="C", ts="2026-09-23T08:00:20Z"),
    ]))
    body = release(client).json()
    assert body["excluded_unit_mismatch_count"] == 0
    assert body["candidate_count"] == 3
    assert body["released"] is True


# ---------------------------------------------------------------------------
# 排序与包内位置
# ---------------------------------------------------------------------------

def test_sort_by_time_then_seq_then_position(client):
    # 同 ts 的 3 条读数：跨包 seq 2、同包 seq 3 内两个位置
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(9.0, ts="2026-09-23T07:59:00Z"),  # 窗外
    ]))
    client.post("/api/v1/packages", json=pkg(2, "2", [
        m(20.0, ts="2026-09-23T08:00:00Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(3, "3", [
        {"metric": "ph", "value": 7.0},  # 其他指标，占用位置 0
        m(20.1, ts="2026-09-23T08:00:00Z"),  # 位置 1
        m(20.2, ts="2026-09-23T08:00:00Z"),  # 位置 2
    ]))
    client.post("/api/v1/packages", json=pkg(4, "4", [
        m(20.1, ts="2026-09-23T08:00:10Z"),
    ]))
    body = release(client, min_duration_seconds=0.001, min_readings=3,
                   max_gap_seconds=20).json()
    assert body["released"] is True
    seg = body["segment"]
    # t=0 的三条读数时长 0 尚不足 0.001s；t=10 到达后段含全部 4 条，
    # 同时间先 seq 2 position 0，再 seq 3 position 1、2
    assert [(r["seq"], r["position"]) for r in seg["readings"]] == [
        (2, 0), (3, 1), (3, 2), (4, 0)
    ]
    assert seg["max_adjacent_gap_seconds"] == 10.0


def test_readings_index_re_numbered_within_segment(client):
    for p in stable_readings([99.0, 20.0, 20.1, 20.2]):
        client.post("/api/v1/packages", json=p)
    body = release(client, max_fluctuation=0.5, min_duration_seconds=20).json()
    seg = body["segment"]
    assert [r["index"] for r in seg["readings"]] == [1, 2, 3]
    assert [r["seq"] for r in seg["readings"]] == [2, 3, 4]


# ---------------------------------------------------------------------------
# 数据源：仅已提交包；指标过滤
# ---------------------------------------------------------------------------

def test_only_committed_packages_participate(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T08:00:00Z")]))
    client.post("/api/v1/packages", json=pkg(
        2, "2", [m(20.1, ts="2026-09-23T08:00:10Z")]))
    # seq 4 先到：缓冲，不参与
    client.post("/api/v1/packages", json=pkg(
        4, "4", [m(20.2, ts="2026-09-23T08:00:20Z")]))
    body = release(client).json()
    assert body["candidate_count"] == 2
    assert body["released"] is False
    # 补齐 seq 3 后排水提交 seq 3、4
    client.post("/api/v1/packages", json=pkg(
        3, "3", [m(20.05, ts="2026-09-23T08:00:15Z")]))
    body = release(client).json()
    assert body["candidate_count"] == 4
    assert body["released"] is True


def test_metric_filter_and_other_metrics_keep_position(client):
    client.post("/api/v1/packages", json=pkg(1, "1", [
        {"metric": "ph", "value": 7.0},
        m(20.0, ts="2026-09-23T08:00:00Z"),
        {"metric": "humidity", "value": 55.0},
        m(20.1, ts="2026-09-23T08:00:10Z"),
        m(20.2, ts="2026-09-23T08:00:20Z"),
    ]))
    body = release(client, metric="ph", unit="C").json()
    assert body["candidate_count"] == 0
    assert body["released"] is False
    body = release(client).json()
    assert body["candidate_count"] == 3
    assert [r["position"] for r in body["segment"]["readings"]] == [1, 3, 4]


# ---------------------------------------------------------------------------
# 空数据 / 参数校验 / 404 / 只读
# ---------------------------------------------------------------------------

def test_unknown_device_404(client):
    r = client.get(
        "/api/v1/devices/ghost-device/metrics/warmup-release",
        params={"metric": "temperature", "unit": "C", **BASE, **DEFAULTS},
    )
    assert r.status_code == 404
    assert "未知设备" in r.json()["detail"]


def test_no_candidates_not_released(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T10:00:00Z")]))  # 时间窗外
    body = release(client).json()
    assert body["released"] is False
    assert body["segment"] is None
    assert body["candidate_count"] == 0


def test_invalid_params_rejected(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T08:00:00Z")]))
    base = {"metric": "temperature", "unit": "C", **BASE, **DEFAULTS}

    def status(**over):
        p = {**base, **over}
        return client.get(
            f"/api/v1/devices/{_dev()}/metrics/warmup-release", params=p
        ).status_code

    assert status(**{"from": BASE["to"], "to": BASE["from"]}) == 422
    assert status(max_fluctuation=-0.1) == 422
    assert status(min_duration_seconds=0) == 422
    assert status(min_readings=0) == 422
    assert status(max_gap_seconds=-1) == 422
    for missing in ("metric", "unit", "from", "to", "max_fluctuation",
                    "min_duration_seconds", "min_readings", "max_gap_seconds"):
        p = {**base}
        del p[missing]
        assert client.get(
            f"/api/v1/devices/{_dev()}/metrics/warmup-release", params=p
        ).status_code == 422


def test_echo_params_in_response(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T08:00:00Z")]))
    body = release(client, max_fluctuation=0.5, min_duration_seconds=12,
                   min_readings=2, max_gap_seconds=7).json()
    assert body["max_fluctuation"] == 0.5
    assert body["min_duration_seconds"] == 12
    assert body["min_readings"] == 2
    assert body["max_gap_seconds"] == 7
    assert body["from"] == BASE["from"]
    assert body["to"] == BASE["to"]
    assert body["unit"] == "C" and body["metric"] == "temperature"
    assert body["device_id"] == _dev()


def test_read_only_repeated_calls_identical(client):
    for p in stable_readings([50.0, 20.0, 20.1, 20.2]):
        client.post("/api/v1/packages", json=p)
    b1 = release(client).json()
    b2 = release(client).json()
    assert b1 == b2
    assert client.get(
        f"/api/v1/devices/{_dev()}/committed"
    ).json()["seqs"] == [1, 2, 3, 4]
    assert client.get(f"/api/v1/devices/{_dev()}/gaps").json()["gaps"] == []
