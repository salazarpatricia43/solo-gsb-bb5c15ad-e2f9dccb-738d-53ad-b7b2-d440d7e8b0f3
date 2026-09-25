"""FastAPI 路由与应用入口。"""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Response

from . import db
from .alerts import (
    AlertRuleConflict,
    AlertRuleError,
    create_rule,
    delete_rule,
    get_rule,
    update_rule,
)
from .calibrations import (
    CalibrationConflict,
    CalibrationError,
    apply_calibration,
    create_calibration,
    resolve_calibration,
)
from .clock_drift import ClockDriftError, diagnose_clock_drift
from . import exports
from .comparisons import ComparisonError, compare_devices
from .exports import ExportError, InvalidCursor
from .exposure import ExposureError, compute_exposure
from .gaps import gap_page
from .logic import IdempotencyConflict, ingest_package
from .ratio_acceptance import (
    RatioAcceptanceConflict,
    RatioAcceptanceError,
    RatioAcceptanceNotFound,
    create_acceptance,
    evaluate as evaluate_ratio,
    get_acceptance,
    list_acceptances,
)
from .rollup import RollupError, compute_rollup
from .schemas import (
    AlertEventsResponse,
    AlertEventOut,
    ClockDriftResponse,
    ComparisonResponse,
    CurrentAlertOut,
    CurrentAlertsResponse,
    CalibrationCreate,
    CalibrationOut,
    CalibrationResolveResponse,
    CommittedResponse,
    DeviceState,
    DuplicatesResponse,
    DuplicateRecord,
    ExposureResponse,
    ExportCreate,
    ExportCreateResponse,
    ExportManifest,
    ExportVerifyRequest,
    ExportVerifyResponse,
    GapsResponse,
    IngestResponse,
    PackageIn,
    RatioAcceptanceCreate,
    RatioAcceptanceOut,
    RatioAcceptanceResponse,
    RollupResponse,
    RuleCreate,
    RuleListResponse,
    RuleOut,
    RuleUpdate,
    VoidCreate,
    VoidRecordOut,
    VoidResponse,
    VoidsResponse,
    WarmupReleaseResponse,
)
from .voids import (
    VoidConflict,
    VoidError,
    list_void_records,
    void_missing,
    voided_count,
)
from .warmup import WarmupError, evaluate_warmup_release


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 容器/进程启动时自动建库建表
    yield


