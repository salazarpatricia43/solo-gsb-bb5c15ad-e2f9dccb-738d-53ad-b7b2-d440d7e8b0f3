"""GET /api/v1/devices/{id}/metrics/rollup 的测试。"""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(device, seq, key, value=1.0, metric="temperature", sent_at=None, measurements=None):
    p = {
        "device_id": device,
        "seq": seq,
        "idempotency_key": key,
        "measurements": measurements or [{"metric": metric, "value": value, "unit": "C"}],
    }
    if sent_at is not None:
        p["sent_at"] = sent_at
    return p


def rollup(client, device, **params):
    params.setdefault("metric", "temperature")
    return client.get(f"/api/v1/devices/{device}/metrics/rollup", params=params)


def calibration(device, metric, valid_from, valid_to=None, offset=0.0, scale=1.0):
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


def ts(s):
    """解析响应中的 ISO 时间为 aware datetime 便于比较。"""
    return datetime.fromisoformat(s)


def test_rollup_basic_windows_and_stats(client):
    client.post("/api/v1/packages", json=pkg("dev-r1", 1, "r1-1", 1.0, sent_at="2026-09-22T08:00:05Z"))
    client.post("/api/v1/packages", json=pkg("dev-r1", 2, "r1-2", 3.0, sent_at="2026-09-22T08:00:15Z"))
    client.post("/api/v1/packages", json=pkg("dev-r1", 3, "r1-3", 10.0, sent_at="2026-09-22T08:01:05Z"))
    client.post("/api/v1/packages", json=pkg("dev-r1", 4, "r1-4", 20.0, sent_at="2026-09-22T08:01:55Z"))

    r = rollup(
        client, "dev-r1",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:03:00Z"},
        window_seconds=60,
        allowed_lateness=0,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "dev-r1"
    assert body["metric"] == "temperature"
    assert body["late_count"] == 0
    assert ts(body["watermark"]) == datetime(2026, 9, 22, 8, 1, 55, tzinfo=timezone.utc)

    windows = body["windows"]
    assert len(windows) == 3
    # 稳定排序：start 严格递增
    starts = [ts(w["start"]) for w in windows]
    assert starts == sorted(starts)
    assert [ts(w["end"]) for w in windows] == [
        datetime(2026, 9, 22, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 22, 8, 2, tzinfo=timezone.utc),
        datetime(2026, 9, 22, 8, 3, tzinfo=timezone.utc),
    ]
    w0, w1, w2 = windows
    assert (w0["count"], w0["min"], w0["max"], w0["mean"]) == (2, 1.0, 3.0, 2.0)
    assert (w1["count"], w1["min"], w1["max"], w1["mean"]) == (2, 10.0, 20.0, 15.0)
    # 空窗口：count=0，统计值为 null
    assert w2["count"] == 0
    assert w2["min"] is None and w2["max"] is None and w2["mean"] is None


def test_rollup_half_open_boundaries(client):
    client.post("/api/v1/packages", json=pkg("dev-r2", 1, "r2-1", 1.0, sent_at="2026-09-22T08:00:00Z"))
    client.post("/api/v1/packages", json=pkg("dev-r2", 2, "r2-2", 2.0, sent_at="2026-09-22T08:01:00Z"))
    client.post("/api/v1/packages", json=pkg("dev-r2", 3, "r2-3", 3.0, sent_at="2026-09-22T08:02:00Z"))

    body = rollup(
        client, "dev-r2",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:02:00Z"},
        window_seconds=60,
    ).json()
    # sent_at == from 计入首窗口；sent_at 恰在窗口边界归入下一窗口；sent_at == to 排除
    assert [w["count"] for w in body["windows"]] == [1, 1]
    assert body["windows"][0]["mean"] == 1.0
    assert body["windows"][1]["mean"] == 2.0


def test_rollup_late_sample_does_not_rewrite_closed_window(client):
    # 先发事件时间较新的包：窗口 [08:10, 08:11) 关闭
    client.post("/api/v1/packages", json=pkg("dev-r3", 1, "r3-1", 100.0, sent_at="2026-09-22T08:10:00Z"))
    params = {
        "from": "2026-09-22T08:00:00Z",
        "to": "2026-09-22T08:20:00Z",
        "window_seconds": 60,
        "allowed_lateness": 300,
    }
    before = rollup(client, "dev-r3", **params).json()
    assert before["late_count"] == 0
    w = [w for w in before["windows"] if w["count"] > 0]
    assert len(w) == 1 and w[0]["count"] == 1 and w[0]["mean"] == 100.0

    # 迟到包：sent_at 08:00:10 < 水位(08:10:00 - 300s = 08:05:00)
    client.post("/api/v1/packages", json=pkg("dev-r3", 2, "r3-2", 1.0, sent_at="2026-09-22T08:00:10Z"))
    after = rollup(client, "dev-r3", **params).json()
    assert after["late_count"] == 1
    assert ts(after["watermark"]) == datetime(2026, 9, 22, 8, 5, tzinfo=timezone.utc)
    # 已关闭窗口的统计不被改写
    assert [w for w in after["windows"] if w["count"] > 0] == w
    # 迟到样本所属窗口保持为空
    late_window = after["windows"][0]  # [08:00, 08:01)
    assert late_window["count"] == 0


def test_rollup_allowed_lateness_admits_out_of_order(client):
    client.post("/api/v1/packages", json=pkg("dev-r4", 1, "r4-1", 100.0, sent_at="2026-09-22T08:10:00Z"))
    client.post("/api/v1/packages", json=pkg("dev-r4", 2, "r4-2", 1.0, sent_at="2026-09-22T08:00:10Z"))
    # 允许 600s 迟到：水位(08:10:00 - 600s = 08:00:00) 未越过 08:00:10，样本正常入窗
    body = rollup(
        client, "dev-r4",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:20:00Z"},
        window_seconds=60,
        allowed_lateness=600,
    ).json()
    assert body["late_count"] == 0
    assert body["windows"][0]["count"] == 1
    assert body["windows"][0]["mean"] == 1.0


def test_rollup_only_committed_participate(client):
    client.post("/api/v1/packages", json=pkg("dev-r5", 1, "r5-1", 1.0, sent_at="2026-09-22T08:00:00Z"))
    client.post("/api/v1/packages", json=pkg("dev-r5", 3, "r5-3", 3.0, sent_at="2026-09-22T08:02:00Z"))  # 缓冲
    params = {
        "from": "2026-09-22T08:00:00Z",
        "to": "2026-09-22T08:03:00Z",
        "window_seconds": 60,
    }
    body = rollup(client, "dev-r5", **params).json()
    assert [w["count"] for w in body["windows"]] == [1, 0, 0]  # 缓冲中的 seq 3 不参与
    # 补齐缺口后，排水的包按提交顺序参与聚合
    client.post("/api/v1/packages", json=pkg("dev-r5", 2, "r5-2", 2.0, sent_at="2026-09-22T08:01:00Z"))
    body = rollup(client, "dev-r5", **params).json()
    assert [w["count"] for w in body["windows"]] == [1, 1, 1]
    assert body["late_count"] == 0


def test_rollup_metric_filter_and_multi_measurement(client):
    measurements = [
        {"metric": "temperature", "value": 21.0},
        {"metric": "ph", "value": 7.0},
        {"metric": "ph", "value": 7.2},
    ]
    client.post("/api/v1/packages", json=pkg("dev-r6", 1, "r6-1", sent_at="2026-09-22T08:00:10Z", measurements=measurements))
    body = rollup(
        client, "dev-r6", metric="ph",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:01:00Z"},
        window_seconds=60,
    ).json()
    assert body["windows"][0]["count"] == 2
    assert body["windows"][0]["min"] == 7.0
    assert body["windows"][0]["max"] == 7.2
    assert body["windows"][0]["mean"] == pytest.approx(7.1)


def test_rollup_skips_packages_without_sent_at(client):
    client.post("/api/v1/packages", json=pkg("dev-r7", 1, "r7-1", 99.0))  # 无 sent_at
    client.post("/api/v1/packages", json=pkg("dev-r7", 2, "r7-2", 1.0, sent_at="2026-09-22T08:00:10Z"))
    body = rollup(
        client, "dev-r7",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:01:00Z"},
        window_seconds=60,
    ).json()
    assert body["windows"][0]["count"] == 1
    assert body["windows"][0]["mean"] == 1.0
    assert ts(body["watermark"]) == datetime(2026, 9, 22, 8, 0, 10, tzinfo=timezone.utc)


def test_rollup_empty_when_no_samples(client):
    client.post("/api/v1/packages", json=pkg("dev-r8", 1, "r8-1", 1.0, sent_at="2026-09-22T08:00:10Z"))
    body = rollup(
        client, "dev-r8", metric="nonexistent",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:02:00Z"},
        window_seconds=60,
    ).json()
    assert body["late_count"] == 0
    assert [w["count"] for w in body["windows"]] == [0, 0]
    # 水位是设备级的，与查询指标无关
    assert ts(body["watermark"]) == datetime(2026, 9, 22, 8, 0, 10, tzinfo=timezone.utc)


def test_rollup_naive_datetimes_treated_as_utc(client):
    client.post("/api/v1/packages", json=pkg("dev-r9", 1, "r9-1", 5.0, sent_at="2026-09-22T08:00:30"))
    body = rollup(
        client, "dev-r9",
        **{"from": "2026-09-22T08:00:00", "to": "2026-09-22T08:01:00"},
        window_seconds=60,
    ).json()
    assert body["windows"][0]["count"] == 1


def test_rollup_validation(client):
    client.post("/api/v1/packages", json=pkg("dev-r10", 1, "r10-1", 1.0, sent_at="2026-09-22T08:00:00Z"))
    base = {"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:01:00Z", "window_seconds": 60}
    # window_seconds 必须为正
    assert rollup(client, "dev-r10", **{**base, "window_seconds": 0}).status_code == 422
    assert rollup(client, "dev-r10", **{**base, "window_seconds": -5}).status_code == 422
    # from 必须早于 to
    assert rollup(client, "dev-r10", **{**base, "from": base["to"], "to": base["from"]}).status_code == 422
    # allowed_lateness 不能为负
    assert rollup(client, "dev-r10", **base, allowed_lateness=-1).status_code == 422
    # 缺少必填参数
    for missing in ("metric", "from", "to", "window_seconds"):
        params = {"metric": "temperature", **base}
        del params[missing]
        assert client.get("/api/v1/devices/dev-r10/metrics/rollup", params=params).status_code == 422
    # 窗口数超限
    assert rollup(
        client, "dev-r10",
        **{"from": "2026-09-01T00:00:00Z", "to": "2026-09-22T00:00:00Z"},
        window_seconds=1,
    ).status_code == 422


def test_rollup_unknown_device_404(client):
    r = rollup(
        client, "nope",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:01:00Z"},
        window_seconds=60,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 校准值模式（calibrated=true）
# ---------------------------------------------------------------------------

def test_rollup_calibrated_uses_scale_and_offset_at_packet_sent_at(client):
    device = "dev-c1"
    client.post("/api/v1/packages", json=pkg(device, 1, "c1-1", 10.0, sent_at="2026-09-22T08:00:10Z"))
    client.post("/api/v1/packages", json=pkg(device, 2, "c1-2", 20.0, sent_at="2026-09-22T08:01:10Z"))
    # 仅覆盖第一个包 sent_at 所在时段：校准值 = 原始值 * 2 + 1
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "temperature",
        "2026-09-01T00:00:00Z", "2026-09-22T08:01:00Z",
        offset=1.0, scale=2.0,
    )).status_code == 201

    params = {
        "metric": "temperature",
        "from": "2026-09-22T08:00:00Z",
        "to": "2026-09-22T08:02:00Z",
        "window_seconds": 60,
    }
    # 默认（或显式 calibrated=false）：原始值
    raw = rollup(client, device, **params).json()
    assert raw["calibrated"] is False
    assert [w["mean"] for w in raw["windows"]] == [10.0, 20.0]
    assert rollup(client, device, calibrated=False, **params).json()["windows"][0]["mean"] == 10.0

    # 校准模式：包1命中配置 10*2+1=21；包2 sent_at >= valid_to，无配置沿用原始值 20
    body = rollup(client, device, calibrated=True, **params).json()
    assert body["calibrated"] is True
    w0, w1 = body["windows"]
    assert w0["count"] == 1 and w0["mean"] == 21.0
    assert w0["min"] == 21.0 and w0["max"] == 21.0
    assert w1["count"] == 1 and w1["mean"] == 20.0


