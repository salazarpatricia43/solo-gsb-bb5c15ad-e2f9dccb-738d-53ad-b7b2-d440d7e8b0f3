"""双指标配比验收模块的测试：配置校验、包内配组、无效原因与整体结论。"""

import itertools

from fastapi.testclient import TestClient
import pytest

from app.main import app


# 数据库文件在测试进程内共享，逻辑设备名按用例加唯一后缀隔离
_counter = itertools.count()
_suffix = {"v": 0}


@pytest.fixture(autouse=True)
def _unique_device_suffix():
    _suffix["v"] = next(_counter)


def _dev(name="dev"):
    return f"{name}-{_suffix['v']}"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def mm(metric, value, unit=None):
    d = {"metric": metric, "value": value}
    if unit is not None:
        d["unit"] = unit
    return d


def post_pkg(client, seq, measurements, device="dev", key=None):
    device = _dev(device)
    r = client.post("/api/v1/packages", json={
        "device_id": device,
        "seq": seq,
        "idempotency_key": key or f"{device}-k{seq}",
        "measurements": measurements,
    })
    assert r.status_code == 200, r.text
    return r.json()


def create_cfg(client, device="dev", **overrides):
    body = {
        "device_id": _dev(device),
        "numerator_metric": "a",
        "denominator_metric": "b",
        "numerator_unit": "U",
        "denominator_unit": "V",
        "ratio_lower": 0.9,
        "ratio_upper": 1.1,
        "min_valid_groups": 1,
    }
    body.update(overrides)
    return client.post("/api/v1/ratio-acceptances", json=body)


def evaluate(client, from_seq, to_seq, device="dev"):
    return client.get(
        f"/api/v1/devices/{_dev(device)}/ratio-acceptance/evaluate",
        params={"from_seq": from_seq, "to_seq": to_seq},
    )


# ---------------------------------------------------------------------------
# 配置创建与校验
# ---------------------------------------------------------------------------

def test_create_config_ok_and_echoes_fields(client):
    r = create_cfg(client, min_valid_groups=3, ratio_lower=0.5, ratio_upper=2.0)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["device_id"] == _dev()
    assert body["numerator_metric"] == "a"
    assert body["denominator_metric"] == "b"
    assert body["numerator_unit"] == "U"
    assert body["denominator_unit"] == "V"
    assert body["ratio_lower"] == 0.5
    assert body["ratio_upper"] == 2.0
    assert body["min_valid_groups"] == 3
    assert body["id"] >= 1
    assert body["created_at"] and body["updated_at"]


@pytest.mark.parametrize("field,value", [
    ("numerator_metric", "b"),       # 两种指标相同
    ("ratio_lower", 1.2),            # 下限大于上限
    ("min_valid_groups", 0),         # 最低有效组数非正
])
def test_create_config_validation_rejected(client, field, value):
    r = create_cfg(client, **{field: value})
    assert r.status_code == 422, r.text


def test_create_config_equal_limits_ok(client):
    # 下限等于上限合法（闭区间退化为单点）
    r = create_cfg(client, ratio_lower=1.0, ratio_upper=1.0)
    assert r.status_code == 201, r.text


def test_create_config_requires_units(client):
    r = client.post("/api/v1/ratio-acceptances", json={
        "device_id": _dev(), "numerator_metric": "a", "denominator_metric": "b",
        "numerator_unit": "U",  # 缺 denominator_unit
        "ratio_lower": 0.9, "ratio_upper": 1.1, "min_valid_groups": 1,
    })
    assert r.status_code == 422


def test_duplicate_config_per_device_conflict(client):
    assert create_cfg(client).status_code == 201
    r = create_cfg(client, min_valid_groups=5)
    assert r.status_code == 409, r.text


