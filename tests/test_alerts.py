"""持久化阈值告警模块的测试：规则 CRUD、迟滞状态机、
乱序按提交顺序评估、重复包不产生重复事件、告警/事件查询。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(device, seq, key, measurements, sent_at=None):
    body = {
        "device_id": device,
        "seq": seq,
        "idempotency_key": key,
        "measurements": measurements,
    }
    if sent_at is not None:
        body["sent_at"] = sent_at
    return body


def m(metric, value, ts=None):
    d = {"metric": metric, "value": value}
    if ts is not None:
        d["ts"] = ts
    return d


def make_rule(client, device, metric, **over):
    body = {"device_id": device, "metric": metric, "upper_limit": 30.0}
    body.update(over)
    r = client.post("/api/v1/rules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def events(client, device, **params):
    return client.get(f"/api/v1/devices/{device}/alerts/events", params=params).json()


def get_alerts(client, device, **params):
    params.setdefault("device_id", device)
    return client.get("/api/v1/alerts", params=params).json()


# ---------------------------------------------------------------------------
# 规则 CRUD
# ---------------------------------------------------------------------------

def test_rule_create_list_get_validation(client):
    rule = make_rule(client, "dev-crud", "temp", lower_limit=0.0, trigger_count=2,
                     recovery_count=3, enabled=False)
    assert rule["id"] > 0
    assert rule["upper_limit"] == 30.0
    assert rule["lower_limit"] == 0.0
    assert rule["trigger_count"] == 2
    assert rule["recovery_count"] == 3
    assert rule["enabled"] is False

    # 同 设备+指标 重复 -> 409
    r = client.post("/api/v1/rules", json={"device_id": "dev-crud", "metric": "temp",
                                           "upper_limit": 40})
    assert r.status_code == 409

    # 详情
    got = client.get(f"/api/v1/rules/{rule['id']}")
    assert got.status_code == 200 and got.json()["metric"] == "temp"
    assert client.get("/api/v1/rules/99999").status_code == 404

    # 列表 + 过滤
    make_rule(client, "dev-crud", "ph", upper_limit=8.0)
    make_rule(client, "dev-other", "temp", upper_limit=30.0)
    all_rules = client.get("/api/v1/rules").json()
    assert all_rules["count"] == 3
    by_dev = client.get("/api/v1/rules", params={"device_id": "dev-crud"}).json()
    assert {r["metric"] for r in by_dev["rules"]} == {"temp", "ph"}
    by_both = client.get("/api/v1/rules",
                         params={"device_id": "dev-crud", "metric": "ph"}).json()
    assert by_both["count"] == 1

    # 参数校验
    assert client.post("/api/v1/rules", json={
        "device_id": "dev-crud", "metric": "nope"}).status_code == 422  # 无上下限
    assert client.post("/api/v1/rules", json={
        "device_id": "dev-crud", "metric": "bad",
        "upper_limit": 1, "lower_limit": 2}).status_code == 422          # 下限>上限
    assert client.post("/api/v1/rules", json={
        "device_id": "dev-crud", "metric": "bad2",
        "upper_limit": 1, "trigger_count": 0}).status_code == 422       # 次数非法


def test_rule_update_and_delete(client):
    rule = make_rule(client, "dev-upd", "temp", upper_limit=30)
    r = client.put(f"/api/v1/rules/{rule['id']}", json={
        "upper_limit": 40, "lower_limit": 5, "trigger_count": 3,
        "recovery_count": 2, "enabled": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert (body["upper_limit"], body["lower_limit"], body["trigger_count"],
            body["recovery_count"], body["enabled"]) == (40.0, 5.0, 3, 2, False)
    # 更新不存在的规则
    assert client.put("/api/v1/rules/99999",
                      json={"upper_limit": 40}).status_code == 404
    # 更新为非法配置（全量替换语义：下限大于上限）
    assert client.put(f"/api/v1/rules/{rule['id']}", json={
        "upper_limit": 1, "lower_limit": 2, "trigger_count": 1,
        "recovery_count": 1, "enabled": True,
    }).status_code == 422
    # 删除
    assert client.delete(f"/api/v1/rules/{rule['id']}").status_code == 204
    assert client.delete(f"/api/v1/rules/{rule['id']}").status_code == 404


# ---------------------------------------------------------------------------
# 迟滞状态机：连续触发 / 恢复、去抖
# ---------------------------------------------------------------------------

def test_trigger_and_recover_with_consecutive_counts(client):
    # 连续 2 次越界才触发，告警后连续 2 次回到范围内才恢复
    make_rule(client, "dev-sm", "temp", upper_limit=30,
              trigger_count=2, recovery_count=2)

    def send(seq, v, ts=None):
        return client.post("/api/v1/packages",
                           json=pkg("dev-sm", seq, f"sm-{seq}", [m("temp", v, ts)]))

    send(1, 31, "2026-09-22T08:00:01Z")  # 越界 streak=1，不触发
    assert get_alerts(client, "dev-sm")["count"] == 0
    send(2, 20)                           # 回到范围 -> streak 清零（去抖）
    send(3, 32)                           # 越界 streak=1
    send(4, 33, "2026-09-22T08:00:04Z")  # streak=2 -> triggered
    alerts = get_alerts(client, "dev-sm")
    assert alerts["count"] == 1
    a = alerts["alerts"][0]
    assert a["triggered_seq"] == 4
    assert a["triggered_at"] == "2026-09-22T08:00:04Z"
    assert a["last_value"] == 33.0 and a["last_seq"] == 4

    send(5, 29)                           # 告警中：不越界 recovery=1
    send(6, 35)                           # 又越界 -> recovery 清零，保持告警
    assert get_alerts(client, "dev-sm")["count"] == 1
    send(7, 28)                           # recovery=1
    send(8, 27, "2026-09-22T08:00:08Z")  # recovery=2 -> recovered
    assert get_alerts(client, "dev-sm")["count"] == 0

    ev = events(client, "dev-sm")
    assert ev["count"] == 2
    t, rc = ev["events"]
    assert (t["event_type"], t["seq"], t["value"]) == ("triggered", 4, 33.0)
    assert (rc["event_type"], rc["seq"], rc["value"]) == ("recovered", 8, 27.0)
    assert t["id"] < rc["id"]             # 事件序号按提交顺序递增
    assert t["event_time"] == "2026-09-22T08:00:04Z"
    assert rc["event_time"] == "2026-09-22T08:00:08Z"


def test_boundary_values_are_not_breaches(client):
    make_rule(client, "dev-bnd", "temp", upper_limit=30, lower_limit=10,
              trigger_count=1, recovery_count=1)
    for seq, v in [(1, 30.0), (2, 10.0)]:  # 恰等于上下限：不越界
        client.post("/api/v1/packages",
                    json=pkg("dev-bnd", seq, f"bnd-{seq}", [m("temp", v)]))
    assert events(client, "dev-bnd")["count"] == 0


def test_lower_limit_trigger_and_recover(client):
    make_rule(client, "dev-low", "temp", lower_limit=10,
              trigger_count=1, recovery_count=1)
    client.post("/api/v1/packages",
                json=pkg("dev-low", 1, "low-1", [m("temp", 9.0)]))
    alerts = client.get("/api/v1/alerts", params={"device_id": "dev-low"}).json()
    assert alerts["count"] == 1 and alerts["alerts"][0]["lower_limit"] == 10.0
    client.post("/api/v1/packages",
                json=pkg("dev-low", 2, "low-2", [m("temp", 11.0)]))
    assert client.get("/api/v1/alerts", params={"device_id": "dev-low"}).json()["count"] == 0
    ev = events(client, "dev-low")["events"]
    assert [e["event_type"] for e in ev] == ["triggered", "recovered"]


def test_missing_metric_keeps_streak_and_does_not_recover(client):
    make_rule(client, "dev-miss", "temp", upper_limit=30,
              trigger_count=2, recovery_count=1)
    def send(seq, metrics):
        client.post("/api/v1/packages",
                    json=pkg("dev-miss", seq, f"miss-{seq}", metrics))
    send(1, [m("temp", 31)])              # streak=1
    send(2, [m("ph", 7.0)])               # 没有 temp：streak 不变，不触发也不恢复
    assert events(client, "dev-miss")["count"] == 0
    send(3, [m("temp", 32)])              # streak=2 -> 触发
    assert client.get("/api/v1/alerts", params={"device_id": "dev-miss"}).json()["count"] == 1
    send(4, [m("ph", 7.0)])               # 告警中缺数：不算恢复
    assert client.get("/api/v1/alerts", params={"device_id": "dev-miss"}).json()["count"] == 1
    send(5, [m("temp", 20)])              # 真正回到范围 -> 恢复
    assert client.get("/api/v1/alerts", params={"device_id": "dev-miss"}).json()["count"] == 0


def test_multiple_measurements_in_one_package_evaluated_in_order(client):
    # trigger_count=1 / recovery_count=1：同一包内先超上限触发、再回限内恢复
    make_rule(client, "dev-multi", "temp", upper_limit=30,
              trigger_count=1, recovery_count=1)
    client.post("/api/v1/packages", json=pkg(
        "dev-multi", 1, "multi-1",
        [m("temp", 35, "2026-09-22T08:00:05Z"),
         m("temp", 20, "2026-09-22T08:00:06Z")]))
    ev = events(client, "dev-multi")["events"]
    assert [(e["seq"], e["event_type"], e["value"], e["event_time"]) for e in ev] == [
        (1, "triggered", 35.0, "2026-09-22T08:00:05Z"),
        (1, "recovered", 20.0, "2026-09-22T08:00:06Z"),
    ]
    assert get_alerts(client, "dev-multi")["count"] == 0


# ---------------------------------------------------------------------------
# 乱序 / 幂等：按提交顺序评估，重复不产生事件
# ---------------------------------------------------------------------------

def test_buffered_packages_not_evaluated_until_gap_filled(client):
    make_rule(client, "dev-ooo", "temp", upper_limit=30,
              trigger_count=1, recovery_count=1)
    # seq1 首包自举提交（值在范围内，无事件）
    client.post("/api/v1/packages",
                json=pkg("dev-ooo", 1, "ooo-1", [m("temp", 20)]))
    # seq3 先到，进入缓冲：不评估、无事件
    r = client.post("/api/v1/packages",
                    json=pkg("dev-ooo", 3, "ooo-3", [m("temp", 99)]))
    assert r.json()["status"] == "buffered"
    assert events(client, "dev-ooo")["count"] == 0
    assert get_alerts(client, "dev-ooo")["count"] == 0
    # seq2 补齐 -> 按提交顺序 2(正常)、3(越界) 各评估一次
    client.post("/api/v1/packages",
                json=pkg("dev-ooo", 2, "ooo-2", [m("temp", 20)]))
    ev = events(client, "dev-ooo")["events"]
    assert [(e["seq"], e["event_type"]) for e in ev] == [(3, "triggered")]


def test_duplicate_replay_does_not_reemit_events(client):
    make_rule(client, "dev-dup", "temp", upper_limit=30,
              trigger_count=1, recovery_count=1)
    p1 = pkg("dev-dup", 1, "dup-1", [m("temp", 31)])
    client.post("/api/v1/packages", json=p1)
    assert events(client, "dev-dup")["count"] == 1
    # 幂等重放：不重复评估
    replay = client.post("/api/v1/packages", json=p1).json()
    assert replay["status"] == "duplicate"
    assert replay["duplicate_reason"] == "idempotent_replay"
    assert events(client, "dev-dup")["count"] == 1
    # 换键重发已提交序号（already_committed）同样不产生事件
    client.post("/api/v1/packages",
                json=pkg("dev-dup", 1, "dup-1-again", [m("temp", 31)]))
    assert events(client, "dev-dup")["count"] == 1


def test_out_of_order_then_recover_uses_commit_order(client):
    # 到达顺序 1,3,2；提交顺序须为 1(正常) 2(越界) 3(正常)
    make_rule(client, "dev-ord", "temp", upper_limit=30,
              trigger_count=1, recovery_count=1)
    client.post("/api/v1/packages",
                json=pkg("dev-ord", 1, "ord-1", [m("temp", 20)]))  # 自举提交
    client.post("/api/v1/packages",
                json=pkg("dev-ord", 3, "ord-3", [m("temp", 20)]))  # 缓冲
    assert events(client, "dev-ord")["count"] == 0
    client.post("/api/v1/packages",
                json=pkg("dev-ord", 2, "ord-2", [m("temp", 31)]))  # 排水 2,3
    ev = events(client, "dev-ord")["events"]
    assert [(e["seq"], e["event_type"]) for e in ev] == [
        (2, "triggered"), (3, "recovered"),
    ]
    assert [e["id"] for e in ev] == sorted(e["id"] for e in ev)


# ---------------------------------------------------------------------------
# 规则生命周期：更新、禁用、删除
# ---------------------------------------------------------------------------

def test_update_keeps_alarm_state_but_resets_streaks(client):
    rule = make_rule(client, "dev-u", "temp", upper_limit=30,
                     trigger_count=2, recovery_count=2)
    def send(seq, v):
        client.post("/api/v1/packages",
                    json=pkg("dev-u", seq, f"u-{seq}", [m("temp", v)]))
    send(1, 31)  # streak=1
    # 更新阈值：连续计数清零，state 仍为 normal
    client.put(f"/api/v1/rules/{rule['id']}",
               json={"upper_limit": 100, "lower_limit": None,
                     "trigger_count": 1, "recovery_count": 1, "enabled": True})
    send(2, 50)  # 新上限 100 下不越界：无事件
    assert events(client, "dev-u")["count"] == 0

    # 告警中更新：保留告警状态（新阈值下仍触发才恢复）
    send(3, 120)  # > 100，trigger_count=1 -> 触发
    assert get_alerts(client, "dev-u")["count"] == 1
    client.put(f"/api/v1/rules/{rule['id']}",
               json={"upper_limit": 200, "lower_limit": None,
                     "trigger_count": 1, "recovery_count": 1, "enabled": True})
    assert get_alerts(client, "dev-u")["count"] == 1  # 状态保留
    send(4, 150)  # 200 以内 -> recovery_count=1 恢复
    assert get_alerts(client, "dev-u")["count"] == 0


def test_disabled_rule_not_evaluated(client):
    rule = make_rule(client, "dev-dis", "temp", upper_limit=30, enabled=False)
    client.post("/api/v1/packages",
                json=pkg("dev-dis", 1, "dis-1", [m("temp", 99)]))
    assert events(client, "dev-dis")["count"] == 0
    # 重新启用后也只对之后的提交生效
    client.put(f"/api/v1/rules/{rule['id']}",
               json={"upper_limit": 30, "lower_limit": None,
                     "trigger_count": 1, "recovery_count": 1, "enabled": True})
    client.post("/api/v1/packages",
                json=pkg("dev-dis", 2, "dis-2", [m("temp", 99)]))
    assert events(client, "dev-dis")["count"] == 1


def test_rule_created_after_data_does_not_evaluate_history(client):
    client.post("/api/v1/packages",
                json=pkg("dev-late-rule", 1, "lr-1", [m("temp", 99)]))
    make_rule(client, "dev-late-rule", "temp", upper_limit=30)  # 历史不回溯
    assert events(client, "dev-late-rule")["count"] == 0
    client.post("/api/v1/packages",
                json=pkg("dev-late-rule", 2, "lr-2", [m("temp", 99)]))
    assert events(client, "dev-late-rule")["count"] == 1


def test_delete_rule_removes_alert_but_keeps_events(client):
    rule = make_rule(client, "dev-del", "temp", upper_limit=30)
    client.post("/api/v1/packages",
                json=pkg("dev-del", 1, "del-1", [m("temp", 99)]))
    assert get_alerts(client, "dev-del")["count"] == 1
    assert client.delete(f"/api/v1/rules/{rule['id']}").status_code == 204
    # 当前告警随状态机级联删除
    assert get_alerts(client, "dev-del")["count"] == 0
    # 历史事件保留，rule_id 置空
    ev = events(client, "dev-del")
    assert ev["count"] == 1
    assert ev["events"][0]["rule_id"] is None
    assert ev["events"][0]["device_id"] == "dev-del"
    # 再上报也不再评估
    client.post("/api/v1/packages",
                json=pkg("dev-del", 2, "del-2", [m("temp", 99)]))
    assert events(client, "dev-del")["count"] == 1


# ---------------------------------------------------------------------------
# 查询：分页、过滤、404
# ---------------------------------------------------------------------------

def test_events_pagination_and_filters(client):
    make_rule(client, "dev-pg", "temp", upper_limit=30,
              trigger_count=1, recovery_count=1)
    make_rule(client, "dev-pg", "ph", upper_limit=8,
              trigger_count=1, recovery_count=1)
    seq = 0
    for temp, ph in [(31, 7.0), (20, 9.0), (32, 7.0)]:
        seq += 1
        client.post("/api/v1/packages", json=pkg(
            "dev-pg", seq, f"pg-{seq}", [m("temp", temp), m("ph", ph)]))
    # temp: triggered(1) recovered(2) triggered(3)；ph: triggered(2) recovered(3)
    all_ev = events(client, "dev-pg", limit=100)
    assert all_ev["count"] == 5
    temp_only = events(client, "dev-pg", metric="temp")
    assert [(e["metric"], e["event_type"]) for e in temp_only["events"]] == [
        ("temp", "triggered"), ("temp", "recovered"), ("temp", "triggered"),
    ]
    trig = events(client, "dev-pg", event_type="triggered")
    assert trig["count"] == 3 and all(e["event_type"] == "triggered" for e in trig["events"])
    # 分页
    page1 = events(client, "dev-pg", limit=2, offset=0)
    page2 = events(client, "dev-pg", limit=2, offset=2)
    ids = [e["id"] for e in all_ev["events"]]
    assert [e["id"] for e in page1["events"]] == ids[:2]
    assert [e["id"] for e in page2["events"]] == ids[2:4]
    # 非法 event_type -> 422
    r = client.get("/api/v1/devices/dev-pg/alerts/events",
                   params={"event_type": "bogus"})
    assert r.status_code == 422


def test_alert_events_unknown_device_404(client):
    assert client.get("/api/v1/devices/nope/alerts/events").status_code == 404


def test_events_endpoint_includes_time_fields(client):
    make_rule(client, "dev-time", "temp", upper_limit=30,
              trigger_count=1, recovery_count=1)
    # 无 ts 但有包 sent_at：事件时间取 sent_at
    client.post("/api/v1/packages", json=pkg(
        "dev-time", 1, "time-1", [m("temp", 31)],
        sent_at="2026-09-22T08:00:00Z"))
    e = events(client, "dev-time")["events"][0]
    assert e["event_time"] == "2026-09-22T08:00:00Z"
    assert e["created_at"]  # 落库时间始终存在
