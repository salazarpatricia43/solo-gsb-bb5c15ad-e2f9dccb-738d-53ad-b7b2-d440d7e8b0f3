"""GET /api/v1/devices/{id}/metrics/exposure 超阈暴露量积分测试。"""

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
    return f"exp-{name}-{_suffix['v']}"


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


def exposure(client, **params):
    params = {
        "metric": "temperature",
        "unit": "C",
        "threshold": 10,
        "max_gap_seconds": 60,
        **BASE,
        **params,
    }
    return client.get(
        f"/api/v1/devices/{_dev()}/metrics/exposure", params=params
    )


def readings_at(specs, start=0, step=10):
    """按 specs 生成每包一条读数的包序列；元素为数值（按 step 排时间）或 (值, 秒时间[, 单位])。"""
    out = []
    for i, spec in enumerate(specs):
        if isinstance(spec, (int, float)):
            value, sec, unit = float(spec), start + i * step, "C"
        else:
            value = spec[0]
            sec = spec[1] if len(spec) > 1 and spec[1] is not None else start + i * step
            unit = spec[2] if len(spec) > 2 else "C"
        h, rem = divmod(8 * 3600 + sec, 3600)
        mi, s = divmod(rem, 60)
        ts = f"2026-09-23T{h:02d}:{mi:02d}:{s:02d}Z"
        out.append(pkg(i + 1, str(i + 1), [m(value, unit=unit, ts=ts)]))
    return out


# ---------------------------------------------------------------------------
# 基础积分：穿越阈值的三角形 / 梯形
# ---------------------------------------------------------------------------