def test_get_and_list_configs(client):
    create_cfg(client)
    r = client.get(f"/api/v1/devices/{_dev()}/ratio-acceptance")
    assert r.status_code == 200
    assert r.json()["numerator_metric"] == "a"

    assert client.get("/api/v1/devices/ghost-device/ratio-acceptance").status_code == 404
    assert client.get("/api/v1/ratio-acceptances").json()  # 全表非空
    assert (
        client.get("/api/v1/ratio-acceptances",
                   params={"device_id": _dev()}).json()[0]["device_id"]
        == _dev()
    )


def test_evaluate_unknown_device_404(client):
    r = client.get("/api/v1/devices/ghost/ratio-acceptance/evaluate",
                   params={"from_seq": 1, "to_seq": 2})
    assert r.status_code == 404


def test_evaluate_without_config_404(client):
    post_pkg(client, 1, [mm("a", 1, "U"), mm("b", 1, "V")])
    r = evaluate(client, 1, 1)
    assert r.status_code == 404
    assert "配比验收配置" in r.json()["detail"]


@pytest.mark.parametrize("from_seq,to_seq", [(0, 1), (1, 0), (2, 1)])
def test_evaluate_bad_range_422(client, from_seq, to_seq):
    post_pkg(client, 1, [mm("a", 1, "U"), mm("b", 1, "V")])
    create_cfg(client)
    r = evaluate(client, from_seq, to_seq)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 包内一对一配组、禁止跨包
# ---------------------------------------------------------------------------

def test_pairing_one_to_one_by_occurrence_order_within_package(client):
    # 包内顺序交错，配组按各指标"各自出现顺序"：
    # a 序列 = [pos0(10), pos2(20), pos4(30)]；b 序列 = [pos1(10), pos3(10)]
    post_pkg(client, 1, [
        mm("a", 10.0, "U"), mm("b", 10.0, "V"),
        mm("a", 20.0, "U"), mm("b", 10.0, "V"),
        mm("a", 30.0, "U"),
    ])
    create_cfg(client, min_valid_groups=2)
    body = evaluate(client, 1, 1).json()
    assert body["package_count"] == 1
    assert body["group_count"] == 3
    g0, g1, g2 = body["groups"]
    assert (g0["index"], g0["seq"], g0["position"]) == (1, 1, 0)
    assert (g0["numerator"]["position"], g0["denominator"]["position"]) == (0, 1)
    assert g0["ratio"] == pytest.approx(1.0) and g0["conclusion"] == "pass"
    assert (g1["numerator"]["position"], g1["denominator"]["position"]) == (2, 3)
    assert g1["ratio"] == pytest.approx(2.0) and g1["conclusion"] == "fail"
    # 第 3 个分子读数无对应分母（包内残余，不与后续包配组）
    assert g2["position"] == 2
    assert g2["numerator"]["position"] == 4 and g2["denominator"] is None
    assert g2["valid"] is False
    assert g2["invalid_reason"] == "missing_denominator"
    assert "分母" in g2["invalid_reason_detail"]
    assert body["valid_group_count"] == 2
    assert body["failed_group_count"] == 1
    assert body["invalid_group_count"] == 1
    assert body["accepted"] is False  # 存在比例超界的有效组


def test_no_cross_package_pairing(client):
    # 包 1 只有分母（残余 -> 缺分子），包 2 只有分子（残余 -> 缺分母），
    # 二者不得跨包配成一组
    post_pkg(client, 1, [mm("b", 10.0, "V")])
    post_pkg(client, 2, [mm("a", 10.0, "U")])
    create_cfg(client)
    body = evaluate(client, 1, 2).json()
    assert body["group_count"] == 2
    assert body["valid_group_count"] == 0
    assert [g["invalid_reason"] for g in body["groups"]] == [
        "missing_numerator", "missing_denominator"
    ]
    assert [g["seq"] for g in body["groups"]] == [1, 2]


