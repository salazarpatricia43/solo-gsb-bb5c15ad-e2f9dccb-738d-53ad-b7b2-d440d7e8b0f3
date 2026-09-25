# 实验室仪器数据接收 API

基于 **FastAPI + SQLite** 的仪器批量测量包接收服务。仪器按设备上报带序号的测量包，服务提供：

- **幂等去重**：每个包携带幂等键，重试 / 重发不会产生重复数据或重复告警事件；
- **乱序缓冲**：序号不连续的包先暂存，缺口补齐后**按序连续提交**（告警也按该提交顺序评估）；
- **缺包作废**：确定丢失的连续序号段可登记作废以推进顺序前沿（段必须从 `next_expected` 开始、段内确为缺口、段尾不超过缓冲区最大序号），作废后立即排水提交随后连续的缓冲包；作废序号**不是测量包**，不进入已提交数据、不参与告警；
- **可观测接口**：查询每台设备的缺口（按连续缺口区间分页）、已提交序号、重复包记录与作废登记；
- **窗口聚合**：按事件时间（`sent_at`）把已提交测量分入半开窗口，输出 count / min / max / mean，并以水位机制隔离迟到样本；
- **持久化阈值告警**：按设备 + 指标配置上限 / 下限与连续触发、恢复次数，带连续计数的迟滞状态机防抖动，规则状态与告警 / 恢复事件持久化在 SQLite。
- **设备指标校准**：按设备 + 指标维护线性校准配置（`校准值 = 原始值 × scale + offset`），有效时间段互不重叠（事务内校验），按时刻解析唯一生效配置并计算校准值，不改写已存原始测量；窗口聚合支持可选的校准值模式（`calibrated=true`），按每个已提交包的 `sent_at` 解析当时生效的校准区间计算统计值，无配置沿用原始值。
- **只读归档导出**：按设备 + seq 范围创建一次性导出任务，返回 NDJSON 分页游标与 manifest（包数、首尾序号、规范化内容 SHA-256），快照不可变、续传字节稳定，并提供摘要校验接口；导出不触碰提交、缓冲、重复与告警数据。
- **双设备并行测量比对**：交接班时按设备、指标、UTC 时间段、最大时间偏差与允许数值偏差，只读比对参考仪器与待检仪器已提交包中的同指标读数；测量时间优先取 `ts`、否则取包 `sent_at`，保序一对一配对（先最大化配对数、再最小化时间差总和、平局按配对序号字典序决胜），返回逐对读数差与是否超差及两侧未配对汇总；不改写原始包、缓冲与告警。
- **双指标配比验收**：为每台设备保存分子 / 分母指标、各自单位、合格比例闭区间与最低有效组数（指标必须不同、下限不大于上限）；按设备 + 序号**闭区间**只读验收已提交包——每个包内两种指标按各自出现顺序**一对一配组、禁止跨包**，缺对应读数、单位不符或分母为零的组标无效并说明原因，有效组比例落在闭区间内才合格；整体须达到最低有效组数且所有有效组合格，返回逐组序号、包内位置、比例与结论；缓冲包与作废序号不参与计算。
- **仪器预热稳定放行**：按设备、指标、单位与 UTC 时间范围只读读取已提交包读数（测量时间优先 `ts`、否则包 `sent_at`，均无或单位不符的读数不参与），按 (时间, 包序号, 包内位置) 排序；以最大波动、最短持续秒数、最少读数与最大相邻采样间隔为条件，寻找**最早完成的连续稳定段**（同一完成时间取最早起点），返回放行结论、稳定段起止时间、读数序号 / 包序号 / 包内位置与实际波动；不存在合格段时不予放行。
- **超阈暴露量积分**：按设备、指标、单位、UTC 时间范围与阈值只读积分已提交包读数——测量时间优先 `ts`、否则包 `sent_at`，均无或单位不符的读数丢弃，按 (时间, 包序号, 包内位置) 排序且同一时刻只留最后一条；相邻读数间线性插值（间隔超过 `max_gap_seconds` 的整段不计），按 `[from, to)` 裁剪线段并切分阈值交点，对 `max(value-threshold, 0)` 积分，返回超阈秒数与超阈面积；范围外相邻读数仍参与边界插值，样本不足返回零。
- **设备时钟漂移诊断**：按设备、指标与包发送时间（`sent_at`）范围只读分析已提交包——缺 `sent_at` 的包整包跳过，每包只取该指标**首条带 `ts`** 的测量，时间统一为 UTC 后计算偏移 `ts - sent_at`，同一发送时刻只保留序号最大的一包；对不同发送时刻的全部样本对求偏移斜率，取**斜率中位数**估计每小时漂移，再以**残差中位数**估计末次发送时刻的偏移；返回样本数、漂移、末次偏移与绝对漂移是否超过请求上限的结论。不同发送时刻的有效样本不足三包时返回证据不足；范围内包数超过 200 返回 `422`。

## 快速开始

### Docker Compose（推荐，单命令）

```bash
docker compose up --build
```

这一条命令完成全部初始化：构建镜像 → 容器内安装依赖（`docker-entrypoint.sh`）→
应用启动钩子自动创建 SQLite 数据库与全部表结构（含告警规则、规则状态、告警事件、校准配置与双指标配比验收配置表）→ 启动服务。
数据库文件持久化在命名卷 `lab-data`（容器内 `/data/lab.db`），重启不丢规则与事件。

服务地址：http://localhost:8000 ，交互式文档：http://localhost:8000/docs

### 本地开发（单命令初始化 + 起服务）

```bash
pip install -r requirements-dev.txt   # 初始化依赖（含运行时与 pytest）
uvicorn app.main:app --reload         # 数据库默认创建在 ./lab.db，可用 DB_PATH 覆盖
```

另起终端运行测试：

```bash
pytest tests/
```

## 数据模型

仪器上报的测量包：

```json
{
  "device_id": "ph-meter-01",
  "seq": 12,
  "idempotency_key": "ph-meter-01-2026-09-22-0012",
  "sent_at": "2026-09-22T08:00:00Z",
  "measurements": [
    {"metric": "ph", "value": 7.01, "unit": "pH", "ts": "2026-09-22T07:59:58Z"},
    {"metric": "temperature", "value": 21.4, "unit": "C"}
  ]
}
```

- `seq`：包序号，**按设备**从 1 起单调递增；正常情况下连续，确认丢失的序号可通过缺包作废登记为永久缺口（见「缺包作废」）；
- `idempotency_key`：幂等键（也可通过 `Idempotency-Key` 请求头传入，二者至少提供一个）。

## 提交规则

每台设备独立维护 `committed_upto`（已连续**处理**的最大序号，处理 = 真实提交或缺包作废）：

1. **首包自举**：设备第一个到达的包立即提交，`committed_upto` 初始化为 `seq - 1`；
2. 之后到达的包：
   - `seq <= committed_upto` → 重复；已提交序号为 `already_committed`，已作废序号（缺包迟到）为 `already_voided`；
   - 已在缓冲区 → 重复（`already_buffered`）；
   - 否则放入缓冲区，并从 `committed_upto + 1` 起连续"排水"：缓冲中存在的包按序提交，已登记作废的序号直接跨过（不写入 committed），直到遇到下一个新缺口；
3. 同一幂等键重放 → 返回首次处理结果（`idempotent_replay`）；
4. 同一幂等键携带**不同载荷** → `409 Conflict`。

> 注意：作废后已提交序号集合**不再连续**（作废序号永久缺席），但顺序前沿
> `committed_upto` 仍单调推进、排水与告警严格按序号顺序进行。

