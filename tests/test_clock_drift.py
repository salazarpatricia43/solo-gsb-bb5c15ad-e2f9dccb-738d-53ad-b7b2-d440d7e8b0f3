"""GET /api/v1/devices/{id}/metrics/clock-drift 设备时钟漂移诊断测试。"""

import itertools
from datetime import datetime, timedelta, timezone

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
    return f"drift-{name}-{_suffix['v']}"


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


def m(value=20.0, unit="C", ts=None, metric="temperature"):
    d = {"metric": metric, "value": value}
    if unit is not None:
        d["unit"] = unit
    if ts is not None:
        d["ts"] = ts
    return d


T0 = datetime(2026, 9, 24, 8, 0, 0, tzinfo=timezone.utc)
BASE = {"from": "2026-09-24T08:00:00Z", "to": "2026-09-24T09:00:00Z"}


def iso(dt):
    # 保留亚秒精度（漂移测试常用 0.5s 级偏移）
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_plus8(dt):
    # 同一时刻的 +08:00 表示，用于时区归一化测试
    s = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        s += f".{dt.microsecond:06d}"
    return s + "+08:00"


def diagnose(client, device="dev", **params):
    params.setdefault("metric", "temperature")
    params.setdefault("from", BASE["from"])
    params.setdefault("to", BASE["to"])
    params.setdefault("max_abs_drift_per_hour", 1.0)
    return client.get(
        f"/api/v1/devices/{_dev(device)}/metrics/clock-drift", params=params
    )


# ---------------------------------------------------------------------------
# 基本估计
# ---------------------------------------------------------------------------

def test_basic_constant_drift_estimate(client):
    # 每小时快 2 秒：08:00 偏移 0、08:30 偏移 1、08:45 偏移 1.5
    minutes = [0, 30, 45]
    offsets = [0.0, 1.0, 1.5]
    for i, (minute, off) in enumerate(zip(minutes, offsets)):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))

    body = diagnose(client, max_abs_drift_per_hour=5.0).json()
    assert body["sample_count"] == 3
    assert body["package_count"] == 3
    assert body["sufficient_evidence"] is True
    assert body["conclusion"] == "within_limit"
    assert body["drift_per_hour"] == pytest.approx(2.0)
    # 末次（08:45）偏移 1.5s
    assert body["last_offset_seconds"] == pytest.approx(1.5)
    assert body["last_sent_at"] == "2026-09-24T08:45:00Z"
    assert body["samples"][0]["offset_seconds"] == pytest.approx(0.0)
    assert body["samples"][0]["seq"] == 1
    assert body["samples"][0]["position"] == 0


def test_negative_drift_clock_running_slow(client):
    # 每小时慢 3 秒：08:00 偏移 0、08:30 偏移 -1.5、08:45 偏移 -2.25
    minutes = [0, 30, 45]
    offsets = [0.0, -1.5, -2.25]
    for i, (minute, off) in enumerate(zip(minutes, offsets)):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))

    body = diagnose(client, max_abs_drift_per_hour=1.0).json()
    assert body["drift_per_hour"] == pytest.approx(-3.0)
    assert body["conclusion"] == "drift_exceeded"
    assert body["drift_exceeded"] is True
    assert body["last_offset_seconds"] == pytest.approx(-2.25)


def test_exceeded_is_strict_greater_than_limit_boundary_ok(client):
    # 漂移恰好等于上限 2 秒/小时：边界合格，不超限
    minutes = [0, 30, 45]
    offsets = [0.0, 1.0, 1.5]
    for i, (minute, off) in enumerate(zip(minutes, offsets)):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))

    body = diagnose(client, max_abs_drift_per_hour=2.0).json()
    assert body["drift_per_hour"] == pytest.approx(2.0)
    assert body["drift_exceeded"] is False
    assert body["conclusion"] == "within_limit"


def test_median_pairwise_slope_robust_to_outlier(client):
    # 理想线性 2 秒/小时的四个点上叠加一个异常偏移，中位数斜率仍为 2
    for i in range(4):
        sent = T0 + timedelta(minutes=15 * i)
        ideal = 2.0 * i / 4  # 0, 0.5, 1.0, 1.5
        if i == 2:
            ideal += 30.0  # 异常点
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}", [m(ts=iso(sent + timedelta(seconds=ideal)))],
            sent_at=iso(sent)))

    body = diagnose(client).json()
    # 6 个样本对：含异常点的斜率被抬高，不含异常点的斜率为 2；中位数 = 2
    assert body["drift_per_hour"] == pytest.approx(2.0)
    assert body["sample_count"] == 4