def test_basic_triangle_crossing(client):
    # t=0:0（阈下）、t=10:10（恰在阈值）、t=20:20（峰）、t=30:0：
    # 上升段在 t=10 穿越、下降段在 t=25 穿越，超阈区间 (10, 25)。
    # 面积 = 上升三角 10·10/2 + 下降三角 10·5/2 = 50 + 25 = 75
    for p in readings_at([(0.0, 0), (10.0, 10), (20.0, 20), (0.0, 30)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client).json()
    assert body["sample_count"] == 4
    assert body["segment_count"] == 3
    assert body["exposure_seconds"] == pytest.approx(15.0)
    assert body["exposure_area"] == pytest.approx(75.0)


def test_trapezoid_above_threshold(client):
    # t=0:20、t=10:20（矩形）、t=30:0（下降）：
    # 矩形 10s×10 + 下降段穿越点 t=20，三角 10s×10/2
    for p in readings_at([(20.0, 0), (20.0, 10), (0.0, 30)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client).json()
    assert body["exposure_seconds"] == pytest.approx(20.0)
    assert body["exposure_area"] == pytest.approx(100.0 + 50.0)


def test_all_below_threshold_is_zero(client):
    for p in readings_at([(0.0, 0), (5.0, 10), (10.0, 20), (0.0, 30)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client).json()
    assert body["exposure_seconds"] == 0.0
    assert body["exposure_area"] == 0.0
    # 恰在阈值上不超阈
    assert body["segment_count"] == 3


def test_threshold_boundary_strict(client):
    # 恒为阈值：秒数与面积都为 0
    for p in readings_at([(10.0, 0), (10.0, 10), (10.0, 20)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client).json()
    assert body["exposure_seconds"] == 0.0
    assert body["exposure_area"] == 0.0


def test_negative_threshold(client):
    # 阈值 -10：全程超阈（取值恒为 0），面积 = 10 × 范围内时长
    for p in readings_at([(0.0, 0), (0.0, 20), (0.0, 40)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, threshold=-10, **{
        "from": "2026-09-23T08:00:00Z",
        "to": "2026-09-23T08:00:30Z",
    }).json()
    assert body["exposure_seconds"] == pytest.approx(30.0)
    assert body["exposure_area"] == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# [from, to) 裁剪（含边界半开语义）
# ---------------------------------------------------------------------------

def test_window_clips_line_segments(client):
    # 三角形 (0:0, 10:10, 20:20, 30:0)，窗口 [15, 25) 同时裁到上升、下降段：
    # 上升段裁剪到 [15,20]：v 15→20，面积 (5+10)·5/2 = 37.5；
    # 下降段裁剪到 [20,25]：v 20→10，面积 (10+0)·5/2 = 25。
    # 超阈时长 10s、面积 62.5（注意 t=15 处的取值由窗外相邻读数插值得到）。
    for p in readings_at([(0.0, 0), (10.0, 10), (20.0, 20), (0.0, 30)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, **{
        "from": "2026-09-23T08:00:15Z",
        "to": "2026-09-23T08:00:25Z",
    }).json()
    assert body["segment_count"] == 2
    assert body["exposure_seconds"] == pytest.approx(10.0)
    assert body["exposure_area"] == pytest.approx(62.5)


def test_half_open_endpoint_excludes_to(client):
    # 三角形 (0:0, 10:10, 20:20, 30:0)：
    # 窗口 [10, 30)：超阈区间 (10, 25)，时长 15s、面积 75
    for p in readings_at([(0.0, 0), (10.0, 10), (20.0, 20), (0.0, 30)]):
        client.post("/api/v1/packages", json=p)
    body_half = exposure(client, **{
        "from": "2026-09-23T08:00:10Z",
        "to": "2026-09-23T08:00:30Z",
    }).json()
    assert body_half["exposure_seconds"] == pytest.approx(15.0)
    assert body_half["exposure_area"] == pytest.approx(75.0)
    # 右端外扩到 31：最后一条读数在 t=30，其后再无线段；额外时间窗内
    # 曲线已在阈值下方（t=30 处降至 0），秒数、面积与线段数均不变
    body_closed = exposure(client, **{
        "from": "2026-09-23T08:00:10Z",
        "to": "2026-09-23T08:00:31Z",
    }).json()
    assert body_closed["exposure_seconds"] == pytest.approx(15.0)
    assert body_closed["exposure_area"] == pytest.approx(75.0)
    assert body_closed["segment_count"] == body_half["segment_count"] == 2


def test_reading_exactly_at_to_no_contribution(client):
    # 唯一超阈读数恰在 to：半开区间排除，结果为零
    for p in readings_at([(20.0, 0), (20.0, 10)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, **{
        "from": "2026-09-23T08:00:00Z",
        "to": "2026-09-23T08:00:10Z",
    }).json()
    assert body["exposure_seconds"] == pytest.approx(10.0)
    # 范围前无任何读数时，to 处的单点不能与"自己"成段
    body2 = exposure(client, **{
        "from": "2026-09-23T08:00:10Z",
        "to": "2026-09-23T08:00:20Z",
    }).json()
    assert body2["exposure_seconds"] == 0.0
    assert body2["exposure_area"] == 0.0
    assert body2["segment_count"] == 0


# ---------------------------------------------------------------------------
# 范围外相邻读数参与边界插值
# ---------------------------------------------------------------------------

def test_out_of_range_neighbors_interpolate_boundaries(client):
    # t=-10:0（窗外，阈下）→ t=10:20（超阈峰）→ t=30:0（窗外，阈下）；
    # 窗口 [5, 15)：上升段交点恰在 t=5、下降段交点恰在 t=15，
    # 两个交点都依赖窗外相邻读数插值得到。超阈时长 10s；
    # 面积 = 两个梯形 (0+10)·5/2 ×2 = 75（t=10 处峰高 20，超阈 10）。
    for p in readings_at([(0.0, -10), (20.0, 10), (0.0, 30)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, **{
        "from": "2026-09-23T08:00:05Z",
        "to": "2026-09-23T08:00:15Z",
    }).json()
    assert body["sample_count"] == 3  # 范围外读数仍计入样本
    assert body["segment_count"] == 2
    assert body["exposure_seconds"] == pytest.approx(10.0)
    assert body["exposure_area"] == pytest.approx(75.0)


def test_crossing_inside_range_needs_both_neighbors(client):
    # 两条读数都在窗口外，但连线穿越窗口与阈值：
    # t=-10:0 → t=20:20，窗口 [0, 10)；t=0 处 v=6.67（阈下），
    # 交点 t=5，超阈 [5, 10)，t=10 处 v=13.33
    for p in readings_at([(0.0, -10), (20.0, 20)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, **{
        "from": "2026-09-23T08:00:00Z",
        "to": "2026-09-23T08:00:10Z",
    }).json()
    assert body["sample_count"] == 2
    assert body["segment_count"] == 1
    assert body["exposure_seconds"] == pytest.approx(5.0)
    assert body["exposure_area"] == pytest.approx((10.0 / 3.0) * 5.0 / 2.0)


def test_segments_disjoint_from_window_ignored(client):
    # 全部读数在窗口左侧；最近邻在 from 处结束，不贡献
    for p in readings_at([(20.0, -20), (20.0, -10)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, **{
        "from": "2026-09-23T08:00:00Z",
        "to": "2026-09-23T08:00:10Z",
    }).json()
    assert body["exposure_seconds"] == 0.0
    assert body["exposure_area"] == 0.0
    assert body["segment_count"] == 0


# ---------------------------------------------------------------------------
# max_gap_seconds：间隔超限整段不计
# ---------------------------------------------------------------------------

def test_large_gap_drops_whole_segment(client):
    # t=0:20、t=10:20（正常）、t=100:20（间隔 90s 超限）：
    # 仅 [0,10] 段参与积分
    for p in readings_at([(20.0, 0), (20.0, 10), (20.0, 100)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, max_gap_seconds=60).json()
    assert body["sample_count"] == 3
    assert body["segment_count"] == 1
    assert body["exposure_seconds"] == pytest.approx(10.0)
    assert body["exposure_area"] == pytest.approx(100.0)


def test_gap_boundary_inclusive(client):
    # 相邻间隔恰为 max_gap_seconds：边界合格，段仍参与
    for p in readings_at([(20.0, 0), (20.0, 60)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, max_gap_seconds=60, **{
        "from": "2026-09-23T08:00:00Z",
        "to": "2026-09-23T08:01:00Z",
    }).json()
    assert body["segment_count"] == 1
    assert body["exposure_seconds"] == pytest.approx(60.0)


def test_gap_does_not_invalidate_points_for_other_segments(client):
    # 0→10 超限，但 10→20、20→30 正常：后两段照常积分
    for p in readings_at([(20.0, 0), (20.0, 100), (20.0, 110), (20.0, 120)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, max_gap_seconds=60).json()
    assert body["segment_count"] == 2
    assert body["exposure_seconds"] == pytest.approx(20.0)


def test_zero_max_gap_keeps_zero_width_duplicates_only(client):
    # max_gap_seconds=0：只有同一时刻（间隔 0）的相邻读数段保留；
    # 去重后同刻只剩一条，因此没有任何段
    for p in readings_at([(20.0, 0), (20.0, 10), (20.0, 20)]):
        client.post("/api/v1/packages", json=p)
    body = exposure(client, max_gap_seconds=0).json()
    assert body["segment_count"] == 0
    assert body["exposure_seconds"] == 0.0


# ---------------------------------------------------------------------------
# 同一时刻保留最后一条
# ---------------------------------------------------------------------------

def test_same_time_keeps_last_after_sort(client):
    # t=10 三条：seq1 在后；seq2 包内位置 0；seq3 包内位置 2（其它指标占位）。
    # 排序 (时间, seq, 位置) 后最后一条为 seq3/位置2 = 20.0
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(12.0, ts="2026-09-23T08:00:10Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(2, "2", [
        m(15.0, ts="2026-09-23T08:00:10Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(3, "3", [
        {"metric": "ph", "value": 7.0},
        {"metric": "ph", "value": 7.1},
        m(20.0, ts="2026-09-23T08:00:10Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(4, "4", [
        m(20.0, ts="2026-09-23T08:00:20Z"),
    ]))
    body = exposure(client).json()
    assert body["sample_count"] == 2  # 同刻三条去重为一条
    assert body["segment_count"] == 1
    # t=10 值 20 → t=20 值 20：恒超阈 10s，面积 100
    assert body["exposure_seconds"] == pytest.approx(10.0)
    assert body["exposure_area"] == pytest.approx(100.0)


def test_same_time_dedup_counts_unit_filter_first(client):
    # 同刻两条：单位不符的一条先被丢弃，C 的一条保留
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(50.0, unit="F", ts="2026-09-23T08:00:10Z"),
        m(20.0, unit="C", ts="2026-09-23T08:00:10Z"),
    ]))
    body = exposure(client).json()
    assert body["sample_count"] == 1
    assert body["excluded_unit_mismatch_count"] == 1


# ---------------------------------------------------------------------------
# 时间来源：ts 优先、sent_at 回退、无时间丢弃
# ---------------------------------------------------------------------------

def test_ts_preferred_over_sent_at(client):
    specs = [
        (1, "2026-09-23T08:00:00Z", "2026-09-23T09:00:00Z", 20.0),
        (2, "2026-09-23T08:00:10Z", "2026-09-23T09:05:00Z", 20.0),
    ]
    for seq, ts, sent, v in specs:
        client.post("/api/v1/packages", json=pkg(
            seq, str(seq), [m(v, ts=ts)], sent_at=sent))
    body = exposure(client, max_gap_seconds=60).json()
    assert body["exposure_seconds"] == pytest.approx(10.0)


def test_sent_at_fallback(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts=None)], sent_at="2026-09-23T08:00:00Z"))
    client.post("/api/v1/packages", json=pkg(
        2, "2", [m(20.0, ts=None)], sent_at="2026-09-23T08:00:10Z"))
    body = exposure(client).json()
    assert body["sample_count"] == 2
    assert body["exposure_seconds"] == pytest.approx(10.0)
    assert body["exposure_area"] == pytest.approx(100.0)


def test_no_time_readings_dropped_and_reported(client):
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(20.0, ts=None),
        m(30.0, ts=None),
    ]))  # 包无 sent_at：两条全部丢弃
    body = exposure(client).json()
    assert body["sample_count"] == 0
    assert body["excluded_no_time_count"] == 2
    assert body["exposure_seconds"] == 0.0
    assert body["exposure_area"] == 0.0


def test_naive_datetimes_treated_as_utc(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T08:00:00")]))
    client.post("/api/v1/packages", json=pkg(
        2, "2", [m(20.0, ts="2026-09-23T08:00:10")]))
    body = exposure(client, **{
        "from": "2026-09-23T08:00:00", "to": "2026-09-23T09:00:00"
    }).json()
    assert body["exposure_seconds"] == pytest.approx(10.0)
    assert body["from"].endswith("Z")


# ---------------------------------------------------------------------------
# 单位过滤
# ---------------------------------------------------------------------------

def test_unit_mismatch_dropped_and_reported(client):
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(20.0, unit="C", ts="2026-09-23T08:00:00Z"),
        m(99.0, unit="F", ts="2026-09-23T08:00:05Z"),
        m(99.0, unit=None, ts="2026-09-23T08:00:06Z"),
        m(20.0, unit="C", ts="2026-09-23T08:00:10Z"),
    ]))
    body = exposure(client, max_gap_seconds=60).json()
    assert body["excluded_unit_mismatch_count"] == 2
    assert body["sample_count"] == 2
    assert body["exposure_seconds"] == pytest.approx(10.0)


def test_unit_mismatch_counted_even_outside_window(client):
    # 单位过滤先于时间范围：窗外的单位不符读数仍计入计数，
    # 且窗外 C 读数仍作为样本参与边界插值
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(99.0, unit="F", ts="2026-09-23T07:00:00Z"),
        m(5.0, unit="C", ts="2026-09-23T07:59:50Z"),
        m(15.0, unit="C", ts="2026-09-23T08:00:00Z"),
    ]))
    body = exposure(client, max_gap_seconds=60).json()
    assert body["excluded_unit_mismatch_count"] == 1
    assert body["sample_count"] == 2


