"""请求 / 响应的数据模型。"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class Measurement(BaseModel):
    metric: str = Field(min_length=1, description="指标名，如 temperature")
    value: float
    unit: Optional[str] = None
    ts: Optional[datetime] = None


class PackageIn(BaseModel):
    """仪器上报的一个批量测量包。"""

    device_id: str = Field(min_length=1)
    seq: int = Field(ge=1, description="包序号，按设备单调递增、连续")
    idempotency_key: Optional[str] = Field(
        default=None, description="幂等键；也可用 Idempotency-Key 请求头传入"
    )
    sent_at: Optional[datetime] = None
    measurements: List[Measurement] = Field(min_length=1)


class IngestResponse(BaseModel):
    status: Literal["committed", "buffered", "duplicate"]
    device_id: str
    seq: int
    committed_upto: int
    committed_seqs: List[int] = Field(
        default_factory=list, description="本次请求触发提交的全部序号（含排水）"
    )
    duplicate_reason: Optional[str] = None


class DeviceState(BaseModel):
    device_id: str
    committed_upto: int = Field(
        description="已连续处理（提交或作废）的最大序号"
    )
    next_expected: int
    buffered_count: int
    committed_count: int
    duplicate_count: int
    voided_count: int = Field(description="已作废序号总数（缺包作废段长度之和）")


class GapsResponse(BaseModel):
    device_id: str
    committed_upto: int
    next_expected: int
    buffered_seqs: List[int] = Field(description="当前缓冲区全部序号（升序）")
    gap_count: int = Field(
        description="next_expected 到缓冲区最大序号之间的缺失序号总数"
    )
    limit: int = Field(description="本页大小上限（默认 1000，最大 10000）")
    offset: int = Field(description="本页起始缺口偏移（从 0 起）")
    gaps: List[int] = Field(
        description="本页缺失序号（升序），最多 limit 条；按连续缺口区间计算，不展开全集"
    )


class CommittedResponse(BaseModel):
    device_id: str
    committed_upto: int
    count: int
    seqs: List[int]


class DuplicateRecord(BaseModel):
    id: int
    device_id: str
    seq: Optional[int]
    idempotency_key: Optional[str]
    reason: str
    received_at: str


class DuplicatesResponse(BaseModel):
    device_id: str
    count: int
    records: List[DuplicateRecord]


# ---------------------------------------------------------------------------
# 缺包作废
# ---------------------------------------------------------------------------

class VoidCreate(BaseModel):
    """对一段确定缺失的连续序号发起作废。"""

    from_seq: int = Field(
        ge=1, description="作废段起点（含）；必须恰好等于当前 next_expected"
    )
    to_seq: int = Field(
        ge=1, description="作废段终点（含）；不得超过当前缓冲区最大序号"
    )
    idempotency_key: Optional[str] = Field(
        default=None, description="幂等键；也可用 Idempotency-Key 请求头传入"
    )

    @model_validator(mode="after")
    def _check_range(self):
        if self.from_seq > self.to_seq:
            raise ValueError("from_seq 不能大于 to_seq")
        return self


class VoidResponse(BaseModel):
    status: Literal["voided"]
    device_id: str
    from_seq: int = Field(description="作废段起点（含）")
    to_seq: int = Field(description="作废段终点（含）")
    voided_seqs: List[int] = Field(description="本次登记作废的全部序号")
    committed_upto: int = Field(
        description="作废并排水后已连续处理（提交或作废）的最大序号"
    )
    committed_seqs: List[int] = Field(
        default_factory=list,
        description="作废后立即排水提交的真实缓冲包序号（不含作废序号）",
    )


class VoidRecordOut(BaseModel):
    """一条作废登记：一段连续的作废序号。"""

    id: int
    device_id: str
    from_seq: int = Field(description="作废段起点（含）")
    to_seq: int = Field(description="作废段终点（含）")
    voided_seqs: List[int] = Field(description="该段作废的全部序号")
    created_at: str


class VoidsResponse(BaseModel):
    device_id: str
    committed_upto: int = Field(
        description="已连续处理（提交或作废）的最大序号"
    )
    next_expected: int
    count: int = Field(description="作废登记段数")
    voided_count: int = Field(description="已作废序号总数")
    records: List[VoidRecordOut]


class RollupWindow(BaseModel):
    """单个半开窗口 [start, end) 的聚合结果；空窗口 count=0 且统计值为 null。"""

    start: datetime
    end: datetime
    count: int
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None


class RollupResponse(BaseModel):
    """按事件时间（sent_at）的窗口聚合结果。"""

    model_config = {"populate_by_name": True}

    device_id: str
    metric: str
    from_: datetime = Field(alias="from", description="聚合范围起点（含）")
    to: datetime = Field(description="聚合范围终点（不含）")
    window_seconds: float
    allowed_lateness: float
    calibrated: bool = Field(
        default=False,
        description="统计值是否为校准值：true 时按包 sent_at 生效的校准区间计算，"
        "无生效配置沿用原始值；false 时始终为原始值",
    )
    watermark: Optional[datetime] = Field(
        default=None,
        description="设备已提交数据的最大 sent_at 减 allowed_lateness；无数据时为 null",
    )
    late_count: int = Field(
        description="范围内 sent_at 早于水位的迟到样本数（不改写已关闭窗口）"
    )
    windows: List[RollupWindow] = Field(description="按 start 升序的全部窗口")


# ---------------------------------------------------------------------------
# 阈值告警
# ---------------------------------------------------------------------------

class RuleBase(BaseModel):
    """规则的可配置字段（创建与全量更新共用）。"""

    upper_limit: Optional[float] = Field(
        default=None, description="上限；value > upper_limit 视为越界，null 表示不限"
    )
    lower_limit: Optional[float] = Field(
        default=None, description="下限；value < lower_limit 视为越界，null 表示不限"
    )
    trigger_count: int = Field(
        default=1, ge=1, description="连续越界多少次后触发告警"
    )
    recovery_count: int = Field(
        default=1, ge=1, description="告警后连续不越界多少次后恢复"
    )
    enabled: bool = Field(default=True, description="是否参与评估")

    @model_validator(mode="after")
    def _check_limits(self):
        if self.upper_limit is None and self.lower_limit is None:
            raise ValueError("upper_limit 与 lower_limit 至少需要提供一个")
        if (
            self.upper_limit is not None
            and self.lower_limit is not None
            and self.lower_limit > self.upper_limit
        ):
            raise ValueError("lower_limit 不能大于 upper_limit")
        return self


class RuleCreate(RuleBase):
    device_id: str = Field(min_length=1)
    metric: str = Field(min_length=1, description="指标名，如 temperature")


class RuleUpdate(RuleBase):
    """PUT 全量更新；device_id 与 metric 不可修改。"""


class RuleOut(BaseModel):
    id: int
    device_id: str
    metric: str
    upper_limit: Optional[float]
    lower_limit: Optional[float]
    trigger_count: int
    recovery_count: int
    enabled: bool
    created_at: str
    updated_at: str


class RuleListResponse(BaseModel):
    count: int
    rules: List[RuleOut]


class AlertEventOut(BaseModel):
    """一次触发或恢复事件；id 为按提交顺序单调递增的事件序号。"""

    id: int
    rule_id: Optional[int] = Field(
        description="规则删除后为 null，事件本身仍可查询"
    )
    device_id: str
    metric: str
    seq: int = Field(description="触发 / 恢复时处理的包序号")
    event_type: Literal["triggered", "recovered"]
    value: float = Field(description="触发或恢复的那个测量值")
    event_time: Optional[str] = Field(
        description="触发 / 恢复时间：优先取测量 ts，其次包 sent_at，再无则提交时刻"
    )
    created_at: str = Field(description="事件落库时间")


class AlertEventsResponse(BaseModel):
    count: int
    limit: int
    offset: int
    events: List[AlertEventOut]


class CurrentAlertOut(BaseModel):
    """当前仍处于告警中的规则状态（规则状态为 triggered 且规则启用）。"""

    rule_id: int
    device_id: str
    metric: str
    upper_limit: Optional[float]
    lower_limit: Optional[float]
    trigger_count: int
    recovery_count: int
    triggered_seq: int = Field(description="触发告警的包序号")
    triggered_event_id: int = Field(description="触发事件的事件序号")
    triggered_at: Optional[str] = Field(description="触发时间")
    last_value: Optional[float]
    last_seq: Optional[int]
    recovery_streak: int = Field(description="当前连续不越界计数（未达恢复次数）")


class CurrentAlertsResponse(BaseModel):
    count: int
    alerts: List[CurrentAlertOut]


# ---------------------------------------------------------------------------
# 设备指标校准
# ---------------------------------------------------------------------------

class CalibrationCreate(BaseModel):
    """创建一条线性校准配置：校准值 = 原始值 * scale + offset。"""

    device_id: str = Field(min_length=1)
    metric: str = Field(min_length=1, description="指标名，如 temperature")
    valid_from: datetime = Field(description="生效起点（含），ISO 8601；不带时区按 UTC")
    valid_to: Optional[datetime] = Field(
        default=None, description="生效终点（不含），ISO 8601；缺省表示无限期"
    )
    offset: float = Field(default=0.0, description="线性偏移")
    scale: float = Field(default=1.0, description="线性增益")

    @model_validator(mode="after")
    def _check_range(self):
        if self.valid_to is not None and self.valid_from >= self.valid_to:
            raise ValueError("valid_from 必须早于 valid_to")
        return self


class CalibrationOut(BaseModel):
    id: int
    device_id: str
    metric: str
    valid_from: datetime
    valid_to: Optional[datetime]
    offset: float
    scale: float
    created_at: str


class CalibrationResolveResponse(BaseModel):
    """按时刻解析生效校准配置并计算校准值；无生效配置时 calibration 为 null。"""

    device_id: str
    metric: str
    at: datetime
    value: float = Field(description="原始测量值")
    calibrated_value: Optional[float] = Field(
        description="校准值 = value * scale + offset；无生效配置时为 null"
    )
    calibration: Optional[CalibrationOut] = Field(
        description="at 时刻唯一生效的校准配置；无配置为 null"
    )


# ---------------------------------------------------------------------------
# 只读归档导出
# ---------------------------------------------------------------------------

class ExportCreate(BaseModel):
    """创建一次按设备 + seq 范围的只读导出任务。"""

    device_id: str = Field(min_length=1)
    from_seq: Optional[int] = Field(
        default=None, ge=1, description="起始序号（含）；缺省从首包开始"
    )
    to_seq: Optional[int] = Field(
        default=None, ge=1, description="结束序号（含）；缺省到创建时刻已提交末包"
    )
    page_size: int = Field(
        default=100, ge=1, le=1000, description="每页 NDJSON 行数"
    )

    @model_validator(mode="after")
    def _check_range(self):
        if (
            self.from_seq is not None
            and self.to_seq is not None
            and self.from_seq > self.to_seq
        ):
            raise ValueError("from_seq 不能大于 to_seq")
        return self


class ExportManifest(BaseModel):
    """导出任务清单：范围、包数、首尾序号与规范化内容摘要。"""

    export_id: str
    device_id: str
    from_seq: Optional[int]
    to_seq: Optional[int]
    page_size: int
    package_count: int = Field(description="快照中的包数")
    first_seq: Optional[int] = Field(description="快照首序号；空导出为 null")
    last_seq: Optional[int] = Field(description="快照末序号；空导出为 null")
    complete: bool = Field(
        description="请求的 seq 范围是否被创建时刻已提交数据连续完整覆盖"
    )
    content_sha256: str = Field(
        description="全量 NDJSON（按 seq 升序，逐行 sha256）的 SHA-256"
    )
    created_at: str


class ExportCreateResponse(ExportManifest):
    next_cursor: str = Field(description="首页游标（offset=0）")


class ExportVerifyRequest(BaseModel):
    content_sha256: str = Field(
        min_length=1, description="客户端对导出 NDJSON 内容计算的 SHA-256（十六进制）"
    )


class ExportVerifyResponse(BaseModel):
    valid: bool
    expected_sha256: str
    received_sha256: str
    manifest: ExportManifest


# ---------------------------------------------------------------------------
# 双设备同指标并行测量比对
# ---------------------------------------------------------------------------

class ComparisonSidePoint(BaseModel):
    """参与比对的一侧测量：所属包序号、包内位置、有效时间与读数。"""

    device_id: str
    seq: int = Field(description="测量所属已提交包序号")
    position: int = Field(description="测量在该包 measurements 中的原始下标（从 0 起）")
    time: datetime = Field(description="有效时间：优先测量 ts，否则包 sent_at，UTC")
    time_source: Literal["ts", "sent_at"] = Field(
        description="有效时间来源：测量自身 ts 或包 sent_at"
    )
    value: float = Field(description="测量读数（原始值，不应用校准）")
    unit: Optional[str] = Field(description="测量单位；未提供时为 null")


class ComparisonPair(BaseModel):
    """一对成功配对的测量。"""

    pair_time: datetime = Field(description="配对时间：参考侧有效测量时间（UTC）")
    reference: ComparisonSidePoint = Field(description="参考仪器一侧")
    target: ComparisonSidePoint = Field(description="待检仪器一侧")
    time_delta_seconds: float = Field(
        ge=0.0, description="两侧有效测量时间差绝对值（秒），不超过最大时间偏差"
    )
    abs_value_diff: float = Field(ge=0.0, description="两侧读数的绝对数值差")
    exceeded: bool = Field(
        description="是否超差：绝对数值差严格大于 allowed_value_diff 时为 true"
    )


class ComparisonResponse(BaseModel):
    """双设备同指标并行测量比对结果。"""

    model_config = {"populate_by_name": True}

    reference_device: str
    target_device: str
    metric: str
    from_: datetime = Field(alias="from", description="比对 UTC 时间范围起点（含）")
    to: datetime = Field(description="比对 UTC 时间范围终点（不含）")
    max_time_delta_seconds: float = Field(description="允许的最大时间偏差（秒）")
    allowed_value_diff: float = Field(description="允许的读数绝对数值偏差")
    reference_count: int = Field(description="参考侧纳入比对的测量条数")
    target_count: int = Field(description="待检侧纳入比对的测量条数")
    paired_count: int = Field(description="配对成功的数量")
    reference_unpaired_count: int = Field(description="参考侧未配对条数")
    target_unpaired_count: int = Field(description="待检侧未配对条数")
    exceeded_count: int = Field(description="超差配对数")
    pairs: List[ComparisonPair] = Field(
        description="配对列表，按参考侧（两侧一致的）时间顺序排列"
    )


# ---------------------------------------------------------------------------
# 仪器预热稳定放行
# ---------------------------------------------------------------------------

class WarmupReading(BaseModel):
    """稳定段内的一条读数：包序号、包内位置、有效时间与取值。"""

    index: int = Field(description="稳定段内序号，从 1 起（按 时间, 包序号, 包内位置 升序）")
    seq: int = Field(description="读数所属已提交包序号")
    position: int = Field(
        description="读数在该包 measurements 中的原始下标（从 0 起）"
    )
    time: datetime = Field(description="有效时间：优先测量 ts，否则包 sent_at，UTC")
    time_source: Literal["ts", "sent_at"] = Field(
        description="有效时间来源：测量自身 ts 或包 sent_at"
    )
    value: float = Field(description="测量读数（原始值，不应用校准）")
    unit: Optional[str] = Field(description="测量单位；未提供时为 null")


class WarmupSegment(BaseModel):
    """找到的连续稳定段：最早完成、同完成时间取最早起点。"""

    start_time: datetime = Field(description="稳定段首条读数的有效时间（UTC）")
    end_time: datetime = Field(description="稳定段末条读数的有效时间（UTC），即放行完成时间")
    duration_seconds: float = Field(
        ge=0.0, description="首末读数有效时间跨度（秒），达到最短持续时长"
    )
    reading_count: int = Field(description="段内读数条数，不少于最少读数要求")
    min_value: float = Field(description="段内最小读数")
    max_value: float = Field(description="段内最大读数")
    fluctuation: float = Field(
        ge=0.0, description="段内实际波动 = 最大读数 - 最小读数，不超过最大波动要求"
    )
    max_adjacent_gap_seconds: float = Field(
        ge=0.0, description="段内实际最大相邻采样间隔（秒），不超过最大间隔要求"
    )
    readings: List[WarmupReading] = Field(
        description="段内全部读数，按 时间, 包序号, 包内位置 升序"
    )


class WarmupReleaseResponse(BaseModel):
    """仪器预热稳定放行判定结果；无合格稳定段时 released=false 且 segment 为 null。"""

    model_config = {"populate_by_name": True}

    device_id: str
    metric: str
    unit: str = Field(description="要求的读数单位（读数须逐字相同，缺单位视为不符）")
    from_: datetime = Field(alias="from", description="判定 UTC 时间范围起点（含）")
    to: datetime = Field(description="判定 UTC 时间范围终点（不含）")
    max_fluctuation: float = Field(description="允许的段内最大波动（最大读数-最小读数）")
    min_duration_seconds: float = Field(description="要求的稳定段最短持续时长（秒）")
    min_readings: int = Field(description="要求的稳定段最少读数条数")
    max_gap_seconds: float = Field(description="允许的段内最大相邻采样间隔（秒）")
    candidate_count: int = Field(
        description="参与判定的读数条数：指标相符、单位相符、有有效时间且落在时间范围内"
    )
    excluded_no_time_count: int = Field(
        description="因既无测量 ts 也无包 sent_at 而被排除的同指标读数条数"
    )
    excluded_unit_mismatch_count: int = Field(
        description="时间范围内单位与要求不符（含缺单位）而被排除的同指标读数条数"
    )
    released: bool = Field(description="放行结论：存在合格连续稳定段时为 true")
    segment: Optional[WarmupSegment] = Field(
        default=None, description="最早完成的合格稳定段；未放行时为 null"
    )
    not_released_reason: Optional[str] = Field(
        default=None, description="未放行说明；放行时为 null"
    )


# ---------------------------------------------------------------------------
# 超阈暴露量积分
# ---------------------------------------------------------------------------

class ExposureResponse(BaseModel):
    """读数曲线超过阈值部分在 [from, to) 上的暴露秒数与暴露面积。"""

    model_config = {"populate_by_name": True}

    device_id: str
    metric: str
    unit: str = Field(description="要求的读数单位（读数须逐字相同，缺单位视为不符）")
    from_: datetime = Field(alias="from", description="积分范围起点（含）")
    to: datetime = Field(description="积分范围终点（不含）")
    threshold: float = Field(description="超阈判定阈值：对 max(value-threshold, 0) 积分")
    max_gap_seconds: float = Field(
        description="相邻读数最大允许间隔（秒）；间隔严格超过该值时整段插值不计"
    )
    sample_count: int = Field(
        description="指标相符、单位相符且有有效时间的读数条数（同一时刻去重后）"
    )
    segment_count: int = Field(
        description="实际参与积分的相邻线段数（裁剪到 [from, to) 且间隔未超限）"
    )
    excluded_no_time_count: int = Field(
        description="因既无测量 ts 也无包 sent_at 而被丢弃的同指标读数条数"
    )
    excluded_unit_mismatch_count: int = Field(
        description="单位与要求不符（含缺单位）而被丢弃的同指标读数条数"
    )
    exposure_seconds: float = Field(
        ge=0.0, description="范围内插值曲线严格高于阈值的总时长（秒）"
    )
    exposure_area: float = Field(
        ge=0.0,
        description="范围内 max(插值读数-threshold, 0) 对时间的积分（读数单位·秒）",
    )


# ---------------------------------------------------------------------------
# 双指标配比验收
# ---------------------------------------------------------------------------

class RatioAcceptanceCreate(BaseModel):
    """为设备保存双指标配比验收配置；每台设备至多一条。"""

    device_id: str = Field(min_length=1)
    numerator_metric: str = Field(
        min_length=1, description="分子指标名，必须与分母指标不同"
    )
    denominator_metric: str = Field(min_length=1, description="分母指标名")
    numerator_unit: str = Field(
        min_length=1, description="分子读数要求的单位（读数须逐字相同）"
    )
    denominator_unit: str = Field(
        min_length=1, description="分母读数要求的单位（读数须逐字相同）"
    )
    ratio_lower: float = Field(description="合格比例下限（含），不得大于上限")
    ratio_upper: float = Field(description="合格比例上限（含）")
    min_valid_groups: int = Field(
        ge=1, description="整体验收要求达到的最低有效组数"
    )

    @model_validator(mode="after")
    def _check_config(self):
        if self.numerator_metric == self.denominator_metric:
            raise ValueError("分子指标与分母指标必须不同")
        if self.ratio_lower > self.ratio_upper:
            raise ValueError("ratio_lower 不能大于 ratio_upper")
        return self


class RatioAcceptanceOut(BaseModel):
    """一条设备双指标配比验收配置。"""

    id: int
    device_id: str
    numerator_metric: str
    denominator_metric: str
    numerator_unit: str
    denominator_unit: str
    ratio_lower: float
    ratio_upper: float
    min_valid_groups: int
    created_at: str
    updated_at: str


class RatioReading(BaseModel):
    """一组配比中一侧（分子或分母）的读数定位与取值。"""

    seq: int = Field(description="读数所属已提交包序号")
    position: int = Field(
        description="读数在该包 measurements 中的原始下标（从 0 起）"
    )
    value: float
    unit: Optional[str] = Field(description="读数自带单位；未提供时为 null")
    unit_matches: bool = Field(
        description="读数单位是否与配置要求的单位逐字一致（读数缺单位视为不符）"
    )


class RatioGroup(BaseModel):
    """一个包内按出现顺序一对一配成的一组（或因缺读数未配成的残余侧）。"""

    index: int = Field(
        description="逐组序号：从 1 起，按包序号与包内顺序跨包连续编号"
    )
    seq: int = Field(description="本组所属已提交包序号（配组不跨包）")
    position: int = Field(
        description="包内位置：该组两侧读数在各自指标序列中的包内序号（从 0 起）"
    )
    numerator: Optional[RatioReading] = Field(
        description="分子读数；分子侧无对应读数时为 null"
    )
    denominator: Optional[RatioReading] = Field(
        description="分母读数；分母侧无对应读数时为 null"
    )
    valid: bool = Field(
        description="是否为有效组：读数齐全、单位均相符且分母非零"
    )
    invalid_reason: Optional[Literal[
        "missing_numerator",
        "missing_denominator",
        "unit_mismatch",
        "zero_denominator",
    ]] = Field(
        default=None,
        description="无效原因代码；有效组为 null："
        "missing_numerator/missing_denominator=缺少对应读数，"
        "unit_mismatch=单位与配置不符（含读数缺单位），"
        "zero_denominator=分母读数为零",
    )
    invalid_reason_detail: Optional[str] = Field(
        default=None, description="无效原因的中文说明；有效组为 null"
    )
    ratio: Optional[float] = Field(
        default=None,
        description="分子读数 / 分母读数；无效组（含分母为零）为 null",
    )
    conclusion: Literal["pass", "fail", "invalid"] = Field(
        description="结论：pass=有效且比例落在闭区间内；"
        "fail=有效但比例超出闭区间；invalid=无效组不参与合格判定"
    )


class RatioAcceptanceResponse(BaseModel):
    """按设备 + seq 闭区间执行双指标配比验收的结果。"""

    device_id: str
    from_seq: int = Field(description="验收闭区间起点（含）")
    to_seq: int = Field(description="验收闭区间终点（含）")
    config: RatioAcceptanceOut = Field(description="本次验收使用的设备配置")
    package_count: int = Field(
        description="区间内参与计算的已提交包数（缓冲包与作废序号不计）"
    )
    group_count: int = Field(description="配组总数（含无效组）")
    valid_group_count: int = Field(description="有效组数")
    invalid_group_count: int = Field(description="无效组数")
    failed_group_count: int = Field(
        description="有效但比例超出闭区间的组数"
    )
    accepted: bool = Field(
        description="整体验收结论：有效组数达到最低要求且所有有效组比例均在闭区间内"
    )
    not_accepted_reason: Optional[str] = Field(
        default=None, description="整体不合格的说明；合格时为 null"
    )
    groups: List[RatioGroup] = Field(
        description="逐组明细，按逐组序号升序（即包序号与包内顺序）"
    )


# ---------------------------------------------------------------------------
# 设备时钟漂移诊断
# ---------------------------------------------------------------------------

class ClockDriftSample(BaseModel):
    """一个发送时刻的有效样本（同发送时刻已只保留序号最大的一包）。"""

    seq: int = Field(description="样本所属已提交包序号")
    position: int = Field(
        description="该指标首条带 ts 测量在包 measurements 中的原始下标（从 0 起）"
    )
    sent_at: datetime = Field(description="包发送时间（UTC）")
    ts: datetime = Field(description="该指标首条带 ts 测量的时间戳（UTC）")
    offset_seconds: float = Field(
        description="偏移秒数 = ts - sent_at（正值表示设备时钟偏快）"
    )


class ClockDriftResponse(BaseModel):
    """设备时钟漂移诊断结果。

    不同发送时刻的有效样本不足三包时结论为证据不足，漂移与末次偏移为 null。
    """

    model_config = {"populate_by_name": True}

    device_id: str
    metric: str
    from_: datetime = Field(alias="from", description="诊断 UTC 发送时间范围起点（含）")
    to: datetime = Field(description="诊断 UTC 发送时间范围终点（不含）")
    max_abs_drift_per_hour: float = Field(
        ge=0.0, description="请求允许的每小时绝对漂移上限（秒/小时）"
    )
    package_count: int = Field(
        description="sent_at 落在范围内的已提交包数（含因无 ts 跳过的包；"
        "缺 sent_at 的包不计）"
    )
    excluded_no_sent_at_count: int = Field(
        description="因包缺少 sent_at 而被跳过的已提交包数（与范围无关）"
    )
    excluded_no_ts_count: int = Field(
        description="范围内但包内没有该指标带 ts 测量而被跳过的包数"
    )
    sample_count: int = Field(
        description="去重后不同发送时刻的有效样本数（同刻只留序号最大的一包）"
    )
    sufficient_evidence: bool = Field(
        description="证据是否充分：不同发送时刻的有效样本至少三包时为 true"
    )
    samples: List[ClockDriftSample] = Field(
        description="全部有效样本，按 sent_at 升序"
    )
    last_sent_at: Optional[datetime] = Field(
        default=None, description="末次（最大）发送时间（UTC）；无样本时为 null"
    )
    drift_per_hour: Optional[float] = Field(
        default=None,
        description="估计的每小时漂移（秒/小时，成对偏移斜率中位数）；"
        "证据不足时为 null",
    )
    last_offset_seconds: Optional[float] = Field(
        default=None,
        description="末次发送时刻的估计偏移（秒，残差中位数）；证据不足时为 null",
    )
    drift_exceeded: Optional[bool] = Field(
        default=None,
        description="绝对漂移是否严格超过请求上限；证据不足时为 null",
    )
    conclusion: Literal[
        "within_limit",
        "drift_exceeded",
        "insufficient_evidence",
    ] = Field(
        description="结论：within_limit=绝对漂移未超上限；"
        "drift_exceeded=绝对漂移超过上限；"
        "insufficient_evidence=有效样本不足三包、证据不足",
    )
    conclusion_detail: str = Field(description="结论的中文说明")