app = FastAPI(
    title="Lab Instrument Ingest API",
    version="1.0.0",
    description="实验室仪器批量测量包接收服务：幂等去重、乱序缓冲、按序提交。",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/packages", response_model=IngestResponse, status_code=200)
def post_package(
    pkg: PackageIn,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    key = pkg.idempotency_key or idempotency_key
    if not key:
        raise HTTPException(
            status_code=422,
            detail="缺少幂等键：请在请求体 idempotency_key 或 Idempotency-Key 请求头中提供",
        )
    with db.ingest_lock, db.get_conn() as conn:
        try:
            return ingest_package(conn, pkg, key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/devices", response_model=List[DeviceState])
def list_devices():
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id,
                   d.committed_upto,
                   (SELECT COUNT(*) FROM buffer       b WHERE b.device_id = d.device_id) AS buffered_count,
                   (SELECT COUNT(*) FROM committed    c WHERE c.device_id = d.device_id) AS committed_count,
                   (SELECT COUNT(*) FROM duplicates   p WHERE p.device_id = d.device_id) AS duplicate_count,
                   (SELECT COALESCE(SUM(v.to_seq - v.from_seq + 1), 0)
                      FROM void_records v WHERE v.device_id = d.device_id) AS voided_count
            FROM devices d ORDER BY d.device_id
            """
        ).fetchall()
    return [
        DeviceState(
            device_id=r["device_id"],
            committed_upto=r["committed_upto"],
            next_expected=r["committed_upto"] + 1,
            buffered_count=r["buffered_count"],
            committed_count=r["committed_count"],
            duplicate_count=r["duplicate_count"],
            voided_count=r["voided_count"],
        )
        for r in rows
    ]


@app.get("/api/v1/devices/{device_id}/gaps", response_model=GapsResponse)
def get_gaps(
    device_id: str,
    limit: int = Query(default=1000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
):
    """缺口查询：按连续缺口区间分页返回缺失序号，不展开缺失序号全集。

    gap_count 为 next_expected 到缓冲区最大序号之间的缺失序号总数；
    gaps 为本页（升序，最多 limit 条）；buffered_seqs 为当前全部缓冲序号。
    """
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT committed_upto FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        committed_upto = dev["committed_upto"]
        buffered = [
            r["seq"]
            for r in conn.execute(
                "SELECT seq FROM buffer WHERE device_id = ? ORDER BY seq", (device_id,)
            ).fetchall()
        ]
    gap_count, gaps = gap_page(committed_upto + 1, buffered, limit, offset)
    return GapsResponse(
        device_id=device_id,
        committed_upto=committed_upto,
        next_expected=committed_upto + 1,
        buffered_seqs=buffered,
        gap_count=gap_count,
        limit=limit,
        offset=offset,
        gaps=gaps,
    )


@app.get("/api/v1/devices/{device_id}/committed", response_model=CommittedResponse)
def get_committed(
    device_id: str,
    from_seq: Optional[int] = Query(default=None, ge=1),
    to_seq: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=1000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
):
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT committed_upto FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        where, params = "device_id = ?", [device_id]
        if from_seq is not None:
            where += " AND seq >= ?"
            params.append(from_seq)
        if to_seq is not None:
            where += " AND seq <= ?"
            params.append(to_seq)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM committed WHERE {where}", params
        ).fetchone()["n"]
        seqs = [
            r["seq"]
            for r in conn.execute(
                f"SELECT seq FROM committed WHERE {where} ORDER BY seq LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        ]
    return CommittedResponse(
        device_id=device_id,
        committed_upto=dev["committed_upto"],
        count=total,
        seqs=seqs,
    )


@app.get("/api/v1/devices/{device_id}/duplicates", response_model=DuplicatesResponse)
def get_duplicates(
    device_id: str,
    reason: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        where, params = "device_id = ?", [device_id]
        if reason:
            where += " AND reason = ?"
            params.append(reason)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM duplicates WHERE {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM duplicates WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return DuplicatesResponse(
        device_id=device_id,
        count=total,
        records=[DuplicateRecord(**dict(r)) for r in rows],
    )


# ---------------------------------------------------------------------------
# 缺包作废
# ---------------------------------------------------------------------------

@app.post("/api/v1/devices/{device_id}/voids", response_model=VoidResponse)
def post_void(
    device_id: str,
    void_in: VoidCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """登记一段确定缺失的连续序号并立即排水。

    仅允许对已有设备、从当前 next_expected 开始且段内确为缺口的连续序号
    作废；段尾不得超过缓冲区最大序号，已提交 / 已缓冲 / 已作废序号均不可
    作废。与摄入共用串行锁与事务，幂等键重试不产生重复登记或重复推进。
    """
    key = void_in.idempotency_key or idempotency_key
    if not key:
        raise HTTPException(
            status_code=422,
            detail="缺少幂等键：请在请求体 idempotency_key 或 Idempotency-Key 请求头中提供",
        )
    with db.ingest_lock, db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        try:
            return void_missing(
                conn, device_id, void_in.from_seq, void_in.to_seq, key
            )
        except VoidConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VoidError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/devices/{device_id}/voids", response_model=VoidsResponse)
def get_voids(
    device_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """按设备分页查询作废登记，按登记先后升序返回。"""
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT committed_upto FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        total, rows = list_void_records(conn, device_id, limit, offset)
        voided_total = voided_count(conn, device_id)
        upto = dev["committed_upto"]
    records = []
    for r in rows:
        d = dict(r)
        d["voided_seqs"] = list(range(r["from_seq"], r["to_seq"] + 1))
        records.append(VoidRecordOut(**d))
    return VoidsResponse(
        device_id=device_id,
        committed_upto=upto,
        next_expected=upto + 1,
        count=total,
        voided_count=voided_total,
        records=records,
    )


@app.get("/api/v1/devices/{device_id}/metrics/rollup", response_model=RollupResponse)
def get_metrics_rollup(
    device_id: str,
    metric: str = Query(min_length=1, description="指标名，如 temperature"),
    from_: datetime = Query(alias="from", description="聚合范围起点（含），ISO 8601"),
    to: datetime = Query(description="聚合范围终点（不含），ISO 8601"),
    window_seconds: float = Query(gt=0, description="窗口宽度（秒）"),
    allowed_lateness: float = Query(
        default=0.0, ge=0, description="允许的迟到时长（秒），用于计算水位"
    ),
    calibrated: bool = Query(
        default=False,
        description="true 时按每个已提交包 sent_at 生效的校准区间计算统计值，"
        "无生效配置沿用原始值；false（默认）统计原始值",
    ),
):
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        try:
            return compute_rollup(
                conn,
                device_id,
                metric,
                from_,
                to,
                window_seconds,
                allowed_lateness,
                calibrated,
            )
        except RollupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 仪器预热稳定放行
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/devices/{device_id}/metrics/warmup-release",
    response_model=WarmupReleaseResponse,
)
def get_warmup_release(
    device_id: str,
    metric: str = Query(min_length=1, description="指标名，如 temperature"),
    unit: str = Query(min_length=1, description="要求的读数单位（读数须逐字相同，缺单位视为不符）"),
    from_: datetime = Query(alias="from", description="UTC 时间范围起点（含），ISO 8601"),
    to: datetime = Query(description="UTC 时间范围终点（不含），ISO 8601"),
    max_fluctuation: float = Query(
        ge=0, description="稳定段允许的最大波动：段内最大读数 - 最小读数不得超过该值"
    ),
    min_duration_seconds: float = Query(
        gt=0, description="稳定段首末读数时间跨度至少达到的秒数"
    ),
    min_readings: int = Query(ge=1, description="稳定段至少包含的读数条数"),
    max_gap_seconds: float = Query(
        ge=0, description="稳定段内相邻两次采样的最大允许间隔（秒）"
    ),
):
    """仪器预热稳定放行判定（纯只读，不改写任何已存数据）。

    仅查询该设备已提交包中的同指标测量：每条测量优先用 ts，否则用包
    sent_at，均无则忽略；按 (时间, 包序号, 包内位置) 排序。单位须与
    请求 unit 逐字相同（缺单位视为不符，不参与）。寻找最早完成的连续
    稳定段：段内最大值与最小值之差不超过 max_fluctuation、相邻采样
    间隔不超过 max_gap_seconds、首末时间跨度达到 min_duration_seconds
    且读数不少于 min_readings；同一完成时间取最早起点。无合格段时
    released=false 且 segment 为 null。
    """
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        try:
            return evaluate_warmup_release(
                conn,
                device_id,
                metric,
                unit,
                from_,
                to,
                max_fluctuation,
                min_duration_seconds,
                min_readings,
                max_gap_seconds,
            )
        except WarmupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 超阈暴露量积分
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/devices/{device_id}/metrics/exposure",
    response_model=ExposureResponse,
)
def get_metrics_exposure(
    device_id: str,
    metric: str = Query(min_length=1, description="指标名，如 temperature"),
    unit: str = Query(min_length=1, description="要求的读数单位（读数须逐字相同，缺单位视为不符）"),
    from_: datetime = Query(alias="from", description="UTC 积分范围起点（含），ISO 8601"),
    to: datetime = Query(description="UTC 积分范围终点（不含），ISO 8601"),
    threshold: float = Query(description="超阈判定阈值：对 max(value-threshold, 0) 积分"),
    max_gap_seconds: float = Query(
        ge=0, description="相邻读数最大允许间隔（秒）；间隔严格超过该值的整段不参与"
    ),
):
    """超阈暴露量积分（纯只读，不改写任何已存数据）。

    仅查询该设备已提交包中的同指标、同单位测量：每条测量优先用 ts，
    否则用包 sent_at，均无则丢弃；按 (时间, 包序号, 包内位置) 排序，
    同一时刻只保留最后一条。相邻读数间线性插值，间隔严格超过
    max_gap_seconds 的整段不计；按 [from, to) 裁剪线段并切分阈值交点，
    对 max(value-threshold, 0) 积分，返回超阈秒数与超阈面积。
    范围外的相邻读数仍参与边界插值；不足两条读数时结果为零。
    """
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        try:
            return compute_exposure(
                conn,
                device_id,
                metric,
                unit,
                from_,
                to,
                threshold,
                max_gap_seconds,
            )
        except ExposureError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 设备时钟漂移诊断
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/devices/{device_id}/metrics/clock-drift",
    response_model=ClockDriftResponse,
)
def get_metrics_clock_drift(
    device_id: str,
    metric: str = Query(min_length=1, description="指标名，如 temperature"),
    from_: datetime = Query(alias="from", description="UTC 包发送时间范围起点（含），ISO 8601"),
    to: datetime = Query(description="UTC 包发送时间范围终点（不含），ISO 8601"),
    max_abs_drift_per_hour: float = Query(
        ge=0,
        description="请求允许的每小时绝对漂移上限（秒/小时）；"
        "估计绝对漂移严格大于该值时结论为超限",
    ),
):
    """设备时钟漂移诊断（纯只读，不改写任何已存数据）。

    仅按包 sent_at 圈定范围、查询该设备已提交包：缺 sent_at 的包整包
    跳过；每包只取该 metric 首条带 ts 的测量，包内没有则跳过；时间统一
    为 UTC，偏移 = ts - sent_at（秒），同一发送时刻只保留序号最大的
    一包。对不同发送时刻的全部样本对求偏移斜率，取斜率中位数估计每小时
    漂移，再以残差中位数估计末次发送时刻的偏移。不同发送时刻的有效样本
    不足三包时返回证据不足；范围内包数超过 200 返回 422。
    """
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        try:
            return diagnose_clock_drift(
                conn,
                device_id,
                metric,
                from_,
                to,
                max_abs_drift_per_hour,
            )
        except ClockDriftError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 双设备同指标并行测量比对（交接班核对参考 / 待检仪器读数）
# ---------------------------------------------------------------------------

@app.get("/api/v1/comparisons", response_model=ComparisonResponse)
def get_comparison(
    reference_device: str = Query(
        min_length=1, description="参考仪器设备号"
    ),
    target_device: str = Query(
        min_length=1, description="待检仪器设备号"
    ),
    metric: str = Query(min_length=1, description="指标名，如 temperature"),
    from_: datetime = Query(alias="from", description="UTC 时间段起点（含），ISO 8601"),
    to: datetime = Query(description="UTC 时间段终点（不含），ISO 8601"),
    max_time_delta_seconds: float = Query(
        ge=0, description="允许的最大时间偏差（秒）；时间差不超过该值才允许配对"
    ),
    allowed_value_diff: float = Query(
        ge=0, description="允许的读数绝对数值偏差；严格大于该值标记为超差"
    ),
):
    """双设备同指标并行测量比对（只读，不改写原始包、缓冲与告警）。

    仅查询两侧已提交包中的同指标测量：每条测量优先用 ts，否则用包
    sent_at，均无则忽略；按 (时间, 包序号, 包内位置) 排序。仅在单位
    相同且时间差不超过 max_time_delta_seconds 时配对，配对保持两侧
    时间顺序、每条测量最多一次：先最大化配对数，再最小化配对时间差
    总和，平局按配对序号字典序决胜。单侧候选超过 200 条直接 422 拒绝。
    """
    with db.get_conn() as conn:
        for label, device_id in (
            ("参考设备", reference_device),
            ("待检设备", target_device),
        ):
            dev = conn.execute(
                "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if dev is None:
                raise HTTPException(status_code=404, detail=f"未知{label}: {device_id}")
        try:
            return compare_devices(
                conn,
                reference_device,
                target_device,
                metric,
                from_,
                to,
                max_time_delta_seconds,
                allowed_value_diff,
            )
        except ComparisonError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 双指标配比验收
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/ratio-acceptances",
    response_model=RatioAcceptanceOut,
    status_code=201,
)
def post_ratio_acceptance(cfg_in: RatioAcceptanceCreate):
    """为设备保存双指标配比验收配置（每台设备至多一条）。

    分子指标与分母指标必须不同，ratio_lower 不得大于 ratio_upper，
    min_valid_groups 至少为 1；重复配置返回 409。
    """
    with db.ingest_lock, db.get_conn() as conn:
        try:
            cfg_id = create_acceptance(
                conn,
                cfg_in.device_id,
                cfg_in.numerator_metric,
                cfg_in.denominator_metric,
                cfg_in.numerator_unit,
                cfg_in.denominator_unit,
                cfg_in.ratio_lower,
                cfg_in.ratio_upper,
                cfg_in.min_valid_groups,
            )
        except RatioAcceptanceConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RatioAcceptanceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = conn.execute(
            "SELECT * FROM ratio_acceptances WHERE id = ?", (cfg_id,)
        ).fetchone()
        return RatioAcceptanceOut(**dict(row))


@app.get("/api/v1/ratio-acceptances", response_model=List[RatioAcceptanceOut])
def list_ratio_acceptances(
    device_id: Optional[str] = Query(default=None, description="按设备过滤"),
):
    """列出双指标配比验收配置（可按设备过滤）。"""
    with db.get_conn() as conn:
        if device_id is not None:
            rows = conn.execute(
                "SELECT * FROM ratio_acceptances WHERE device_id = ? ORDER BY id",
                (device_id,),
            ).fetchall()
        else:
            rows = list_acceptances(conn)
    return [RatioAcceptanceOut(**dict(r)) for r in rows]


@app.get(
    "/api/v1/devices/{device_id}/ratio-acceptance",
    response_model=RatioAcceptanceOut,
)
def get_device_ratio_acceptance(device_id: str):
    """查询单台设备的双指标配比验收配置；未配置返回 404。"""
    with db.get_conn() as conn:
        row = get_acceptance(conn, device_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"设备 {device_id} 尚无可用双指标配比验收配置",
        )
    return RatioAcceptanceOut(**dict(row))


@app.get(
    "/api/v1/devices/{device_id}/ratio-acceptance/evaluate",
    response_model=RatioAcceptanceResponse,
)
def get_device_ratio_acceptance_result(
    device_id: str,
    from_seq: int = Query(ge=1, description="验收闭区间起点（含）"),
    to_seq: int = Query(ge=1, description="验收闭区间终点（含）"),
):
    """按设备 + seq 闭区间执行双指标配比验收（纯只读）。

    仅读取已提交包：在每个包内按分子 / 分母指标各自出现顺序一对一
    配组，禁止跨包；缺少对应读数、单位不符或分母为零的组标为无效并
    说明原因；有效组比例落在配置闭区间内才合格，整体须达到最低有效
    组数且所有有效组合格。缓冲包与作废序号不参与计算。
    """
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        try:
            return evaluate_ratio(conn, device_id, from_seq, to_seq)
        except RatioAcceptanceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RatioAcceptanceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 阈值告警规则
# ---------------------------------------------------------------------------

@app.post("/api/v1/rules", response_model=RuleOut, status_code=201)
def post_rule(rule_in: RuleCreate):
    with db.ingest_lock, db.get_conn() as conn:
        try:
            rule = create_rule(
                conn,
                rule_in.device_id,
                rule_in.metric,
                rule_in.upper_limit,
                rule_in.lower_limit,
                rule_in.trigger_count,
                rule_in.recovery_count,
                rule_in.enabled,
            )
        except AlertRuleConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlertRuleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RuleOut(**conn.execute(
            "SELECT * FROM alert_rules WHERE id = ?", (rule.id,)
        ).fetchone())


@app.get("/api/v1/rules", response_model=RuleListResponse)
def list_rules(
    device_id: Optional[str] = Query(default=None, description="按设备过滤"),
    metric: Optional[str] = Query(default=None, description="按指标过滤"),
):
    where, params = [], []
    if device_id:
        where.append("device_id = ?")
        params.append(device_id)
    if metric:
        where.append("metric = ?")
        params.append(metric)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM alert_rules {clause} ORDER BY id", params
        ).fetchall()
    return RuleListResponse(count=len(rows), rules=[RuleOut(**dict(r)) for r in rows])


def _get_rule_or_404(conn, rule_id: int):
    rule = get_rule(conn, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return rule


@app.get("/api/v1/rules/{rule_id}", response_model=RuleOut)
def get_rule_detail(rule_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM alert_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
        return RuleOut(**dict(row))


@app.put("/api/v1/rules/{rule_id}", response_model=RuleOut)
def put_rule(rule_id: int, rule_in: RuleUpdate):
    with db.ingest_lock, db.get_conn() as conn:
        _get_rule_or_404(conn, rule_id)
        try:
            update_rule(
                conn,
                rule_id,
                rule_in.upper_limit,
                rule_in.lower_limit,
                rule_in.trigger_count,
                rule_in.recovery_count,
                rule_in.enabled,
            )
        except AlertRuleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = conn.execute(
            "SELECT * FROM alert_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return RuleOut(**dict(row))


@app.delete("/api/v1/rules/{rule_id}", status_code=204)
def remove_rule(rule_id: int):
    with db.ingest_lock, db.get_conn() as conn:
        if not delete_rule(conn, rule_id):
            raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return None


# ---------------------------------------------------------------------------
# 当前告警与告警事件
# ---------------------------------------------------------------------------

@app.get("/api/v1/alerts", response_model=CurrentAlertsResponse)
def get_current_alerts(
    device_id: Optional[str] = Query(default=None, description="按设备过滤"),
    metric: Optional[str] = Query(default=None, description="按指标过滤"),
):
    """当前仍处于告警状态的规则。triggered_* 取自最近一次触发事件。"""
    where, params = ["s.state = 'triggered'", "r.enabled = 1"], []
    if device_id:
        where.append("r.device_id = ?")
        params.append(device_id)
    if metric:
        where.append("r.metric = ?")
        params.append(metric)
    clause = " AND ".join(where)
    sql = f"""
        SELECT r.id               AS rule_id,
               r.device_id        AS device_id,
               r.metric           AS metric,
               r.upper_limit      AS upper_limit,
               r.lower_limit      AS lower_limit,
               r.trigger_count    AS trigger_count,
               r.recovery_count   AS recovery_count,
               t.seq              AS triggered_seq,
               t.id               AS triggered_event_id,
               t.event_time       AS triggered_at,
               s.last_value       AS last_value,
               s.last_seq         AS last_seq,
               s.recovery_streak  AS recovery_streak
          FROM alert_rule_states s
          JOIN alert_rules r ON r.id = s.rule_id
          JOIN alert_events t
            ON t.id = (
                SELECT MAX(e.id) FROM alert_events e
                 WHERE e.rule_id = r.id AND e.event_type = 'triggered'
            )
         WHERE {clause}
         ORDER BY t.id
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return CurrentAlertsResponse(
        count=len(rows), alerts=[CurrentAlertOut(**dict(r)) for r in rows]
    )


@app.get("/api/v1/devices/{device_id}/alerts/events", response_model=AlertEventsResponse)
def get_device_alert_events(
    device_id: str,
    metric: Optional[str] = Query(default=None, description="按指标过滤"),
    event_type: Optional[str] = Query(
        default=None, description="按事件类型过滤：triggered / recovered"
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """按设备（可选指标）分页查询告警事件，按事件序号升序返回。"""
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        if event_type not in (None, "triggered", "recovered"):
            raise HTTPException(
                status_code=422,
                detail="event_type 仅支持 triggered 或 recovered",
            )
        where, params = ["device_id = ?"], [device_id]
        if metric:
            where.append("metric = ?")
            params.append(metric)
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        clause = " AND ".join(where)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM alert_events WHERE {clause}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM alert_events WHERE {clause} ORDER BY id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return AlertEventsResponse(
        count=total,
        limit=limit,
        offset=offset,
        events=[AlertEventOut(**dict(r)) for r in rows],
    )


# ---------------------------------------------------------------------------
# 设备指标校准
# ---------------------------------------------------------------------------

@app.post("/api/v1/calibrations", response_model=CalibrationOut, status_code=201)
def post_calibration(cal_in: CalibrationCreate):
    """创建线性校准配置；同 (设备, 指标) 有效时间段重叠时返回 409。"""
    with db.ingest_lock, db.get_conn() as conn:
        try:
            cal_id = create_calibration(
                conn,
                cal_in.device_id,
                cal_in.metric,
                cal_in.valid_from,
                cal_in.valid_to,
                cal_in.offset,
                cal_in.scale,
            )
        except CalibrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CalibrationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = conn.execute(
            "SELECT * FROM calibrations WHERE id = ?", (cal_id,)
        ).fetchone()
        return CalibrationOut(**dict(row))


@app.get(
    "/api/v1/devices/{device_id}/calibrations/resolve",
    response_model=CalibrationResolveResponse,
)
def resolve_device_calibration(
    device_id: str,
    metric: str = Query(min_length=1, description="指标名，如 temperature"),
    at: datetime = Query(description="生效时刻，ISO 8601；不带时区按 UTC"),
    value: float = Query(description="原始测量值"),
):
    """按 at 解析 (设备, 指标) 唯一生效的校准配置并计算校准值。

    校准值 = value * scale + offset；无生效配置时 calibration 与
    calibrated_value 均为 null。只读操作，不改写已存储的原始测量。
    """
    with db.get_conn() as conn:
        row = resolve_calibration(conn, device_id, metric, at)
    calibration = CalibrationOut(**dict(row)) if row is not None else None
    calibrated = (
        apply_calibration(value, row["scale"], row["offset"])
        if row is not None
        else None
    )
    return CalibrationResolveResponse(
        device_id=device_id,
        metric=metric,
        at=at,
        value=value,
        calibrated_value=calibrated,
        calibration=calibration,
    )


# ---------------------------------------------------------------------------
# 只读归档导出（NDJSON 分页 + manifest 校验）
# ---------------------------------------------------------------------------

def _get_export_or_404(conn, export_id: str):
    task = exports.get_task(conn, export_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"导出任务不存在: {export_id}")
    return task


@app.post("/api/v1/exports", response_model=ExportCreateResponse, status_code=201)
def post_export(export_in: ExportCreate):
    """按设备 + seq 范围创建只读快照导出，返回 manifest 与首页游标。

    仅读取创建时刻已提交的包；缓冲、重复、告警数据均不受影响。
    """
    with db.ingest_lock, db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (export_in.device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(
                status_code=404, detail=f"未知设备: {export_in.device_id}"
            )
        try:
            task = exports.create_export(
                conn,
                export_in.device_id,
                export_in.from_seq,
                export_in.to_seq,
                export_in.page_size,
            )
        except ExportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    body = exports.manifest_dict(task)
    body["next_cursor"] = exports.first_cursor(task)
    return ExportCreateResponse(**body)


@app.get("/api/v1/exports/{export_id}/manifest", response_model=ExportManifest)
def get_export_manifest(export_id: str):
    with db.get_conn() as conn:
        task = _get_export_or_404(conn, export_id)
        return ExportManifest(**exports.manifest_dict(task))


@app.get("/api/v1/exports/{export_id}/pages")
def get_export_page(
    export_id: str,
    cursor: Optional[str] = Query(
        default=None, description="续传游标；缺省从首页（offset=0）开始"
    ),
):
    """按游标取下一页 NDJSON（每包一行），稳定内容与摘要，可任意重复读取。

    响应头 X-Next-Cursor 给出下一页游标，末页不返回该头并带 X-Export-Done: true。
    """
    with db.get_conn() as conn:
        task = _get_export_or_404(conn, export_id)
        cursor = cursor or exports.first_cursor(task)
        try:
            page = exports.fetch_page(conn, task, cursor)
        except InvalidCursor as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    headers = {
        "X-Export-Id": task.export_id,
        "X-Page-Offset": str(page["offset"]),
        "X-Package-Count": str(task.package_count),
        "X-Content-SHA256": task.content_sha256,
        "X-Export-Done": "true" if page["done"] else "false",
    }
    if page["next_cursor"] is not None:
        headers["X-Next-Cursor"] = page["next_cursor"]
    return Response(
        content=b"".join(page["lines"]),
        media_type="application/x-ndjson",
        headers=headers,
    )


@app.post(
    "/api/v1/exports/{export_id}/verify", response_model=ExportVerifyResponse
)
def post_export_verify(export_id: str, verify_in: ExportVerifyRequest):
    """提交导出内容摘要以校验完整性；摘要一致时 valid=true。"""
    with db.get_conn() as conn:
        task = _get_export_or_404(conn, export_id)
        valid = exports.verify_manifest(task, verify_in.content_sha256)
        manifest = ExportManifest(**exports.manifest_dict(task))
    return ExportVerifyResponse(
        valid=valid,
        expected_sha256=task.content_sha256,
        received_sha256=verify_in.content_sha256.strip().lower(),
        manifest=manifest,
    )