所有摄入 / 作废操作共用同一把进程级串行锁（`ingest_lock`）并各自在单事务内
完成，保证并发下状态一致。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/packages` | 上报测量包（幂等、乱序缓冲、按序提交） |
| GET | `/api/v1/devices` | 设备列表及状态汇总 |
| GET | `/api/v1/devices/{id}/gaps` | 缺口：缓冲序号与缺失序号分页（`limit`/`offset`，按连续缺口区间计算，含 `gap_count`） |
| GET | `/api/v1/devices/{id}/committed` | 已提交序号（支持 `from_seq`/`to_seq`/`limit`/`offset`；作废序号不在其中） |
| GET | `/api/v1/devices/{id}/duplicates` | 重复包记录（可按 `reason` 过滤，含 `already_voided`） |
| POST | `/api/v1/devices/{id}/voids` | 登记缺包作废段（幂等；推进前沿并立即排水提交后续缓冲包） |
| GET | `/api/v1/devices/{id}/voids` | 按设备分页查询作废登记 |
| GET | `/api/v1/devices/{id}/metrics/rollup` | 按事件时间的窗口聚合（count/min/max/mean + 水位迟到统计，可选 `calibrated=true` 校准值口径） |
| GET | `/api/v1/devices/{id}/metrics/warmup-release` | 仪器预热稳定放行：在已提交读数中寻找最早完成的连续稳定段（只读） |
| GET | `/api/v1/devices/{id}/metrics/exposure` | 超阈暴露量积分：线性插值后对 `max(value-threshold,0)` 在 `[from,to)` 积分（只读） |
| GET | `/api/v1/devices/{id}/metrics/clock-drift` | 设备时钟漂移诊断：成对斜率中位数估计每小时漂移、残差中位数估计末次偏移（只读） |
| POST | `/api/v1/rules` | 创建阈值告警规则（按 设备+指标 唯一） |
| GET | `/api/v1/rules` | 规则列表（可按 `device_id`/`metric` 过滤） |
| GET | `/api/v1/rules/{rule_id}` | 规则详情 |
| PUT | `/api/v1/rules/{rule_id}` | 全量更新规则（上下限、连续次数、启用状态） |
| DELETE | `/api/v1/rules/{rule_id}` | 删除规则（历史事件保留） |
| GET | `/api/v1/alerts` | 当前仍处于告警中的规则（可按 `device_id`/`metric` 过滤） |
| GET | `/api/v1/devices/{id}/alerts/events` | 按设备、可选指标分页查询触发/恢复事件 |
| POST | `/api/v1/calibrations` | 创建设备指标线性校准配置（有效时间段不重叠，事务内校验） |
| GET | `/api/v1/devices/{id}/calibrations/resolve` | 按 `metric`/`at`/`value` 解析唯一生效配置并计算校准值（无配置返回 null） |
| POST | `/api/v1/exports` | 按设备 + seq 范围创建只读导出任务（manifest + 首页游标） |
| GET | `/api/v1/exports/{id}/manifest` | 查询导出 manifest |
| GET | `/api/v1/exports/{id}/pages?cursor=` | NDJSON 分页续传（响应头返回下一个游标） |
| POST | `/api/v1/exports/{id}/verify` | 提交导出内容 SHA-256 校验完整性 |
| GET | `/api/v1/comparisons` | 双设备同指标并行测量比对（交接班核对参考 / 待检仪器读数） |
| POST | `/api/v1/ratio-acceptances` | 为设备保存双指标配比验收配置（每台设备至多一条；指标须不同、下限不大于上限） |
| GET | `/api/v1/ratio-acceptances` | 配比验收配置列表（可按 `device_id` 过滤） |
| GET | `/api/v1/devices/{id}/ratio-acceptance` | 查询单台设备的配比验收配置 |
| GET | `/api/v1/devices/{id}/ratio-acceptance/evaluate?from_seq=&to_seq=` | 按设备 + 序号闭区间执行双指标配比验收（只读，逐组返回结论） |

## 缺口查询（Gaps）

`GET /api/v1/devices/{device_id}/gaps` 返回设备的缓冲序号与缺失序号。缺失序号
按**连续缺口区间**计算：`next_expected` 到缓冲区最大序号之间未被缓冲的序号
天然是若干段连续区间，服务端只做区间算术，**绝不展开缺失序号全集**——即使
设备先提交 `seq=1`、再缓冲 `seq=100000000`（近一亿个缺口），请求也在毫秒内
返回，不会阻塞或耗尽内存。

| 参数 | 必填 | 说明 |
|---|---|---|
| `limit` | 否 | 每页缺失序号条数，默认 `1000`，最大 `10000` |
| `offset` | 否 | 本页起始缺口偏移（从 0 起），默认 `0` |

响应字段：

- `gap_count`：`next_expected` 到缓冲区最大序号之间的缺失序号**总数**
  （准确值，与分页无关）；
- `gaps`：本页缺失序号（升序，最多 `limit` 条）；小范围查询与原有行为一致；
- `buffered_seqs`：当前缓冲区全部序号（升序，不受分页影响）；
- `limit` / `offset`：回显本次分页参数。

翻到深处（大 `offset`）时被跳过的整段缺口按区间长度算术跳过，不逐个遍历；
`offset` 达到或超过 `gap_count` 时本页 `gaps` 为空。未知设备返回 `404`。

```bash
# 已提交 1；100000000 在缓冲区 → 缺口为 2..99999999
curl 'localhost:8000/api/v1/devices/ph-meter-01/gaps?limit=1000&offset=0'
# => {"device_id":"ph-meter-01","committed_upto":1,"next_expected":2,
#     "buffered_seqs":[100000000],"gap_count":99999998,
#     "limit":1000,"offset":0,"gaps":[2,3,4, ..., 1001]}

# 最后一页：按 offset 直接定位，无需遍历被跳过的缺口
curl 'localhost:8000/api/v1/devices/ph-meter-01/gaps?limit=10&offset=99999995'
# => {...,"gap_count":99999998,"limit":10,"offset":99999995,
#     "gaps":[99999997,99999998,99999999]}
```

## 窗口聚合（Rollup）

`GET /api/v1/devices/{device_id}/metrics/rollup` 按**事件时间**（包的 `sent_at`）聚合**已提交**的测量：

| 参数 | 必填 | 说明 |
|---|---|---|
| `metric` | 是 | 指标名，只统计该指标的测量值 |
| `from` / `to` | 是 | 聚合范围 `[from, to)`，ISO 8601（不带时区按 UTC 处理） |
| `window_seconds` | 是 | 窗口宽度（秒），必须为正 |
| `allowed_lateness` | 否 | 允许的迟到时长（秒），默认 `0` |
| `calibrated` | 否 | 是否按校准值统计：`true` / `false`（`1`/`0`、`yes`/`no` 亦可），默认 `false` 统计原始值 |

语义：

- 窗口是以 `from` 为锚点的**半开区间** `[from + k·w, from + (k+1)·w)`，覆盖 `[from, to)`；窗口按 `start` 升序稳定返回，空窗口 `count=0` 且统计值为 `null`；
- **水位** `watermark = 设备已提交包的最大 sent_at - allowed_lateness`（设备级，与查询的指标、时间范围无关）；
- 迟到判定发生在**包提交时刻**：按提交顺序重放，若某包的 `sent_at` 早于当时水位，其样本为迟到样本——计入 `late_count`，**不改写已关闭窗口**的统计；
- 缓冲区中尚未提交的包不参与聚合；无 `sent_at` 的包无法分窗，跳过；
- **校准值模式**（`calibrated=true`）：对每个已提交包按其 **`sent_at`** 解析该（设备，指标）在该时刻唯一生效的校准区间，以 `校准值 = 原始值 × scale + offset` 参与 count/min/max/mean 统计；该时刻无生效配置（区间之前 / 之后 / 从未配置）时**沿用原始值**。响应中回显 `"calibrated": true`。迟到样本在任何模式下都只计入 `late_count`，**不解析校准、不入窗**；校准只在查询时计算，已存测量与归档导出始终保持原始值（见下文校准章节）；
- 单次查询窗口数上限 10000，超出返回 `422`（请增大 `window_seconds` 或缩小范围）。

示例：

```bash
# 默认：统计原始值
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/rollup?metric=temperature&from=2026-09-22T08:00:00Z&to=2026-09-22T09:00:00Z&window_seconds=300&allowed_lateness=60'
# => {"device_id":"ph-meter-01","metric":"temperature","from":"...","to":"...",
#     "window_seconds":300.0,"allowed_lateness":60.0,"calibrated":false,
#     "watermark":"2026-09-22T08:59:00Z","late_count":0,
#     "windows":[{"start":"...","end":"...","count":3,"min":21.0,"max":21.6,"mean":21.3}, ...]}