def test_last_offset_is_median_residual(client):
    # 零漂移但偏移含噪声：末次偏移估计取残差中位数（中间包偏移 5 不参与中位）
    for i, (minute, off) in enumerate([(0, 2.0), (20, 5.0), (40, 2.0)]):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))

    body = diagnose(client, max_abs_drift_per_hour=100.0).json()
    # 斜率 +9 / 0 / -9 秒每小时，中位数为 0；残差即各偏移，中位数 = 2.0
    assert body["drift_per_hour"] == pytest.approx(0.0, abs=1e-6)
    assert body["last_offset_seconds"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 样本提取与去重
# ---------------------------------------------------------------------------

def test_same_sent_at_keeps_largest_seq(client):
    # seq 1、2 同 sent_at 08:00（seq 2 的偏移 100 应被采用）
    client.post("/api/v1/packages", json=pkg(
        1, "k1", [m(ts=iso(T0))], sent_at=iso(T0)))
    client.post("/api/v1/packages", json=pkg(
        2, "k2", [m(ts=iso(T0 + timedelta(seconds=100)))], sent_at=iso(T0)))
    sent2 = T0 + timedelta(minutes=30)
    client.post("/api/v1/packages", json=pkg(
        3, "k3", [m(ts=iso(sent2 + timedelta(seconds=100)))],
        sent_at=iso(sent2)))
    sent3 = T0 + timedelta(minutes=45)
    client.post("/api/v1/packages", json=pkg(
        4, "k4", [m(ts=iso(sent3 + timedelta(seconds=100)))],
        sent_at=iso(sent3)))

    body = diagnose(client, max_abs_drift_per_hour=100.0).json()
    assert body["sample_count"] == 3
    assert body["package_count"] == 4
    first = body["samples"][0]
    assert first["seq"] == 2  # 同刻保留序号最大的一包
    assert first["offset_seconds"] == pytest.approx(100.0)
    # 三个点偏移恒为 100：零漂移
    assert body["drift_per_hour"] == pytest.approx(0.0)
    assert body["last_offset_seconds"] == pytest.approx(100.0)


def test_first_ts_measurement_per_package(client):
    # 同一指标前两条带 ts：只取第一条；中间夹无 ts 测量不影响
    client.post("/api/v1/packages", json=pkg(
        1, "k1",
        [
            m(20.0, ts=iso(T0)),
            m(21.0, ts=None),
            m(22.0, ts=iso(T0 + timedelta(seconds=9))),
        ],
        sent_at=iso(T0)))
    for i, (minute, off) in enumerate([(30, 4.0), (45, 6.0)]):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 2, f"k{i+2}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))

    body = diagnose(client).json()
    assert body["sample_count"] == 3
    assert body["samples"][0]["position"] == 0
    assert body["samples"][0]["offset_seconds"] == pytest.approx(0.0)


def test_skip_packages_without_sent_at(client):
    # 无 sent_at 的包跳过且不占上限
    client.post("/api/v1/packages", json=pkg(
        1, "k1", [m(ts=iso(T0))]))
    for i, (minute, off) in enumerate([(30, 1.0), (45, 1.5)]):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 2, f"k{i+2}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))

    body = diagnose(client).json()
    assert body["sample_count"] == 2
    assert body["package_count"] == 2
    assert body["excluded_no_sent_at_count"] == 1
    assert body["sufficient_evidence"] is False
    assert body["conclusion"] == "insufficient_evidence"
    assert body["drift_per_hour"] is None
    assert body["last_offset_seconds"] is None
    assert body["drift_exceeded"] is None
    assert "不足 3" in body["conclusion_detail"]


def test_skip_in_range_packages_without_ts(client):
    # 范围内包无该指标带 ts 测量：跳过并计入 no_ts；范围外包完全不计数
    client.post("/api/v1/packages", json=pkg(
        1, "k1", [m(ts=None)], sent_at=iso(T0)))
    client.post("/api/v1/packages", json=pkg(
        2, "k2", [m(metric="ph", value=7.0, ts=iso(T0 + timedelta(minutes=5)))],
        sent_at=iso(T0 + timedelta(minutes=5))))
    # 范围外（07:00）
    client.post("/api/v1/packages", json=pkg(
        3, "k3", [m(ts=iso(T0 - timedelta(hours=1)))],
        sent_at=iso(T0 - timedelta(hours=1))))
    for i, (minute, off) in enumerate([(30, 1.0), (45, 1.5)]):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 4, f"k{i+4}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))

    body = diagnose(client).json()
    assert body["sample_count"] == 2
    assert body["package_count"] == 4  # 范围内 4 包（含两包无 ts）
    assert body["excluded_no_ts_count"] == 2
    assert body["excluded_no_sent_at_count"] == 0
    assert body["sufficient_evidence"] is False


def test_half_open_range_on_sent_at_boundary(client):
    # sent_at == from 纳入；sent_at == to 排除（to 边界不含）
    client.post("/api/v1/packages", json=pkg(
        1, "k1", [m(ts=iso(T0))], sent_at=iso(T0)))
    sent = T0 + timedelta(minutes=30)
    client.post("/api/v1/packages", json=pkg(
        2, "k2", [m(ts=iso(sent))], sent_at=iso(sent)))
    at_to = datetime(2026, 9, 24, 9, 0, 0, tzinfo=timezone.utc)
    client.post("/api/v1/packages", json=pkg(
        3, "k3", [m(ts=iso(at_to))], sent_at=iso(at_to)))

    body = diagnose(client).json()
    assert body["package_count"] == 2
    assert body["samples"][-1]["sent_at"] == iso(sent)


def test_no_samples_at_all(client):
    # 设备存在但范围内没有该指标带 ts 的样本
    client.post("/api/v1/packages", json=pkg(
        1, "k1", [m(metric="ph", value=7.0, ts=iso(T0))], sent_at=iso(T0)))
    body = diagnose(client).json()
    assert body["sample_count"] == 0
    assert body["sufficient_evidence"] is False
    assert body["conclusion"] == "insufficient_evidence"
    assert body["last_sent_at"] is None
    assert body["samples"] == []


# ---------------------------------------------------------------------------
# 上限、参数与错误
# ---------------------------------------------------------------------------

def test_package_limit_exactly_200_ok(client):
    for i in range(200):
        sent = T0 + timedelta(seconds=i * 10)
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}", [m(ts=iso(sent))], sent_at=iso(sent)))
    r = diagnose(client)
    assert r.status_code == 200
    assert r.json()["package_count"] == 200