def test_rollup_calibrated_half_open_boundary_uses_packet_sent_at(client):
    device = "dev-c2"
    # 两段相接的配置：[08:00, 08:01) scale=2；[08:01, ...) scale=3
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "temperature",
        "2026-09-22T08:00:00Z", "2026-09-22T08:01:00Z", scale=2.0,
    )).status_code == 201
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "temperature", "2026-09-22T08:01:00Z", scale=3.0,
    )).status_code == 201

    client.post("/api/v1/packages", json=pkg(device, 1, "c2-1", 10.0, sent_at="2026-09-22T08:00:59Z"))
    client.post("/api/v1/packages", json=pkg(device, 2, "c2-2", 10.0, sent_at="2026-09-22T08:01:00Z"))

    body = rollup(
        client, device, calibrated=True, metric="temperature",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:02:00Z"},
        window_seconds=60,
    ).json()
    # 边界按包的 sent_at：08:00:59 命中首段（20），08:01:00 命中次段（30）
    assert [w["mean"] for w in body["windows"]] == [20.0, 30.0]


def test_rollup_calibrated_without_config_falls_back_to_raw(client):
    device = "dev-c3"
    client.post("/api/v1/packages", json=pkg(device, 1, "c3-1", 5.5, sent_at="2026-09-22T08:00:10Z"))
    # 配置在包 sent_at 之后才生效：包时刻无配置，沿用原始值
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "temperature", "2026-10-01T00:00:00Z", scale=2.0, offset=1.0,
    )).status_code == 201
    # 同设备另一指标的配置不影响本指标
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "ph", "2026-09-01T00:00:00Z", scale=10.0,
    )).status_code == 201

    body = rollup(
        client, device, calibrated=True, metric="temperature",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:01:00Z"},
        window_seconds=60,
    ).json()
    assert body["windows"][0]["count"] == 1
    assert body["windows"][0]["mean"] == 5.5