# 校准值模式：按每个包 sent_at 当时生效的校准区间计算
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/rollup?metric=temperature&from=2026-09-22T08:00:00Z&to=2026-09-22T09:00:00Z&window_seconds=300&calibrated=true'
# => {..., "calibrated":true,
#     "windows":[{"start":"...","end":"...","count":3,"min":21.92,"max":22.532,"mean":22.226}, ...]}
# （示例配置 scale=1.02、offset=0.5；无生效配置的包仍按原始值计入同一窗口）
```

## 阈值告警（Threshold Alerts）

按 **设备 + 指标** 配置规则（同一组合仅一条）：

| 字段 | 说明 |
|---|---|
| `upper_limit` / `lower_limit` | 上限 / 下限，**至少提供一个**；严格比较：`value > 上限` 或 `value < 下限` 才算越界，等于阈值不越界 |
| `trigger_count` | 连续越界多少次后触发告警（默认 1） |
| `recovery_count` | 告警后连续多少次不越界才恢复（默认 1），与触发阈值相互独立，形成迟滞 |
| `enabled` | 是否启用；禁用期间不评估，重新启用不回溯历史包 |

### 迟滞状态机（防抖动）

规则状态持久化在 `alert_rule_states`，每条规则一个状态机：

- **normal**：维护连续越界计数 `breach_streak`，越界 +1、不越界清零；达到 `trigger_count` 时翻转为 **triggered** 并写入一条 `triggered` 事件；
- **triggered**：维护连续恢复计数 `recovery_streak`，不越界 +1、再次越界清零（抖动被吸收，不会反复告警）；达到 `recovery_count` 时翻回 **normal** 并写入一条 `recovered` 事件；
- 包内没有该指标的测量值时该规则**跳过、计数不变**（缺数不等于恢复）；一个包内含同一指标多个测量值时，按 measurements 的提交顺序逐个推进状态机。

### 评估时机：只认提交顺序

- 评估只发生在包**按序排水提交**时：缓冲中的包不评估，重复包（幂等重放 / `already_committed` / `already_buffered`）直接短路，**绝不重复产生事件**；
- 乱序缺口被补齐时，缓冲包按 seq 升序一次性排水，每个包**只按这一次提交顺序**评估；
- 规则在建规则之前已提交的历史包**不回溯**；
- 规则配置被更新时，保留当前是否告警的状态，但清零两个连续计数，避免沿用旧阈值的计数误判；
- 删除规则会级联删除其状态（当前告警消失），历史事件保留、`rule_id` 置空。

事件的**时间与序号**：

- 事件序号 `id` 为全表单递增自增，即按实际提交处理顺序编号；`seq` 为触发 / 恢复时处理的包序号（同包内多个测量值可触发+恢复，序号相同、`id` 不同）；
- 事件时间优先取测量值 `ts`，其次取包的 `sent_at`，均无则用提交时刻（UTC，`Z` 结尾）。

### 接口示例

创建规则（温度上限 30：连续 2 次越界触发，连续 2 次回限内恢复）：

```bash
curl -X POST localhost:8000/api/v1/rules -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "metric": "temperature",
  "upper_limit": 30, "trigger_count": 2, "recovery_count": 2
}'
# => 201 {"id":1,"device_id":"ph-meter-01","metric":"temperature",
#        "upper_limit":30.0,"lower_limit":null,"trigger_count":2,
#        "recovery_count":2,"enabled":true,"created_at":"...","updated_at":"..."}
```

更新（全量替换）/ 删除：

```bash
curl -X PUT localhost:8000/api/v1/rules/1 -H 'Content-Type: application/json' -d '{
  "upper_limit": 40, "lower_limit": 0, "trigger_count": 3,
  "recovery_count": 2, "enabled": true
}'
curl -X DELETE localhost:8000/api/v1/rules/1      # 204；历史事件保留
```

上报测量包（接口与前述完全一致，提交时自动评估）：

```bash
curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "seq": 1, "idempotency_key": "k-1",
  "measurements": [{"metric": "temperature", "value": 31.2, "ts": "2026-09-22T08:00:00Z"}]
}'
```

查询当前告警（触发后、恢复前）：

```bash
curl 'localhost:8000/api/v1/alerts?device_id=ph-meter-01'
# => {"count":1,"alerts":[{"rule_id":1,"device_id":"ph-meter-01","metric":"temperature",
#        "upper_limit":30.0,"lower_limit":null,"trigger_count":2,"recovery_count":2,
#        "triggered_seq":4,"triggered_event_id":7,"triggered_at":"2026-09-22T08:00:04Z",
#        "last_value":33.0,"last_seq":4,"recovery_streak":0}]}
```

按设备、指标分页查询事件（`limit`/`offset`，可按 `event_type=triggered|recovered` 过滤）：

```bash
curl 'localhost:8000/api/v1/devices/ph-meter-01/alerts/events?metric=temperature&limit=50&offset=0'
# => {"count":2,"limit":50,"offset":0,"events":[
#       {"id":7,"rule_id":1,"device_id":"ph-meter-01","metric":"temperature","seq":4,
#        "event_type":"triggered","value":33.0,"event_time":"2026-09-22T08:00:04Z","created_at":"..."},
#       {"id":8,"rule_id":1,"device_id":"ph-meter-01","metric":"temperature","seq":8,
#        "event_type":"recovered","value":27.0,"event_time":"2026-09-22T08:00:08Z","created_at":"..."}]}
```

## 缺包作废（Voids）

某些缺口序号经确认永远不会到达（丢包 / 设备跳过），可以把一段**连续缺口**
登记作废，让顺序前沿越过它们继续前进。作废与摄入共用串行锁与数据库事务。

### 允许作废的条件（全部满足，任一不满足返回 `409`）

- 设备**必须已存在**（至少接收过一个包）；未知设备返回 `404`；
- 起点 `from_seq` 必须**恰好等于当前 `next_expected`**（= `committed_upto + 1`），
  不能跳过未处理序号，也不能作废已处理区间；
- 段内 `[from_seq, to_seq]` 每个序号当前都**确为缺口**：
  已提交、已缓冲、已作废的序号都不得作废；
- 段尾 `to_seq` **不得超过当前缓冲区最大序号**——缓冲区之外没有已知边界，
  无缓冲时不存在可作废的缺口。

作废处理（同一事务内）：

1. 在 `void_records` 登记作废段（只登记"缺席"事实，**不构造任何测量包**）；
2. 推进顺序前沿并立即排水：作废序号被跨过，其后连续的缓冲包按序提交到
   `committed`，`committed_upto` 更新为"提交 / 作废"连续覆盖到的最大序号；
3. 排水沿用现有告警评估：只有真实提交的缓冲包进入告警状态机，
   **作废序号绝不被伪装成测量包**，不产生任何告警事件。

作废之后迟到的原包按重复处理（`already_voided`），不会复活、不会写入 committed。

### 登记作废

`POST /api/v1/devices/{device_id}/voids`

| 字段 | 必填 | 说明 |
|---|---|---|
| `from_seq` | 是 | 作废段起点（含），必须等于当前 `next_expected`，`>= 1` |
| `to_seq` | 是 | 作废段终点（含），不得小于 `from_seq`，不得超过缓冲区最大序号 |
| `idempotency_key` | 否 | 幂等键；也可用 `Idempotency-Key` 请求头传入，二者至少提供一个 |

```bash
# 已提交 1；2、3 确认丢失；4、5 已在缓冲区等待
curl -X POST localhost:8000/api/v1/devices/ph-meter-01/voids \
  -H 'Content-Type: application/json' \
  -d '{"from_seq": 2, "to_seq": 3, "idempotency_key": "void-ph-meter-01-2-3"}'
