import pytest

from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(device, seq, key, value=1.0):
    return {
        "device_id": device,
        "seq": seq,
        "idempotency_key": key,
        "measurements": [{"metric": "temperature", "value": value, "unit": "C"}],
    }


def test_in_order_commit(client):
    r = client.post("/api/v1/packages", json=pkg("dev-a", 1, "a-1"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "committed"
    assert body["committed_seqs"] == [1]
    assert body["committed_upto"] == 1


def test_out_of_order_buffer_and_drain(client):
    # 首包自举：seq 10 到达即提交
    assert client.post("/api/v1/packages", json=pkg("dev-b", 10, "b-10")).json()["committed_upto"] == 10
    # 乱序：12 先到，进入缓冲
    r = client.post("/api/v1/packages", json=pkg("dev-b", 12, "b-12")).json()
    assert r["status"] == "buffered"
    assert r["committed_upto"] == 10
    # 缺口查询
    gaps = client.get("/api/v1/devices/dev-b/gaps").json()
    assert gaps["gaps"] == [11]
    assert gaps["buffered_seqs"] == [12]
    assert gaps["next_expected"] == 11
    # 补齐缺口 11 -> 11、12 连续提交
    r = client.post("/api/v1/packages", json=pkg("dev-b", 11, "b-11")).json()
    assert r["status"] == "committed"
    assert r["committed_seqs"] == [11, 12]
    assert r["committed_upto"] == 12
    # 缺口清空
    gaps = client.get("/api/v1/devices/dev-b/gaps").json()
    assert gaps["gaps"] == [] and gaps["buffered_seqs"] == []


def test_idempotent_replay(client):
    p = pkg("dev-c", 1, "c-1")
    first = client.post("/api/v1/packages", json=p).json()
    assert first["status"] == "committed"
    replay = client.post("/api/v1/packages", json=p).json()
    assert replay["status"] == "duplicate"
    assert replay["duplicate_reason"] == "idempotent_replay"
    # 重复记录可查
    dups = client.get("/api/v1/devices/dev-c/duplicates").json()
    assert dups["count"] == 1
    assert dups["records"][0]["reason"] == "idempotent_replay"


def test_duplicate_already_committed_and_buffered(client):
    client.post("/api/v1/packages", json=pkg("dev-d", 1, "d-1"))
    client.post("/api/v1/packages", json=pkg("dev-d", 3, "d-3"))  # 缓冲
    # 已提交序号，换个新键再发 -> already_committed
    r = client.post("/api/v1/packages", json=pkg("dev-d", 1, "d-1-new")).json()
    assert r["status"] == "duplicate" and r["duplicate_reason"] == "already_committed"
    # 缓冲区中序号，换个新键再发 -> already_buffered
    r = client.post("/api/v1/packages", json=pkg("dev-d", 3, "d-3-new")).json()
    assert r["status"] == "duplicate" and r["duplicate_reason"] == "already_buffered"
    dups = client.get("/api/v1/devices/dev-d/duplicates").json()
    assert dups["count"] == 2


def test_same_key_different_payload_conflict(client):
    client.post("/api/v1/packages", json=pkg("dev-e", 1, "e-1"))
    bad = pkg("dev-e", 2, "e-1")  # 同键不同内容
    r = client.post("/api/v1/packages", json=bad)
    assert r.status_code == 409


def test_missing_idempotency_key(client):
    p = pkg("dev-f", 1, "f-1")
    del p["idempotency_key"]
    assert client.post("/api/v1/packages", json=p).status_code == 422
    # 用请求头提供幂等键则成功
    r = client.post("/api/v1/packages", json=p, headers={"Idempotency-Key": "f-1"})
    assert r.status_code == 200


def test_committed_query_and_devices_list(client):
    for i in range(1, 6):
        client.post("/api/v1/packages", json=pkg("dev-g", i, f"g-{i}"))
    r = client.get("/api/v1/devices/dev-g/committed").json()
    assert r["committed_upto"] == 5
    assert r["seqs"] == [1, 2, 3, 4, 5]
    r = client.get("/api/v1/devices/dev-g/committed", params={"from_seq": 2, "to_seq": 4}).json()
    assert r["seqs"] == [2, 3, 4]
    devices = client.get("/api/v1/devices").json()
    g = [d for d in devices if d["device_id"] == "dev-g"][0]
    assert g["committed_count"] == 5 and g["next_expected"] == 6


def test_unknown_device_404(client):
    assert client.get("/api/v1/devices/nope/gaps").status_code == 404
    assert client.get("/api/v1/devices/nope/committed").status_code == 404
    assert client.get("/api/v1/devices/nope/duplicates").status_code == 404
