"""设备指标校准：创建（区间重叠事务内校验）、按时刻解析唯一生效配置并计算校准值、
校准不改写已存原始测量。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def cal(device, metric, valid_from, valid_to=None, offset=0.0, scale=1.0):
    body = {
        "device_id": device,
        "metric": metric,
        "valid_from": valid_from,
        "offset": offset,
        "scale": scale,
    }
    if valid_to is not None:
        body["valid_to"] = valid_to
    return body


def resolve(client, device, metric, at, value):
    return client.get(
        f"/api/v1/devices/{device}/calibrations/resolve",
        params={"metric": metric, "at": at, "value": value},
    )


def test_create_calibration_defaults(client):
    r = client.post(
        "/api/v1/calibrations",
        json=cal("cal-a", "temperature", "2026-01-01T00:00:00Z"),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["device_id"] == "cal-a"
    assert body["metric"] == "temperature"
    assert body["valid_from"] == "2026-01-01T00:00:00Z"
    assert body["valid_to"] is None  # 缺省无限期
    assert body["offset"] == 0.0 and body["scale"] == 1.0
    assert body["id"] >= 1 and body["created_at"]


def test_create_rejects_inverted_or_empty_range(client):
    # valid_from 晚于 valid_to
    r = client.post(
        "/api/v1/calibrations",
        json=cal("cal-b", "temperature", "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    assert r.status_code == 422
    # 空区间（起点等于终点）
    r = client.post(
        "/api/v1/calibrations",
        json=cal("cal-b", "temperature", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    assert r.status_code == 422


def test_overlapping_ranges_rejected(client):
    base = cal("cal-c", "temperature", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert client.post("/api/v1/calibrations", json=base).status_code == 201

    overlapping = [
        base,                                                    # 完全相同
        cal("cal-c", "temperature", "2026-01-10T00:00:00Z", "2026-01-20T00:00:00Z"),  # 被包含
        cal("cal-c", "temperature", "2025-12-01T00:00:00Z", "2026-03-01T00:00:00Z"),  # 包含
        cal("cal-c", "temperature", "2025-12-15T00:00:00Z", "2026-01-15T00:00:00Z"),  # 左交叠
        cal("cal-c", "temperature", "2026-01-15T00:00:00Z", "2026-02-15T00:00:00Z"),  # 右交叠
        cal("cal-c", "temperature", "2026-01-15T00:00:00Z"),     # 无限期，起点落在已有区间内
        cal("cal-c", "temperature", "2025-01-01T00:00:00Z"),     # 无限期，覆盖已有区间
    ]
    for body in overlapping:
        r = client.post("/api/v1/calibrations", json=body)
        assert r.status_code == 409, body
        assert "重叠" in r.json()["detail"]


def test_adjacent_and_disjoint_ranges_allowed(client):
    # 半开区间：终点与另一段起点相接不算重叠
    r = client.post("/api/v1/calibrations", json=cal(
        "cal-c", "temperature", "2025-12-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    assert r.status_code == 201
    r = client.post("/api/v1/calibrations", json=cal(
        "cal-c", "temperature", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"))
    assert r.status_code == 201
    # 不同指标 / 不同设备的相同区间互不影响
    r = client.post("/api/v1/calibrations", json=cal(
        "cal-c", "ph", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"))
    assert r.status_code == 201
    r = client.post("/api/v1/calibrations", json=cal(
        "cal-c2", "temperature", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"))
    assert r.status_code == 201


def test_open_ended_blocks_any_later_range(client):
    assert client.post("/api/v1/calibrations", json=cal(
        "cal-d", "temperature", "2026-06-01T00:00:00Z")).status_code == 201
    # 无限期配置存在后，同 (设备, 指标) 任何更晚的区间都重叠
    r = client.post("/api/v1/calibrations", json=cal(
        "cal-d", "temperature", "2027-01-01T00:00:00Z", "2027-02-01T00:00:00Z"))
    assert r.status_code == 409


def test_resolve_computes_calibrated_value(client):
    assert client.post("/api/v1/calibrations", json=cal(
        "cal-e", "temperature", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
        offset=1.5, scale=2.0)).status_code == 201

    r = resolve(client, "cal-e", "temperature", "2026-01-15T08:00:00Z", 10.0)
    assert r.status_code == 200
    body = r.json()
    assert body["calibrated_value"] == 21.5  # 10.0 * 2.0 + 1.5
    assert body["calibration"]["offset"] == 1.5
    assert body["calibration"]["scale"] == 2.0
    assert body["calibration"]["valid_to"] == "2026-02-01T00:00:00Z"


def test_resolve_boundaries_half_open(client):
    # 起点含：valid_from 时刻生效
    body = resolve(client, "cal-e", "temperature", "2026-01-01T00:00:00Z", 10.0).json()
    assert body["calibration"] is not None
    # 终点不含：valid_to 时刻已无配置
    body = resolve(client, "cal-e", "temperature", "2026-02-01T00:00:00Z", 10.0).json()
    assert body["calibration"] is None
    assert body["calibrated_value"] is None
    # 区间之前
    body = resolve(client, "cal-e", "temperature", "2025-12-31T23:59:59Z", 10.0).json()
    assert body["calibration"] is None
    assert body["calibrated_value"] is None


def test_resolve_no_config_returns_null(client):
    r = resolve(client, "cal-nope", "temperature", "2026-01-15T00:00:00Z", 3.0)
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "cal-nope"
    assert body["value"] == 3.0
    assert body["calibration"] is None
    assert body["calibrated_value"] is None


def test_resolve_naive_at_treated_as_utc_and_tz_normalized(client):
    # 不带时区按 UTC
    body = resolve(client, "cal-e", "temperature", "2026-01-15T08:00:00", 10.0).json()
    assert body["calibration"] is not None
    # 带偏移时区换算到同一 UTC 时刻
    body = resolve(client, "cal-e", "temperature", "2026-01-15T16:00:00+08:00", 10.0).json()
    assert body["calibration"] is not None
    assert body["calibrated_value"] == 21.5


def test_resolve_picks_unique_effective_config(client):
    assert client.post("/api/v1/calibrations", json=cal(
        "cal-f", "ph", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
        offset=0.5, scale=1.0)).status_code == 201
    assert client.post("/api/v1/calibrations", json=cal(
        "cal-f", "ph", "2026-02-01T00:00:00Z",
        offset=1.5, scale=1.0)).status_code == 201

    body = resolve(client, "cal-f", "ph", "2026-01-15T00:00:00Z", 7.0).json()
    assert body["calibrated_value"] == 7.5
    # 无限期配置对任意未来时刻生效
    body = resolve(client, "cal-f", "ph", "2027-06-01T00:00:00Z", 7.0).json()
    assert body["calibrated_value"] == 8.5
    assert body["calibration"]["valid_to"] is None


def test_calibration_does_not_rewrite_stored_measurements(client):
    device = "cal-raw"
    pkg = {
        "device_id": device,
        "seq": 1,
        "idempotency_key": "cal-raw-1",
        "sent_at": "2026-01-15T00:00:00Z",
        "measurements": [
            {"metric": "temperature", "value": 10.0, "ts": "2026-01-15T00:00:00Z"}
        ],
    }
    assert client.post("/api/v1/packages", json=pkg).status_code == 200
    assert client.post("/api/v1/calibrations", json=cal(
        device, "temperature", "2026-01-01T00:00:00Z",
        offset=1.5, scale=2.0)).status_code == 201

    # resolve 按配置计算校准值
    body = resolve(client, device, "temperature", "2026-01-15T00:00:00Z", 10.0).json()
    assert body["calibrated_value"] == 21.5

    # 已存原始测量不变：归档导出内容仍是原始值
    export = client.post("/api/v1/exports", json={"device_id": device}).json()
    page = client.get(f"/api/v1/exports/{export['export_id']}/pages")
    line = json.loads(page.text.splitlines()[0])
    assert line["measurements"][0]["value"] == 10.0

    # 窗口聚合同样基于原始值
    roll = client.get(
        f"/api/v1/devices/{device}/metrics/rollup",
        params={
            "metric": "temperature",
            "from": "2026-01-15T00:00:00Z",
            "to": "2026-01-15T01:00:00Z",
            "window_seconds": 3600,
        },
    ).json()
    assert roll["windows"][0]["mean"] == 10.0