# => 200
# {"status":"voided","device_id":"ph-meter-01","from_seq":2,"to_seq":3,
#  "voided_seqs":[2,3],"committed_upto":5,"committed_seqs":[4,5]}
```

- `committed_seqs`：作废后**立即排水提交的真实缓冲包**（不含作废序号）；
- 同一作废请求（同幂等键同载荷）重试：原样返回首次结果，**不重复登记、
  不重复推进、不重复排水**；
- 同幂等键携带不同载荷，或请求与当前状态冲突（起点不是 `next_expected`、
  段内含缓冲包、段尾越过缓冲边界等）→ `409 Conflict`，`detail` 给出具体原因；
- `from_seq > to_seq`、序号小于 1、缺少幂等键 → `422`。

### 查询作废记录

`GET /api/v1/devices/{device_id}/voids`（`limit` 默认 100、最大 1000，`offset` 分页，
按登记先后升序）：

```bash
curl localhost:8000/api/v1/devices/ph-meter-01/voids
# => {"device_id":"ph-meter-01","committed_upto":5,"next_expected":6,
#     "count":1,"voided_count":2,
#     "records":[{"id":1,"device_id":"ph-meter-01","from_seq":2,"to_seq":3,
#                 "voided_seqs":[2,3],"created_at":"..."}]}
```

`GET /api/v1/devices` 的每个设备状态另含 `voided_count`（已作废序号总数）。

### 序号语义与导出

- 作废序号在**已提交数据中永久缺席**：`committed` 表与
  `/devices/{id}/committed` 只含真实包，因此作废之后已提交序号可能不连续，
  而 `committed_upto` / `next_expected` 仍单调推进；
- 归档导出（见下文）**只包含真实已提交包**；当请求范围与任何作废段相交时，
  manifest 的 `complete=false`（无法保证范围被真实数据连续覆盖），
  但导出内容与 `content_sha256` 仍只对应真实包，作废序号不会出现在 NDJSON 中。

## 设备指标校准（Calibrations）

按 **设备 + 指标** 维护线性校准配置：`校准值 = 原始值 × scale + offset`。
每条配置声明一个半开有效区间 `[valid_from, valid_to)`（`valid_to` 缺省表示无限期），
同一 (设备, 指标) 的有效区间**不得重叠**——创建时在同一事务内完成重叠校验与写入，
重叠返回 `409`，因此任意时刻至多一条配置生效。

校准只在查询时计算，**不改写已存储的原始测量**：已提交数据、归档导出始终保持
原始值；窗口聚合默认（`calibrated=false`）同样统计原始值，仅在显式传
`calibrated=true` 时按校准值统计（见「窗口聚合」一节）。

### 创建配置

`POST /api/v1/calibrations`

| 字段 | 必填 | 说明 |
|---|---|---|
| `device_id` / `metric` | 是 | 设备号与指标名 |
| `valid_from` | 是 | 生效起点（含），ISO 8601；不带时区按 UTC |
| `valid_to` | 否 | 生效终点（不含），ISO 8601；缺省表示无限期 |
| `offset` | 否 | 线性偏移，默认 `0` |
| `scale` | 否 | 线性增益，默认 `1` |

```bash
curl -X POST localhost:8000/api/v1/calibrations -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "metric": "temperature",
  "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z",
  "offset": 0.5, "scale": 1.02
}'
# => 201 {"id":1,"device_id":"ph-meter-01","metric":"temperature",
#        "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z",
#        "offset":0.5,"scale":1.02,"created_at":"..."}
```

错误：`valid_from >= valid_to` → `422`；与同 (设备, 指标) 已有配置的有效区间重叠
→ `409`（相接的半开区间，如一段终点等于另一段起点，不算重叠）。

### 按时刻解析并计算校准值

`GET /api/v1/devices/{device_id}/calibrations/resolve?metric=&at=&value=`

| 参数 | 必填 | 说明 |
|---|---|---|
| `metric` | 是 | 指标名 |
| `at` | 是 | 生效时刻，ISO 8601（不带时区按 UTC）；区间边界为 `[valid_from, valid_to)` |
| `value` | 是 | 原始测量值 |

```bash
curl 'localhost:8000/api/v1/devices/ph-meter-01/calibrations/resolve?metric=temperature&at=2026-09-15T08:00:00Z&value=21.4'
# => {"device_id":"ph-meter-01","metric":"temperature","at":"2026-09-15T08:00:00Z",
#     "value":21.4,"calibrated_value":22.328,
#     "calibration":{"id":1,"device_id":"ph-meter-01","metric":"temperature",
#                    "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z",
#                    "offset":0.5,"scale":1.02,"created_at":"..."}}
```

无生效配置时 HTTP 仍为 `200`，`calibration` 与 `calibrated_value` 均为 `null`。

### 窗口聚合的校准值模式（calibrated=true）

窗口聚合接口 `GET /api/v1/devices/{device_id}/metrics/rollup` 额外支持布尔查询参数
`calibrated`（默认 `false`）：

- `false` / 缺省：count / min / max / mean 全部基于**原始值**，与校准配置是否存在无关；
- `true`：服务把该（设备，指标）的全部校准区间一次性载入，对**每个已提交包**按其
  `sent_at` 二分解析当时唯一生效的区间（边界同样是 `[valid_from, valid_to)`），
  包内该指标的每个测量值统一按 `原始值 × scale + offset` 转换后再入窗；该包
  `sent_at` 时刻无生效配置时，包内样本**沿用原始值**——因此同一窗口中可能既有
  校准值（命中配置的包）也有原始值（未命中的包）。

其余语义保持不变：

- 迟到样本仍只计入 `late_count`，不解析校准、不进入任何窗口，已关闭窗口的统计不被改写；
- 校准只发生在本次查询期间，可任意重复调用；**已存测量与归档导出（NDJSON / manifest
  摘要）始终是原始值**，水位、窗口划分与 `late_count` 也与原始值模式完全一致；
- 响应体回显 `"calibrated": true|false`，便于下游区分两种口径。

```bash
# 先配置一段生效区间
curl -X POST localhost:8000/api/v1/calibrations -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "metric": "temperature",
  "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z",
  "offset": 0.5, "scale": 1.02
}'