def test_rollup_calibrated_late_samples_remain_late_only(client):
    device = "dev-c4"
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "temperature", "2026-09-01T00:00:00Z", scale=2.0, offset=1.0,
    )).status_code == 201
    # 事件时间较新的包先提交
    client.post("/api/v1/packages", json=pkg(device, 1, "c4-1", 100.0, sent_at="2026-09-22T08:10:00Z"))
    # 迟到包：sent_at 08:00:10 < 水位（08:10:00 - 300s）
    client.post("/api/v1/packages", json=pkg(device, 2, "c4-2", 1.0, sent_at="2026-09-22T08:00:10Z"))

    params = {
        "metric": "temperature",
        "from": "2026-09-22T08:00:00Z",
        "to": "2026-09-22T08:20:00Z",
        "window_seconds": 60,
        "allowed_lateness": 300,
    }
    raw = rollup(client, device, **params).json()
    cal = rollup(client, device, calibrated=True, **params).json()

    # 迟到样本在两种模式下都只计入 late_count
    assert raw["late_count"] == 1 and cal["late_count"] == 1
    # 迟到样本所属窗口保持为空，校准值（1*2+1=3）不入窗、不改写统计
    assert cal["windows"][0]["count"] == 0
    # 非迟到样本按校准值统计：100*2+1=201
    nonempty = [w for w in cal["windows"] if w["count"] > 0]
    assert len(nonempty) == 1 and nonempty[0]["mean"] == 201.0
    # 原始模式对照
    assert [w for w in raw["windows"] if w["count"] > 0][0]["mean"] == 100.0