def test_package_limit_201_rejected_not_truncated(client):
    for i in range(201):
        sent = T0 + timedelta(seconds=i * 10)
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}", [m(ts=iso(sent))], sent_at=iso(sent)))
    r = diagnose(client)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "201" in detail and "200" in detail


def test_packages_without_sent_at_do_not_count_toward_limit(client):
    # 201 个无 sent_at 包 + 3 个正常包：不触发上限，正常出结果
    for i in range(201):
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"miss{i}", [m(ts=iso(T0))]))
    for j, (minute, off) in enumerate([(0, 0.0), (30, 1.0), (45, 1.5)]):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            202 + j, f"ok{j}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))
    r = diagnose(client, max_abs_drift_per_hour=5.0)
    assert r.status_code == 200
    body = r.json()
    assert body["sample_count"] == 3
    assert body["excluded_no_sent_at_count"] == 201
    assert body["drift_per_hour"] == pytest.approx(2.0)


def test_unknown_device_404(client):
    r = client.get(
        "/api/v1/devices/no-such-device/metrics/clock-drift",
        params={**BASE, "metric": "x", "max_abs_drift_per_hour": 1.0},
    )
    assert r.status_code == 404


def test_invalid_time_range_and_negative_limit(client):
    client.post("/api/v1/packages", json=pkg(
        1, "k1", [m(ts=iso(T0))], sent_at=iso(T0)))
    r = diagnose(client, **{"from": BASE["to"], "to": BASE["from"]})
    assert r.status_code == 422
    r = diagnose(client, max_abs_drift_per_hour=-0.1)
    assert r.status_code == 422


def test_timezone_normalized_to_utc(client):
    # sent_at 与 ts 均用 +08:00 表示，归一化为 UTC 后求偏移
    base = T0 + timedelta(hours=8)  # 16:00+08:00 == 08:00Z
    for i, (minute, off) in enumerate([(0, 0.0), (30, 1.0), (45, 1.5)]):
        s = base + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}",
            [m(ts=iso_plus8(s + timedelta(seconds=off)))],
            sent_at=iso_plus8(s)))
    body = diagnose(client, max_abs_drift_per_hour=5.0).json()
    assert body["samples"][0]["sent_at"] == "2026-09-24T08:00:00Z"
    assert body["drift_per_hour"] == pytest.approx(2.0)
    assert body["last_sent_at"] == "2026-09-24T08:45:00Z"


def test_read_only_does_not_mutate_packages(client):
    for i, (minute, off) in enumerate([(0, 0.0), (30, 1.0), (45, 1.5)]):
        sent = T0 + timedelta(minutes=minute)
        client.post("/api/v1/packages", json=pkg(
            i + 1, f"k{i}", [m(ts=iso(sent + timedelta(seconds=off)))],
            sent_at=iso(sent)))
    first = diagnose(client).json()
    second = diagnose(client).json()
    # 重复执行结果一致（纯只读）
    assert first == second
    # 原始已提交包未被改写
    committed = client.get(f"/api/v1/devices/{_dev()}/committed").json()
    assert committed["count"] == 3