# 原始值口径（默认）
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/rollup?metric=temperature&from=2026-09-22T08:00:00Z&to=2026-09-22T09:00:00Z&window_seconds=300'
# 校准值口径：21.4 -> 21.4*1.02+0.5 = 22.328
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/rollup?metric=temperature&from=2026-09-22T08:00:00Z&to=2026-09-22T09:00:00Z&window_seconds=300&calibrated=true'
# => {"device_id":"ph-meter-01", ..., "calibrated":true,
#     "windows":[{"start":"...","end":"...","count":3,"min":21.92,"max":22.532,"mean":22.226}, ...]}
```

## 只读归档导出（Exports）

对某台设备按 **seq 范围**创建一次只读导出任务。任务在创建时刻把范围内
**当时已提交**的包复制为不可变快照（独立的 `export_tasks` / `export_items` 表），
之后设备继续摄入、排水提交都不会改变已有导出的内容与摘要；缓冲包、重复包、
告警规则与事件**全程不参与、也不被修改**。

快照复制在**同一 SQLite 事务内分批流式**完成：按 seq 升序以 keyset
（`seq > 上批末序号`）逐批读取 `committed`、逐批写入 `export_items`，
内容 SHA-256、包数与首尾序号都在扫描中逐行累计，任何时刻内存中只保留一批
（每批 1000 行）——**即使请求是无界范围、命中包量巨大，创建请求的内存占用
也只与批大小有关，不会预先把全部 NDJSON 行装入内存**。创建途中任何一步失败，
整次创建（含任务行与已写入的快照行）随事务**整体回滚**，不会留下半成品任务。

### 创建任务

`POST /api/v1/exports`

| 字段 | 必填 | 说明 |
|---|---|---|
| `device_id` | 是 | 设备号 |
| `from_seq` / `to_seq` | 否 | 闭区间范围；缺省分别表示从首包开始 / 到创建时刻已提交末包为止 |
| `page_size` | 否 | 每页 NDJSON 行数，默认 100，范围 1..1000（游标中固化，续传时不可变） |

```bash
curl -X POST localhost:8000/api/v1/exports -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "from_seq": 1, "to_seq": 100, "page_size": 50
}'
# => 201
# {"export_id":"a1b2c3...","device_id":"ph-meter-01","from_seq":1,"to_seq":100,
#  "page_size":50,"package_count":100,"first_seq":1,"last_seq":100,
#  "complete":true,"content_sha256":"9f86d0...","created_at":"...",
#  "next_cursor":"eyJpZCI6..."}
```

Manifest 字段：

- `package_count`：快照包数；`first_seq` / `last_seq`：快照首尾序号（空导出为 `null`）；
- `complete`：请求范围是否被创建时刻的已提交数据**连续完整覆盖**——
  范围越出已提交边界、范围内仍有缺口（缓冲未排水）、或范围与任何**缺包作废段**
  相交时为 `false`，提示调用方快照只是部分内容（作废序号永久缺席，不在导出中）；
- `content_sha256`：全量导出内容（按 seq 升序逐行 NDJSON、UTF-8）的 SHA-256。

### NDJSON 分页与游标续传

`GET /api/v1/exports/{export_id}/pages?cursor=...`（无 cursor 时从首页开始）

- 响应体为 **NDJSON**（`application/x-ndjson`）：每个已提交包一行，
  行内容是规范化（键排序）后的测量包 JSON；整段内容与 manifest 的 `content_sha256` 对应；
- 响应头：
  - `X-Next-Cursor`：下一页游标；**末页不返回**；
  - `X-Export-Done`：`true` / `false`，本页是否最后一页；
  - `X-Page-Offset`：本页起始行偏移；`X-Package-Count`：快照总包数；
  - `X-Content-SHA256`：与 manifest 相同的内容摘要。
- **稳定性**：同一游标可任意重复请求，返回字节完全一致；逐页拼接后计算
  `sha256` 必然等于 manifest 摘要，可边下边校验。
- 错误：任务不存在 → `404`；游标损坏、属于其他任务、页大小不匹配或偏移越界
  （例如末页之后继续翻页）→ `400`，`detail` 给出具体原因。

```bash
# 首页
curl -D headers.txt localhost:8000/api/v1/exports/$EID/pages -o page0.ndjson
NEXT=$(grep -i '^X-Next-Cursor:' headers.txt | cut -d' ' -f2 | tr -d '\r')
# 续传
curl localhost:8000/api/v1/exports/$EID/pages?cursor="$NEXT" -o page1.ndjson
# 校验（逐页拼接等价于对全量内容摘要）
cat page0.ndjson page1.ndjson | sha256sum
```

### Manifest 查询与完整性校验

```bash
curl localhost:8000/api/v1/exports/$EID/manifest

curl -X POST localhost:8000/api/v1/exports/$EID/verify \
  -H 'Content-Type: application/json' \
  -d '{"content_sha256":"9f86d0..."}'
# => 200（注意：摘要不匹配时业务结果 valid=false，HTTP 状态仍为 200；任务不存在才是 404）
# {"valid":true,"expected_sha256":"9f86d0...","received_sha256":"9f86d0...",
#  "manifest":{ ... }}
```

### 示例

首包自举提交（seq 1），乱序包 seq 3 先到，进入缓冲：

```bash
curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "seq": 1, "idempotency_key": "k-1",
  "measurements": [{"metric": "ph", "value": 6.98}]
}'
# => {"status":"committed","committed_seqs":[1],"committed_upto":1,...}

curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "seq": 3, "idempotency_key": "k-3",
  "measurements": [{"metric": "ph", "value": 7.01}]
}'
# => {"status":"buffered","committed_upto":1,"committed_seqs":[],...}
```

查询缺口（缺口巨大时的分页用法详见「缺口查询」一节）：

```bash
curl localhost:8000/api/v1/devices/ph-meter-01/gaps
# => {"committed_upto":1,"next_expected":2,"buffered_seqs":[3],
#     "gap_count":1,"limit":1000,"offset":0,"gaps":[2]}
```

补齐缺口后按序提交：

```bash
curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "seq": 2, "idempotency_key": "k-2",
  "measurements": [{"metric": "ph", "value": 7.05}]
}'
# => {"status":"committed","committed_seqs":[2,3],"committed_upto":3,...}
```

重复重放与查询：

```bash
curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '<同上请求体>'
# => {"status":"duplicate","duplicate_reason":"idempotent_replay",...}

curl localhost:8000/api/v1/devices/ph-meter-01/committed    # 已提交序号
curl localhost:8000/api/v1/devices/ph-meter-01/duplicates   # 重复包记录
```

缺包作废（详见「缺包作废」一节）：

```bash
# 1 已提交；2、3 确认丢失；4 已缓冲
curl -X POST localhost:8000/api/v1/devices/ph-meter-01/voids \
  -H 'Content-Type: application/json' \
  -d '{"from_seq":2,"to_seq":3,"idempotency_key":"void-0012"}'
# => {"status":"voided","voided_seqs":[2,3],"committed_upto":4,"committed_seqs":[4]}