# ---------------------------------------------------------------------------
# 数据源：仅已提交包；排序；指标过滤
# ---------------------------------------------------------------------------

def test_only_committed_packages_participate(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T08:00:00Z")]))
    client.post("/api/v1/packages", json=pkg(
        3, "3", [m(20.0, ts="2026-09-23T08:00:20Z")]))  # 缓冲，不参与
    body = exposure(client).json()
    assert body["sample_count"] == 1
    assert body["segment_count"] == 0
    assert body["exposure_seconds"] == 0.0
    # 补齐 seq 2 后排水提交
    client.post("/api/v1/packages", json=pkg(
        2, "2", [m(20.0, ts="2026-09-23T08:00:10Z")]))
    body = exposure(client).json()
    assert body["sample_count"] == 3
    assert body["segment_count"] == 2
    assert body["exposure_seconds"] == pytest.approx(20.0)


def test_sort_by_time_then_seq_then_position(client):
    # 乱序提交（经缓冲排水后 committed 按 seq），但测量 ts 乱序：
    # 排序须按 (时间, seq, 位置)
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(20.0, ts="2026-09-23T08:00:20Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(2, "2", [
        {"metric": "ph", "value": 7.0},
        m(15.0, ts="2026-09-23T08:00:00Z"),
        m(20.0, ts="2026-09-23T08:00:10Z"),
    ]))
    body = exposure(client).json()
    # 时间序：t0=15（seq2 位置1）、t10=20（seq2 位置2）、t20=20（seq1）
    assert body["sample_count"] == 3
    assert body["exposure_seconds"] == pytest.approx(20.0)
    # 首段 t0:15→t10:20 梯形 (5+10)·10/2=75；次段 20→20 矩形 100
    assert body["exposure_area"] == pytest.approx(75.0 + 100.0)