def test_group_index_is_continuous_across_packages(client):
    post_pkg(client, 1, [mm("a", 10, "U"), mm("b", 10, "V"),
                         mm("a", 11, "U"), mm("b", 10, "V")])
    post_pkg(client, 2, [mm("a", 10, "U"), mm("b", 10, "V")])
    post_pkg(client, 3, [mm("x", 1), mm("y", 2)])  # 两种指标都不出现
    create_cfg(client, min_valid_groups=3)
    body = evaluate(client, 1, 3).json()
    assert body["package_count"] == 3  # 不含指标的包仍被扫描
    assert [g["index"] for g in body["groups"]] == [1, 2, 3]
    assert [g["seq"] for g in body["groups"]] == [1, 1, 2]
    assert [g["position"] for g in body["groups"]] == [0, 1, 0]
    assert body["accepted"] is True


# ---------------------------------------------------------------------------
# 无效原因：缺读数 / 单位不符 / 分母为零
# ---------------------------------------------------------------------------

def test_invalid_reasons_and_priority(client):
    post_pkg(client, 1, [mm("a", 10.0, "U"), mm("b", 10.0, "V")])   # 有效
    post_pkg(client, 2, [mm("a", 10.0, "U")])                        # 缺分母
    post_pkg(client, 3, [mm("b", 10.0, "V")])                        # 缺分子
    post_pkg(client, 4, [mm("a", 10.0, "X"), mm("b", 10.0, "V")])   # 分子单位不符
    post_pkg(client, 5, [mm("a", 10.0, "U"), mm("b", 10.0, "Y")])   # 分母单位不符
    post_pkg(client, 6, [mm("a", 10.0, "U"), mm("b", 10.0)])        # 读数缺单位
    post_pkg(client, 7, [mm("a", 10.0, "U"), mm("b", 0.0, "V")])    # 分母为零
    create_cfg(client, min_valid_groups=1)
    body = evaluate(client, 1, 7).json()
    groups = body["groups"]
    assert [g["invalid_reason"] for g in groups] == [
        None,
        "missing_denominator",
        "missing_numerator",
        "unit_mismatch",
        "unit_mismatch",
        "unit_mismatch",
        "zero_denominator",
    ]
    assert all(g["ratio"] is None for g in groups[1:])
    assert groups[0]["valid"] is True and groups[0]["ratio"] == 1.0
    # 缺单位一侧 unit_matches=False 且 unit 为 null
    assert groups[5]["denominator"]["unit"] is None
    assert groups[5]["denominator"]["unit_matches"] is False
    # 分母为零优先于比例计算（不抛异常），分子侧信息仍返回
    assert groups[6]["numerator"]["value"] == 10.0
    assert groups[6]["denominator"]["value"] == 0.0
    assert "为零" in groups[6]["invalid_reason_detail"]
    assert body["valid_group_count"] == 1
    assert body["invalid_group_count"] == 6


def test_unit_mismatch_detail_names_side_and_units(client):
    post_pkg(client, 1, [mm("a", 1.0, "C"), mm("b", 1.0, "V")])
    create_cfg(client)
    detail = evaluate(client, 1, 1).json()["groups"][0]["invalid_reason_detail"]
    assert "分子" in detail and "'C'" in detail and "'U'" in detail


# ---------------------------------------------------------------------------
# 闭区间边界与整体验收
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("av,passed", [(0.9, True), (1.1, True),
                                       (0.899999, False), (1.100001, False)])
def test_ratio_limits_are_closed_interval(client, av, passed):
    post_pkg(client, 1, [mm("a", av, "U"), mm("b", 1.0, "V")])
    create_cfg(client, ratio_lower=0.9, ratio_upper=1.1)
    g = evaluate(client, 1, 1).json()["groups"][0]
    assert g["conclusion"] == ("pass" if passed else "fail")