# committed 只有真实包：1、4（作废序号 2、3 永久缺席）
curl localhost:8000/api/v1/devices/ph-meter-01/committed
# 范围含作废序号的导出 complete=false，NDJSON 仍只有真实包
```

## 双设备并行测量比对（Comparisons）

交接班时核对**参考仪器**与**待检仪器**在同一时间段内对同一指标的读数。
接口是**纯只读**的：仅查询两侧 `committed` 表中已提交包的原始 payload，
不写入任何数据，也不改写原始包、缓冲、重复、作废、告警与校准。

`GET /api/v1/comparisons`

| 参数 | 必填 | 说明 |
|---|---|---|
| `reference_device` | 是 | 参考仪器设备号 |
| `target_device` | 是 | 待检仪器设备号（不得与参考设备相同） |
| `metric` | 是 | 指标名，只取该指标的测量 |
| `from` / `to` | 是 | UTC 时间段，半开区间 `[from, to)`，ISO 8601（不带时区按 UTC） |
| `max_time_delta_seconds` | 是 | 允许的最大时间偏差（秒），`>= 0`；两侧时间差不超过该值才允许配对 |
| `allowed_value_diff` | 是 | 允许的读数绝对数值偏差，`>= 0`；绝对差**严格大于**该值标记 `exceeded=true`（相等不算超差） |

候选测量与排序：

- 每条测量的有效时间**优先取自身 `ts`，否则取所属包的 `sent_at`**；
  两者都没有的测量直接**忽略**；
- 仅保留有效时间落在 `[from, to)` 内的测量；
- 同一侧按 **(有效时间, 包序号 seq, 包内位置 position)** 升序排序——
  同时间先按包序号、再按该测量在包 `measurements` 中的原始下标排列；
- 缓冲区中尚未提交的包不参与；单侧候选**硬上限 200 条，超过直接
  `422` 拒绝（明确报错，绝不截断）**，请缩小 UTC 时间范围后重试。

配对规则（保序二分匹配，每条测量最多配对一次）：

- 仅当两侧**单位相同**（两侧都未填 `unit` 视为相同；仅一侧缺失视为不同）
  且有效时间差绝对值 **不超过** `max_time_delta_seconds` 时才能配对；
- 配对保持两侧各自的时间顺序（不交叉）；优化目标依次为：
  ① 最大化配对数；② 最小化全部配对的时间差总和；
  ③ 仍平局时按排序后的配对序号序列 `((ref_idx, target_idx), ...)`
  字典序最小决胜——结果唯一确定，重复调用完全一致。

响应：逐对返回配对时间（取参考侧有效时间）、两侧的包序号 / 包内位置 /
有效时间 / 时间来源（`ts` 或 `sent_at`）/ 读数 / 单位、时间差（秒）、
绝对数值差、是否超差；汇总给出两侧候选数、配对数与两侧未配对数、
超差配对数。未知设备返回 `404`；两侧同设备、时间范围倒置、候选超限等
返回 `422`。

```bash
curl 'localhost:8000/api/v1/comparisons?reference_device=ph-ref-01&target_device=ph-dut-02&metric=temperature&from=2026-09-23T08:00:00Z&to=2026-09-23T09:00:00Z&max_time_delta_seconds=5&allowed_value_diff=0.5'
# => {"reference_device":"ph-ref-01","target_device":"ph-dut-02","metric":"temperature",
#     "from":"2026-09-23T08:00:00Z","to":"2026-09-23T09:00:00Z",
#     "max_time_delta_seconds":5.0,"allowed_value_diff":0.5,
#     "reference_count":3,"target_count":3,"paired_count":2,
#     "reference_unpaired_count":1,"target_unpaired_count":1,"exceeded_count":1,
#     "pairs":[{"pair_time":"2026-09-23T08:00:10Z",
#       "reference":{"device_id":"ph-ref-01","seq":1,"position":0,
#                    "time":"2026-09-23T08:00:10Z","time_source":"ts",
#                    "value":20.0,"unit":"C"},
#       "target":{"device_id":"ph-dut-02","seq":1,"position":0,
#                 "time":"2026-09-23T08:00:12Z","time_source":"ts",
#                 "value":20.3,"unit":"C"},
#       "time_delta_seconds":2.0,"abs_value_diff":0.3,"exceeded":false}, ...]}
```

## 双指标配比验收（Ratio Acceptance）

对一台设备保存一组验收配置，随后按**序号闭区间**对已提交包做只读验收。
例如用「加标回收率 = 检出量 / 加入量」核对：两种指标在同一个测量包内
成对上报，逐包一对一配组并检查比例是否落在允许区间。接口是纯只读的，
不写入任何数据，也不改写原始包、缓冲、重复、作废、告警与校准。

### 保存配置

`POST /api/v1/ratio-acceptances`（每台设备至多一条，重复配置返回 `409`）

| 字段 | 必填 | 说明 |
|---|---|---|
| `device_id` | 是 | 设备号 |
| `numerator_metric` / `denominator_metric` | 是 | 分子指标名 / 分母指标名，**二者必须不同** |
| `numerator_unit` / `denominator_unit` | 是 | 分子 / 分母读数要求的单位（非空字符串，读数单位须逐字相同；读数缺单位视为不符） |
| `ratio_lower` / `ratio_upper` | 是 | 合格比例闭区间下限（含）/ 上限（含），**下限不得大于上限**（相等表示单点区间） |
| `min_valid_groups` | 是 | 整体验收要求达到的最低有效组数，`>= 1` |

```bash
curl -X POST localhost:8000/api/v1/ratio-acceptances -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01",
  "numerator_metric": "recovered", "denominator_metric": "added",
  "numerator_unit": "mg", "denominator_unit": "mg",
  "ratio_lower": 0.9, "ratio_upper": 1.1, "min_valid_groups": 3
}'
# => 201 {"id":1,"device_id":"ph-meter-01",
#        "numerator_metric":"recovered","denominator_metric":"added",
#        "numerator_unit":"mg","denominator_unit":"mg",
#        "ratio_lower":0.9,"ratio_upper":1.1,"min_valid_groups":3,
#        "created_at":"...","updated_at":"..."}
```

校验错误（两指标相同、下限大于上限、`min_valid_groups < 1`、缺单位）→ `422`。
查询配置：`GET /api/v1/ratio-acceptances`（可加 `?device_id=` 过滤）、
`GET /api/v1/devices/{id}/ratio-acceptance`（未配置 `404`）。

### 按序号闭区间验收

`GET /api/v1/devices/{device_id}/ratio-acceptance/evaluate?from_seq=&to_seq=`

两个参数均必填、`>= 1`，区间为**闭区间 `[from_seq, to_seq]`**（两端都含）。
计算规则：

- **只读取已提交包**：区间内按 seq 升序扫描 `committed` 真实包；仍在缓冲区
  的包不参与，作废序号本就不在 committed 中、也不参与（例如区间含作废段时
  结果里不会出现那些序号）；
- **包内一对一配组、禁止跨包**：在每个包内分别按 `measurements` 的出现顺序
  收集分子指标与分母指标的读数（保留各自原始下标），两侧第 0、1、2… 个
  读数配成第 0、1、2… 组；某一侧读数更多时，残余读数各自成组（不与下一个包
  的读数配组）；逐组序号 `index` 从 1 起，按包序号与包内位置**跨包连续**编号；
- **无效组**（不参与比例合格判定，但拉低有效组数）按以下优先级取首个原因：
  - `missing_numerator` / `missing_denominator`：该组缺少对应一侧读数
    （含"另一包多出一个读数"的残余情况），`invalid_reason_detail` 说明包序号
    与包内组号；
  - `unit_mismatch`：分子或分母读数单位与配置要求不符（**读数未填单位同样
    视为不符**），说明文字给出是哪一侧、实际单位与要求单位；
  - `zero_denominator`：分母读数为零，比例不可计算；
- **有效组**比例 = 分子读数 / 分母读数，闭区间判定：
  `ratio_lower <= ratio <= ratio_upper`（**边界合格**），在区间内结论
  `pass`，否则 `fail`；
- **整体结论** `accepted=true` 当且仅当
  `有效组数 >= min_valid_groups` **且** 不存在 `fail` 的有效组；
  否则 `accepted=false` 并在 `not_accepted_reason` 说明（比例超界给出组序号
  与区间，或有效组数不足给出实际数与要求数）。

```bash
curl 'localhost:8000/api/v1/devices/ph-meter-01/ratio-acceptance/evaluate?from_seq=1&to_seq=10'
# => 200
# {"device_id":"ph-meter-01","from_seq":1,"to_seq":10,
#  "config":{ ... 回显本次使用的配置 ... },
#  "package_count":4,"group_count":4,
#  "valid_group_count":3,"invalid_group_count":1,"failed_group_count":0,
#  "accepted":true,"not_accepted_reason":null,
#  "groups":[
#    {"index":1,"seq":1,"position":0,
#     "numerator":{"seq":1,"position":1,"value":9.5,"unit":"mg","unit_matches":true},
#     "denominator":{"seq":1,"position":0,"value":10.0,"unit":"mg","unit_matches":true},
#     "valid":true,"invalid_reason":null,"invalid_reason_detail":null,
#     "ratio":0.95,"conclusion":"pass"},
#    {"index":2,"seq":2,"position":0,
#     "numerator":{...,"value":12.0,...},"denominator":{...,"value":10.0,...},
#     "valid":true,"invalid_reason":null,"ratio":1.2,"conclusion":"fail"},
#    {"index":3,"seq":3,"position":0,
#     "numerator":null,
#     "denominator":{"seq":3,"position":0,"value":10.0,"unit":"mg","unit_matches":true},
#     "valid":false,"invalid_reason":"missing_numerator",
#     "invalid_reason_detail":"包 3 的第 1 组缺少分子指标读数，无法配组",
#     "ratio":null,"conclusion":"invalid"},
#    {"index":4,"seq":4,"position":0, ... "invalid_reason":"zero_denominator", ...}
#  ]}
```

说明：`position` 是该组在**所属包内**的组序号（两种指标各自序列中的下标，
从 0 起）；`numerator` / `denominator` 内的 `position` 是读数在包
`measurements` 中的原始下标；无效组与分母为零的组 `ratio` 为 `null`。
未知设备或设备尚无配置 → `404`；区间倒置 / 序号小于 1 → `422`；
区间内没有任何组时 HTTP 仍为 `200`，`accepted=false`（有效组数 0 未达最低要求）。

## 仪器预热稳定放行（Warmup Release）

仪器开机预热后，需要确认读数在一段时间内足够平稳才可放行使用。接口
`GET /api/v1/devices/{device_id}/metrics/warmup-release` 是**纯只读**的：
仅查询该设备 `committed` 表中已提交包的原始 payload，不写入任何数据，
也不改写原始包、缓冲、重复、作废、告警与校准。

| 参数 | 必填 | 说明 |
|---|---|---|
| `metric` | 是 | 指标名，只取该指标的测量 |
| `unit` | 是 | 要求的读数单位（非空字符串，读数单位须逐字相同；读数缺单位视为不符） |
| `from` / `to` | 是 | UTC 时间范围，半开区间 `[from, to)`，ISO 8601（不带时区按 UTC） |
| `max_fluctuation` | 是 | 稳定段允许的最大波动：段内最大读数 - 最小读数**不超过**该值（边界合格），`>= 0` |
| `min_duration_seconds` | 是 | 稳定段首末读数有效时间跨度至少达到的秒数，`> 0`（边界合格） |
| `min_readings` | 是 | 稳定段至少包含的读数条数，`>= 1`（边界合格） |
| `max_gap_seconds` | 是 | 稳定段内相邻两条读数的最大允许采样间隔（秒），`>= 0`（边界合格） |

候选读数与排序：

- 每条测量的有效时间**优先取自身 `ts`，否则取所属包的 `sent_at`**；
  两者都没有的测量**不参与**（计入 `excluded_no_time_count`）；
- 仅保留有效时间落在 `[from, to)` 内、且单位与 `unit` 逐字相同的测量；
  单位不符（含未填单位）的读数**不参与**（计入
  `excluded_unit_mismatch_count`，时间范围外的读数不计入该计数）；
- 候选按 **(有效时间, 包序号 seq, 包内位置 position)** 升序排序，
  缓冲区中尚未提交的包不参与。

稳定段判定：在候选序列上寻找一段**连续**读数，同时满足

1. 段内最大值与最小值之差（`fluctuation`）**不超过** `max_fluctuation`；
2. 段内任意相邻读数的有效时间差**不超过** `max_gap_seconds`——相邻间隔
   超限处序列视为断裂，稳定段**不得跨断裂**；
3. 首末读数的时间跨度**达到** `min_duration_seconds`；
4. 段内读数条数**不少于** `min_readings`。

服务端返回**最早完成**（完成时间 = 段末读数的有效时间）的合格段；
同一完成时间存在多段时取**起点最早**的一段（即该完成时刻可行的最长段）。
没有任何合格段时 HTTP 仍为 `200`，`released=false`、`segment=null` 并给出
`not_released_reason`。未知设备 → `404`；时间范围倒置、阈值为负、
`min_duration_seconds <= 0`、`min_readings < 1`、缺参数等 → `422`。

```bash
curl 'localhost:8000/api/v1/devices/oven-7/metrics/warmup-release?metric=temperature&unit=C&from=2026-09-24T08:00:00Z&to=2026-09-24T09:00:00Z&max_fluctuation=0.5&min_duration_seconds=20&min_readings=3&max_gap_seconds=15'
# => 200
# {"device_id":"oven-7","metric":"temperature","unit":"C",
#  "from":"2026-09-24T08:00:00Z","to":"2026-09-24T09:00:00Z",
#  "max_fluctuation":0.5,"min_duration_seconds":20.0,"min_readings":3,
#  "max_gap_seconds":15.0,
#  "candidate_count":4,"excluded_no_time_count":1,
#  "excluded_unit_mismatch_count":1,"released":true,
#  "segment":{"start_time":"2026-09-24T08:00:00Z",
#             "end_time":"2026-09-24T08:00:20Z","duration_seconds":20.0,
#             "reading_count":3,"min_value":20.0,"max_value":20.2,
#             "fluctuation":0.2,"max_adjacent_gap_seconds":10.0,
#             "readings":[{"index":1,"seq":1,"position":0,
#                          "time":"2026-09-24T08:00:00Z","time_source":"ts",
#                          "value":20.0,"unit":"C"}, ...]},
#  "not_released_reason":null}
```

段内每条读数给出段内序号 `index`（从 1 起）、所属包序号 `seq`、
包内原始下标 `position`、有效时间与来源（`ts` / `sent_at`）、读数与单位；
段上的 `fluctuation` 为实际波动（最大 - 最小），
`max_adjacent_gap_seconds` 为段内实际最大相邻采样间隔。

## 超阈暴露量积分（Exposure）

计算某台设备某项指标的读数曲线**超过阈值的幅度**在一段时间内的累计暴露：
超阈持续多少秒（`exposure_seconds`）、超阈面积（`exposure_area`，
读数单位·秒，即 ∫ max(value − threshold, 0) dt）。接口
`GET /api/v1/devices/{device_id}/metrics/exposure` 是**纯只读**的：
仅查询该设备 `committed` 表中已提交包的原始 payload，不写入任何数据，
也不改写原始包、缓冲、重复、作废、告警与校准。

| 参数 | 必填 | 说明 |
|---|---|---|
| `metric` | 是 | 指标名，只取该指标的测量 |
| `unit` | 是 | 要求的读数单位（非空字符串，读数单位须逐字相同；读数缺单位视为不符） |
| `from` / `to` | 是 | UTC 积分范围，半开区间 `[from, to)`，ISO 8601（不带时区按 UTC） |
| `threshold` | 是 | 超阈判定阈值：对 `max(value-threshold, 0)` 积分（可为负数） |
| `max_gap_seconds` | 是 | 相邻读数最大允许间隔（秒），`>= 0`（边界合格：间隔**严格大于**该值才断） |

读数选择与排序：

- 仅分析**已提交**包（缓冲包不参与）；读数单位须与 `unit` 逐字相同，
  缺单位视为不符（计入 `excluded_unit_mismatch_count`，**与时间范围无关**，
  范围外的单位不符读数也计数）；
- 每条测量的有效时间**优先取自身 `ts`，否则取所属包的 `sent_at`**；
  两者都没有的测量直接丢弃（计入 `excluded_no_time_count`）；
- 有效时间归一化为 UTC，按 **(时间, 包序号 seq, 包内位置 position)** 升序排序；
  **同一有效时刻只保留排序后的最后一条**（同时间决胜：seq 大者、再包内位置大者）；
- 读数**不**按 `[from, to)` 预过滤：范围外的相邻读数仍参与边界插值。

插值、断裂与积分：

- 相邻读数之间按时间**线性插值**；相邻间隔**严格大于** `max_gap_seconds`
  时，**该整段（两个读数之间的连线）完全不参与**积分——断裂不影响两侧
  读数各自与其他相邻读数构成的段；
- 每条有效线段先与半开区间 `[from, to)` 求交裁剪（范围外相邻读数由此
  决定 `from` / `to` 边界处的插值取值），再按阈值交点切分成若干子段；
- 对严格高于阈值的子段积分：`exposure_seconds` 累加子段时长，
  `exposure_area` 按梯形法累加 ∫ max(v(t) − threshold, 0) dt；
  恰在阈值上的部分贡献为零；
- 时间全程使用整数微秒运算。`sample_count` 为去重后纳入的读数条数，
  `segment_count` 为实际与 `[from, to)` 相交且未因间隔超限丢弃的线段数。

样本不足（不足两条有效读数，无法构成任何插值线段）时 HTTP 仍为 `200`，
`exposure_seconds` 与 `exposure_area` 均为 `0.0`。未知设备 → `404`；
时间范围倒置（`from >= to`）、`max_gap_seconds < 0`、缺参数等 → `422`。

```bash
# 读数 0、10（恰为阈值）、20、0，间隔 10s；阈值 10
curl 'localhost:8000/api/v1/devices/oven-7/metrics/exposure?metric=temperature&unit=C&from=2026-09-24T08:00:00Z&to=2026-09-24T08:00:30Z&threshold=10&max_gap_seconds=60'
# => 200
# {"device_id":"oven-7","metric":"temperature","unit":"C",
#  "from":"2026-09-24T08:00:00Z","to":"2026-09-24T08:00:30Z",
#  "threshold":10.0,"max_gap_seconds":60.0,
#  "sample_count":4,"segment_count":3,
#  "excluded_no_time_count":0,"excluded_unit_mismatch_count":0,
#  "exposure_seconds":15.0,"exposure_area":75.0}
# 超阈区间 (08:00:10, 08:00:25)：上升三角 50 + 下降三角 25
```

## 设备时钟漂移诊断（Clock Drift）

仪器自带时钟与上报时间之间可能存在系统性快慢（漂移）。接口
`GET /api/v1/devices/{device_id}/metrics/clock-drift` 利用已提交包中
「包发送时间 `sent_at`」与「测量时间戳 `ts`」的偏移序列估计漂移速率，
是**纯只读**的：仅查询该设备 `committed` 表中已提交包的原始 payload，
不写入任何数据，也不改写原始包、缓冲、重复、作废、告警与校准。

| 参数 | 必填 | 说明 |
|---|---|---|
| `metric` | 是 | 指标名，只取该指标的测量 |
| `from` / `to` | 是 | UTC **包发送时间**范围，半开区间 `[from, to)`，ISO 8601（不带时区按 UTC） |
| `max_abs_drift_per_hour` | 是 | 请求允许的每小时绝对漂移上限（秒/小时），`>= 0`；估计绝对漂移**严格大于**该值时结论为超限（相等不算超限） |

样本提取：

- 仅扫描**已提交**包（缓冲包不参与）；包缺少 `sent_at` 时**整包跳过**
  （计入 `excluded_no_sent_at_count`，且不占分析上限）；
- `sent_at` 归一化为 UTC，仅保留落在 `[from, to)` 内的包；范围内包数
  **硬上限 200，超过直接 `422` 拒绝（明确报错，绝不截断）**，请缩小
  UTC 时间范围后重试；
- 每个范围内的包只取该指标**首条带 `ts` 的测量**（按包内原始顺序）；
  包内没有时整包跳过（计入 `excluded_no_ts_count`）；
- 偏移 `offset = ts - sent_at`（秒，UTC 统一后求差，正值表示设备时钟
  偏快）；**同一发送时刻只保留序号最大的一包**。

漂移估计（对异常点稳健）：

- 对不同发送时刻的全部样本**两两配对**求偏移斜率
  `Δoffset / Δsent_at`，取**斜率中位数**作为每小时漂移
  `drift_per_hour`（秒/小时）；
- 再以该斜率把每个样本的偏移折算到末次发送时刻，取**残差中位数**作为
  末次偏移 `last_offset_seconds`；
- 不同发送时刻的有效样本**不足三包**时证据不足：HTTP 仍为 `200`，
  `conclusion=insufficient_evidence`，漂移与末次偏移均为 `null`。

响应回显参数与范围内包数 `package_count`、两类跳过计数、去重后样本数
`sample_count`、逐样本明细 `samples`（包序号 / 包内位置 / sent_at / ts /
偏移秒数）、末次发送时间 `last_sent_at`，以及结论 `conclusion`
（`within_limit` / `drift_exceeded` / `insufficient_evidence`）与中文
说明 `conclusion_detail`。未知设备 → `404`；时间范围倒置、上限为负、
缺参数等 → `422`。

```bash
# 08:00/08:30/08:45 三个包的偏移分别为 0 / 1 / 1.5 秒（每小时快 2 秒）
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/clock-drift?metric=temperature&from=2026-09-24T08:00:00Z&to=2026-09-24T09:00:00Z&max_abs_drift_per_hour=5'
# => 200
# {"device_id":"ph-meter-01","metric":"temperature",
#  "from":"2026-09-24T08:00:00Z","to":"2026-09-24T09:00:00Z",
#  "max_abs_drift_per_hour":5.0,"package_count":3,
#  "excluded_no_sent_at_count":0,"excluded_no_ts_count":0,
#  "sample_count":3,"sufficient_evidence":true,
#  "samples":[{"seq":1,"position":0,"sent_at":"2026-09-24T08:00:00Z",
#              "ts":"2026-09-24T08:00:00Z","offset_seconds":0.0}, ...],
#  "last_sent_at":"2026-09-24T08:45:00Z",
#  "drift_per_hour":2.0,"last_offset_seconds":1.5,
#  "drift_exceeded":false,"conclusion":"within_limit",
#  "conclusion_detail":"估计每小时漂移 2 秒，绝对值未超过请求上限 5 秒/小时"}
```

## 项目结构

```
app/
  main.py         # FastAPI 路由
  logic.py        # 摄入核心：幂等判定、缓冲、排水提交（摄入与作废共用的排水路径，提交时驱动告警评估）
  gaps.py         # 缺口查询：连续缺口区间计算与分页（不展开缺失序号全集，深翻页算术定位）
  voids.py        # 缺包作废：缺口段校验、登记推进、共用排水、幂等与登记查询
  alerts.py       # 阈值告警：规则 CRUD、带连续计数的迟滞状态机、按提交顺序评估
  calibrations.py # 设备指标校准：有效区间不重叠校验、按时刻解析与线性校准计算（含区间索引供聚合批量解析）
  rollup.py       # 窗口聚合：半开窗口分桶、水位迟到判定、可选校准值口径
  exports.py      # 只读归档导出：同事务分批流式复制不可变快照（无界范围不预载全量行，失败整体回滚）、NDJSON 游标编解码、manifest 摘要校验（范围含作废段时 complete=false）
  comparisons.py  # 双设备同指标并行测量比对：候选时间排序、保序二分匹配（配对数>时间差总和>字典序）
  ratio_acceptance.py # 双指标配比验收：配置表、包内一对一配组（禁跨包）、无效原因与闭区间合格判定
  warmup.py       # 仪器预热稳定放行：候选读数过滤排序、单调队列寻找最早完成的连续稳定段
  exposure.py     # 超阈暴露量积分：候选去重排序、线性插值、间隔断裂丢弃、阈值交点切分与梯形积分
  clock_drift.py  # 设备时钟漂移诊断：逐包首条 ts 样本提取、同刻去重、成对斜率中位数与残差中位数估计
  db.py           # SQLite 连接与建表（启动时自动执行）
  schemas.py      # Pydantic 模型