def test_metric_filter(client):
    client.post("/api/v1/packages", json=pkg(1, "1", [
        {"metric": "ph", "value": 99.0, "unit": "C",
         "ts": "2026-09-23T08:00:00Z"},
        m(20.0, ts="2026-09-23T08:00:00Z"),
    ]))
    body = exposure(client, metric="ph").json()
    assert body["sample_count"] == 1
    assert body["segment_count"] == 0
    assert body["exposure_seconds"] == 0.0


# ---------------------------------------------------------------------------
# 远期时间的微秒分辨率：相对坐标积分，绝对纪元微秒转 float64 不吞精度
# ---------------------------------------------------------------------------

def test_one_microsecond_segment_far_future_keeps_resolution(client):
    # 2300-01-01 附近自 UTC epoch 的绝对微秒约 1.04e16，float64 的 ULP
    # 约为 2，相邻 1 微秒转浮点后相等；线段运算须以相对首端点的微秒
    # 坐标进行。读数 2、2、阈值 1 的 1 微秒线段应得 0.000001 秒、
    # 0.000001 面积（超阈幅度恒为 1）。
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(2.0, ts="2300-01-01T00:00:00.000000Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(2, "2", [
        m(2.0, ts="2300-01-01T00:00:00.000001Z"),
    ]))
    body = exposure(client, threshold=1, max_gap_seconds=60, **{
        "from": "2300-01-01T00:00:00.000000Z",
        "to": "2300-01-01T00:00:00.000002Z",
    }).json()
    assert body["sample_count"] == 2
    assert body["segment_count"] == 1
    assert body["exposure_seconds"] == pytest.approx(0.000001)
    assert body["exposure_area"] == pytest.approx(0.000001)