def test_rollup_calibrated_applies_per_packet_to_multi_measurements(client):
    device = "dev-c5"
    # 同一包内该指标有多个测量值；另一包时刻落在另一段配置
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "ph",
        "2026-09-22T08:00:00Z", "2026-09-22T08:01:00Z",
        scale=1.0, offset=0.5,
    )).status_code == 201
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "ph", "2026-09-22T08:01:00Z", scale=2.0,
    )).status_code == 201
    client.post("/api/v1/packages", json=pkg(
        device, 1, "c5-1", sent_at="2026-09-22T08:00:30Z",
        measurements=[{"metric": "ph", "value": 7.0}, {"metric": "ph", "value": 7.2}],
    ))
    client.post("/api/v1/packages", json=pkg(device, 2, "c5-2", metric="ph", value=7.0,
                                             sent_at="2026-09-22T08:01:30Z"))

    body = rollup(
        client, device, calibrated=True, metric="ph",
        **{"from": "2026-09-22T08:00:00Z", "to": "2026-09-22T08:02:00Z"},
        window_seconds=60,
    ).json()
    w0, w1 = body["windows"]
    # 包1两个测量值同按首段校准：7.5 与 7.7
    assert w0["count"] == 2
    assert w0["min"] == 7.5 and w0["max"] == 7.7
    assert w0["mean"] == pytest.approx(7.6)
    # 包2按次段校准：7.0*2=14.0
    assert w1["mean"] == 14.0


def test_rollup_calibrated_mode_does_not_touch_stored_raw_values(client):
    device = "dev-c6"
    client.post("/api/v1/packages", json=pkg(device, 1, "c6-1", 10.0, sent_at="2026-09-22T08:00:10Z"))
    assert client.post("/api/v1/calibrations", json=calibration(
        device, "temperature", "2026-09-01T00:00:00Z", scale=2.0, offset=1.0,
    )).status_code == 201

    # 校准查询可重复执行，结果一致
    params = {
        "metric": "temperature",
        "from": "2026-09-22T08:00:00Z",
        "to": "2026-09-22T08:01:00Z",
        "window_seconds": 60,
    }
    first = rollup(client, device, calibrated=True, **params).json()
    second = rollup(client, device, calibrated=True, **params).json()
    assert first["windows"][0]["mean"] == second["windows"][0]["mean"] == 21.0

    # 归档导出仍是原始值
    export = client.post("/api/v1/exports", json={"device_id": device}).json()
    page = client.get(f"/api/v1/exports/{export['export_id']}/pages")
    line = json.loads(page.text.splitlines()[0])
    assert line["measurements"][0]["value"] == 10.0