tests/
  conftest.py           # 测试数据库隔离（临时文件）
  test_api.py           # 摄入 / 查询接口测试
  test_gaps.py          # 缺口查询分页测试（区间计算 / 深翻页 / 参数校验 / 404）
  test_voids.py         # 缺包作废：约束 / 排水 / 共用排水告警 / 幂等 / 冲突 / 导出完整性测试
  test_rollup.py        # 窗口聚合测试
  test_alerts.py        # 阈值告警测试
  test_calibrations.py  # 校准配置 / 解析 / 原始数据不变测试
  test_exports.py       # 归档导出 / 游标 / 摘要校验测试
  test_comparisons.py   # 双设备并行测量比对测试（排序 / 保序匹配 / 决胜 / 200 上限 / 只读）
  test_ratio_acceptance.py # 双指标配比验收测试（配置校验 / 包内配组 / 无效原因 / 闭区间 / 缓冲作废排除 / 只读）
  test_warmup.py        # 仪器预热稳定放行测试（ts 回退 / 单位与无时间排除 / 波动间隔时长读数约束 / 最早完成与最早起点 / 只读）
  test_exposure.py      # 超阈暴露量积分测试（插值 / 裁剪 / 阈值交点 / 间隔断裂 / 同刻去重 / 范围外邻点 / 只读）
  test_clock_drift.py   # 设备时钟漂移诊断测试（首条 ts / 同刻去重 / 斜率与残差中位数 / 证据不足 / 200 上限 / 只读）
Dockerfile / docker-entrypoint.sh / docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | 容器内 `/data/lab.db`，本地 `./lab.db` | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（entrypoint 使用） |