def test_one_microsecond_crossing_segment_far_future_splits(client):
    # 同一远期时刻：0 → 2 的 1 微秒线段在 0.5us 处穿越阈值 1，
    # 只积分实际超阈的后半段：0.5us = 0.0000005 秒；
    # 面积为三角形 1·0.5us/2 = 0.25 读数单位·微秒 = 0.00000025。
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(0.0, ts="2300-01-01T00:00:00.000000Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(2, "2", [
        m(2.0, ts="2300-01-01T00:00:00.000001Z"),
    ]))
    body = exposure(client, threshold=1, max_gap_seconds=60, **{
        "from": "2300-01-01T00:00:00.000000Z",
        "to": "2300-01-01T00:00:00.000002Z",
    }).json()
    assert body["segment_count"] == 1
    assert body["exposure_seconds"] == pytest.approx(0.0000005)
    assert body["exposure_area"] == pytest.approx(0.00000025)


def test_far_future_window_clipping_keeps_microseconds(client):
    # 三条相距 1 微秒的读数（均为 2、阈值 1）：窗口恰为中间线段
    # [t1, t2)。前段终点恰在 from 上被半开裁剪排除；中间 1us 线段
    # 完整落入窗口，远期绝对微秒下裁剪仍须保住这 1 微秒。
    client.post("/api/v1/packages", json=pkg(1, "1", [
        m(2.0, ts="2299-12-31T23:59:59.999999Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(2, "2", [
        m(2.0, ts="2300-01-01T00:00:00.000000Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(3, "3", [
        m(2.0, ts="2300-01-01T00:00:00.000001Z"),
    ]))
    # 窗口恰为中间 1 微秒线段 [t1, t2)：半开右端不含 t2 之后的延伸
    body = exposure(client, threshold=1, max_gap_seconds=60, **{
        "from": "2300-01-01T00:00:00.000000Z",
        "to": "2300-01-01T00:00:00.000001Z",
    }).json()
    assert body["sample_count"] == 3
    assert body["segment_count"] == 1
    assert body["exposure_seconds"] == pytest.approx(0.000001)
    assert body["exposure_area"] == pytest.approx(0.000001)


# ---------------------------------------------------------------------------
# 空数据 / 参数校验 / 404 / 只读
# ---------------------------------------------------------------------------

def test_unknown_device_404(client):
    r = client.get(
        "/api/v1/devices/ghost-device/metrics/exposure",
        params={
            "metric": "temperature", "unit": "C",
            "threshold": 10, "max_gap_seconds": 60, **BASE,
        },
    )
    assert r.status_code == 404
    assert "未知设备" in r.json()["detail"]


def test_insufficient_samples_return_zeros(client):
    # 已知设备但零读数
    client.post("/api/v1/packages", json=pkg(
        1, "1", [{"metric": "ph", "value": 7.0}]))
    body = exposure(client).json()
    assert body["sample_count"] == 0
    assert body["segment_count"] == 0
    assert body["exposure_seconds"] == 0.0
    assert body["exposure_area"] == 0.0

    # 仅一条读数：无法插值，同样为零
    client.post("/api/v1/packages", json=pkg(
        2, "2", [m(99.0, ts="2026-09-23T08:00:00Z")]))
    body = exposure(client).json()
    assert body["sample_count"] == 1
    assert body["segment_count"] == 0
    assert body["exposure_seconds"] == 0.0
    assert body["exposure_area"] == 0.0


def test_invalid_params_rejected(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T08:00:00Z")]))
    base = {
        "metric": "temperature", "unit": "C",
        "threshold": 10, "max_gap_seconds": 60, **BASE,
    }

    def status(**over):
        p = {**base, **over}
        return client.get(
            f"/api/v1/devices/{_dev()}/metrics/exposure", params=p
        ).status_code

    assert status(**{"from": BASE["to"], "to": BASE["from"]}) == 422
    assert status(max_gap_seconds=-0.1) == 422
    for missing in ("metric", "unit", "from", "to", "threshold", "max_gap_seconds"):
        p = {**base}
        del p[missing]
        assert client.get(
            f"/api/v1/devices/{_dev()}/metrics/exposure", params=p
        ).status_code == 422


def test_echo_params_in_response(client):
    client.post("/api/v1/packages", json=pkg(
        1, "1", [m(20.0, ts="2026-09-23T08:00:00Z")]))
    body = exposure(client, threshold=12.5, max_gap_seconds=7).json()
    assert body["threshold"] == 12.5
    assert body["max_gap_seconds"] == 7
    assert body["from"] == BASE["from"]
    assert body["to"] == BASE["to"]
    assert body["unit"] == "C" and body["metric"] == "temperature"
    assert body["device_id"] == _dev()


def test_read_only_repeated_calls_identical(client):
    for p in readings_at([20.0, 15.0, 5.0, 20.0]):
        client.post("/api/v1/packages", json=p)
    b1 = exposure(client).json()
    b2 = exposure(client).json()
    assert b1 == b2
    assert client.get(
        f"/api/v1/devices/{_dev()}/committed"
    ).json()["seqs"] == [1, 2, 3, 4]
    assert client.get(f"/api/v1/devices/{_dev()}/gaps").json()["gaps"] == []