def test_accepted_requires_min_valid_groups(client):
    post_pkg(client, 1, [mm("a", 10, "U"), mm("b", 10, "V")])  # 1 个有效组
    create_cfg(client, min_valid_groups=2)
    body = evaluate(client, 1, 1).json()
    assert body["valid_group_count"] == 1
    assert body["accepted"] is False
    assert "最低要求" in body["not_accepted_reason"]
    assert "2" in body["not_accepted_reason"]

    post_pkg(client, 2, [mm("a", 10, "U"), mm("b", 10, "V")])  # 第 2 个有效组
    body = evaluate(client, 1, 2).json()
    assert body["valid_group_count"] == 2
    assert body["accepted"] is True
    assert body["not_accepted_reason"] is None


def test_invalid_groups_do_not_count_toward_minimum(client):
    post_pkg(client, 1, [mm("a", 10, "U"), mm("b", 10, "V")])
    post_pkg(client, 2, [mm("a", 10, "U"), mm("b", 0, "V")])  # 无效
    create_cfg(client, min_valid_groups=2)
    body = evaluate(client, 1, 2).json()
    assert body["group_count"] == 2 and body["valid_group_count"] == 1
    assert body["accepted"] is False


def test_empty_range_is_not_accepted_but_returns_200(client):
    post_pkg(client, 1, [mm("a", 10, "U"), mm("b", 10, "V")])
    create_cfg(client)
    body = evaluate(client, 100, 200).json()
    assert body["groups"] == []
    assert body["package_count"] == 0 and body["group_count"] == 0
    assert body["accepted"] is False
    assert body["not_accepted_reason"] == "有效组数 0 未达到最低要求 1"


# ---------------------------------------------------------------------------
# 仅已提交包参与：缓冲包与作废序号
# ---------------------------------------------------------------------------

def test_buffered_packages_excluded(client):
    post_pkg(client, 1, [mm("a", 10, "U"), mm("b", 10, "V")])
    post_pkg(client, 3, [mm("a", 99, "U"), mm("b", 1, "V")])  # 缺口 -> 缓冲
    create_cfg(client, min_valid_groups=1)
    body = evaluate(client, 1, 3).json()
    assert body["package_count"] == 1
    assert [g["seq"] for g in body["groups"]] == [1]
    assert body["accepted"] is True

    # 补齐缺口排水提交后，seq 3 进入验收
    post_pkg(client, 2, [mm("a", 10, "U"), mm("b", 10, "V")])
    body = evaluate(client, 1, 3).json()
    assert body["package_count"] == 3
    assert [g["seq"] for g in body["groups"]] == [1, 2, 3]


def test_voided_seqs_excluded_after_drain(client):
    post_pkg(client, 1, [mm("a", 10, "U"), mm("b", 10, "V")])
    post_pkg(client, 3, [mm("a", 10, "U"), mm("b", 10, "V")])  # 缓冲
    r = client.post(f"/api/v1/devices/{_dev()}/voids", json={
        "from_seq": 2, "to_seq": 2, "idempotency_key": "void-2",
    })
    assert r.status_code == 200, r.text
    create_cfg(client, min_valid_groups=2)
    body = evaluate(client, 1, 3).json()
    # 作废序号 2 不出现在结果中；真实包 1、3 各一组
    assert [g["seq"] for g in body["groups"]] == [1, 3]
    assert body["package_count"] == 2
    assert body["accepted"] is True


def test_range_is_closed_interval(client):
    for seq in range(1, 4):
        post_pkg(client, seq, [mm("a", 10, "U"), mm("b", 10, "V")])
    create_cfg(client)
    body = evaluate(client, 2, 2).json()
    assert [g["seq"] for g in body["groups"]] == [2]


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------

def test_evaluate_is_read_only_and_repeatable(client):
    post_pkg(client, 1, [mm("a", 10, "U"), mm("b", 0, "V")])
    create_cfg(client)
    b1 = evaluate(client, 1, 1).json()
    b2 = evaluate(client, 1, 1).json()
    assert b1 == b2
    assert client.get(f"/api/v1/devices/{_dev()}/committed").json()["seqs"] == [1]
    assert client.get(f"/api/v1/devices/{_dev()}/gaps").json()["gaps"] == []
