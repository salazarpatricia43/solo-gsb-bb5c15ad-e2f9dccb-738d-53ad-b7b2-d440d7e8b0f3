"""持久化阈值告警：规则 CRUD、带连续计数的迟滞状态机、提交时评估。

状态机（每条 设备+指标 规则一个，状态持久化在 alert_rule_states 中）：

- normal（正常）：维护 breach_streak，每次越界 +1，任一不越界清零；
  breach_streak 达到 trigger_count 时转为 triggered，产生 triggered 事件。
- triggered（告警中）：维护 recovery_streak，每次不越界 +1，任一越界清零；
  recovery_streak 达到 recovery_count 时转回 normal，产生 recovered 事件。

上下限均为严格比较：value > upper_limit 或 value < lower_limit 才算越界，
等于阈值不越界。指标在一个包中缺失时（该包没有对应 measurement）该规则
跳过、计数不变，避免缺数被当作恢复。

评估只在包"按提交顺序排水"时发生：重复包走幂等短路、缓冲包尚未提交，
都不会评估；补齐乱序缺口时，缓冲包按 seq 升序只评估一次。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

NORMAL = "normal"
TRIGGERED = "triggered"
EVENT_TRIGGERED = "triggered"
EVENT_RECOVERED = "recovered"


def _normalize_iso(raw: str) -> str:
    """测量 ts / 包 sent_at 归一化为 UTC 且以 Z 结尾（无小数秒时省略）。"""
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    text = dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        timespec="microseconds"
    )
    if text.endswith(".000000"):
        text = text[:-7]
    return text + "Z"


class AlertRuleError(ValueError):
    """规则参数非法。"""


class AlertRuleConflict(Exception):
    """同一 (设备, 指标) 已存在规则。"""


@dataclass(frozen=True)
class Rule:
    id: int
    device_id: str
    metric: str
    upper_limit: Optional[float]
    lower_limit: Optional[float]
    trigger_count: int
    recovery_count: int
    enabled: bool


@dataclass
class RuleState:
    state: str = NORMAL
    breach_streak: int = 0
    recovery_streak: int = 0
    last_value: Optional[float] = None
    last_seq: Optional[int] = None
    last_evaluated_at: Optional[str] = None


def is_breaching(rule: Rule, value: float) -> bool:
    """严格比较：等于阈值不算越界。"""
    if rule.upper_limit is not None and value > rule.upper_limit:
        return True
    if rule.lower_limit is not None and value < rule.lower_limit:
        return True
    return False


def advance(rule: Rule, st: RuleState, value: float) -> Optional[str]:
    """喂入一个测量值，推进状态机；发生状态翻转时返回事件类型，否则 None。"""
    breaching = is_breaching(rule, value)
    st.last_value = value
    if st.state == NORMAL:
        if breaching:
            st.breach_streak += 1
            if st.breach_streak >= rule.trigger_count:
                st.state = TRIGGERED
                st.breach_streak = 0
                st.recovery_streak = 0
                return EVENT_TRIGGERED
        else:
            st.breach_streak = 0
        return None

    # triggered：迟滞恢复，阈值与触发阈值相互独立
    if breaching:
        st.recovery_streak = 0
    else:
        st.recovery_streak += 1
        if st.recovery_streak >= rule.recovery_count:
            st.state = NORMAL
            st.recovery_streak = 0
            st.breach_streak = 0
            return EVENT_RECOVERED
    return None


def _validate_limits(
    upper_limit: Optional[float], lower_limit: Optional[float]
) -> None:
    if upper_limit is None and lower_limit is None:
        raise AlertRuleError("upper_limit 与 lower_limit 至少需要提供一个")
    if upper_limit is not None and lower_limit is not None and lower_limit > upper_limit:
        raise AlertRuleError("lower_limit 不能大于 upper_limit")


def _rule_from_row(row: sqlite3.Row) -> Rule:
    return Rule(
        id=row["id"],
        device_id=row["device_id"],
        metric=row["metric"],
        upper_limit=row["upper_limit"],
        lower_limit=row["lower_limit"],
        trigger_count=row["trigger_count"],
        recovery_count=row["recovery_count"],
        enabled=bool(row["enabled"]),
    )


def get_rule(conn: sqlite3.Connection, rule_id: int) -> Optional[Rule]:
    row = conn.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
    return _rule_from_row(row) if row is not None else None


def create_rule(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    upper_limit: Optional[float],
    lower_limit: Optional[float],
    trigger_count: int,
    recovery_count: int,
    enabled: bool = True,
) -> Rule:
    """创建规则并初始化其状态机。同 (设备, 指标) 重复创建抛 AlertRuleConflict。"""
    _validate_limits(upper_limit, lower_limit)
    exists = conn.execute(
        "SELECT 1 FROM alert_rules WHERE device_id = ? AND metric = ?",
        (device_id, metric),
    ).fetchone()
    if exists is not None:
        raise AlertRuleConflict(f"设备 {device_id} 指标 {metric} 的规则已存在")
    cur = conn.execute(
        """
        INSERT INTO alert_rules
            (device_id, metric, upper_limit, lower_limit, trigger_count,
             recovery_count, enabled)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            device_id,
            metric,
            upper_limit,
            lower_limit,
            trigger_count,
            recovery_count,
            1 if enabled else 0,
        ),
    )
    rule_id = cur.lastrowid
    conn.execute(
        "INSERT INTO alert_rule_states (rule_id) VALUES (?)", (rule_id,)
    )
    return get_rule(conn, rule_id)


def update_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    upper_limit: Optional[float],
    lower_limit: Optional[float],
    trigger_count: int,
    recovery_count: int,
    enabled: bool,
) -> Optional[Rule]:
    """更新规则配置。为避免沿用旧阈值下的计数造成误判，连续计数清零，
    当前是否告警（state）保留。规则不存在返回 None。"""
    _validate_limits(upper_limit, lower_limit)
    cur = conn.execute(
        """
        UPDATE alert_rules
           SET upper_limit = ?, lower_limit = ?, trigger_count = ?,
               recovery_count = ?, enabled = ?,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
         WHERE id = ?
        """,
        (
            upper_limit,
            lower_limit,
            trigger_count,
            recovery_count,
            1 if enabled else 0,
            rule_id,
        ),
    )
    if cur.rowcount == 0:
        return None
    conn.execute(
        """
        UPDATE alert_rule_states
           SET breach_streak = 0, recovery_streak = 0
         WHERE rule_id = ?
        """,
        (rule_id,),
    )
    return get_rule(conn, rule_id)


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    """删除规则及其状态机（级联）；历史事件保留。不存在返回 False。"""
    cur = conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
    return cur.rowcount > 0


def _load_rules_for_device(conn: sqlite3.Connection, device_id: str) -> List[Rule]:
    return [
        _rule_from_row(r)
        for r in conn.execute(
            "SELECT * FROM alert_rules WHERE device_id = ? AND enabled = 1 ORDER BY id",
            (device_id,),
        ).fetchall()
    ]


def _load_states(
    conn: sqlite3.Connection, rule_ids: List[int]
) -> Dict[int, RuleState]:
    if not rule_ids:
        return {}
    placeholders = ",".join("?" for _ in rule_ids)
    return {
        r["rule_id"]: RuleState(
            state=r["state"],
            breach_streak=r["breach_streak"],
            recovery_streak=r["recovery_streak"],
            last_value=r["last_value"],
            last_seq=r["last_seq"],
            last_evaluated_at=r["last_evaluated_at"],
        )
        for r in conn.execute(
            f"SELECT * FROM alert_rule_states WHERE rule_id IN ({placeholders})",
            rule_ids,
        ).fetchall()
    }


def _persist_state(
    conn: sqlite3.Connection, rule_id: int, st: RuleState
) -> None:
    conn.execute(
        """
        UPDATE alert_rule_states
           SET state = ?, breach_streak = ?, recovery_streak = ?,
               last_value = ?, last_seq = ?, last_evaluated_at = ?
         WHERE rule_id = ?
        """,
        (
            st.state,
            st.breach_streak,
            st.recovery_streak,
            st.last_value,
            st.last_seq,
            st.last_evaluated_at,
            rule_id,
        ),
    )


def evaluate_payload(
    conn: sqlite3.Connection,
    rules: List[Rule],
    states: Dict[int, RuleState],
    seq: int,
    payload_json: str,
) -> None:
    """按包内 measurements 的提交顺序，对设备的每条启用规则评估一个包。

    状态翻转时写入 alert_events（triggered/recovered 共用同一个处理序号）。
    幂等性由调用方保证：仅在排水提交时调用，每个包恰好一次。
    """
    payload = json.loads(payload_json)
    raw_sent_at = payload.get("sent_at")
    commit_fallback = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    )

    by_metric: Dict[str, List[dict]] = {}
    for m in payload.get("measurements", []):
        by_metric.setdefault(m.get("metric"), []).append(m)

    for rule in rules:
        st = states.get(rule.id)
        if st is None:  # 防御：状态行缺失时按全新状态处理
            st = RuleState()
        measurements = by_metric.get(rule.metric)
        if not measurements:
            continue  # 该包没有此指标：计数保持不变
        for m in measurements:
            value = float(m["value"])
            event_type = advance(rule, st, value)
            if event_type is None:
                continue
            raw_ts = m.get("ts") or raw_sent_at
            event_time = _normalize_iso(raw_ts) if raw_ts is not None else commit_fallback
            conn.execute(
                """
                INSERT INTO alert_events
                    (rule_id, device_id, metric, seq, event_type, value, event_time)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    rule.id,
                    rule.device_id,
                    rule.metric,
                    seq,
                    event_type,
                    value,
                    event_time,
                ),
            )
        st.last_seq = seq
        st.last_evaluated_at = commit_fallback
        _persist_state(conn, rule.id, st)


def evaluate_drain(
    conn: sqlite3.Connection,
    device_id: str,
    committed: List[tuple],
) -> None:
    """对一次排水提交的若干 (seq, payload_json) 按序评估告警规则。

    committed 必须按 seq 升序（即实际提交顺序）。
    """
    if not committed:
        return
    rules = _load_rules_for_device(conn, device_id)
    if not rules:
        return
    states = _load_states(conn, [r.id for r in rules])
    for seq, payload_json in committed:
        evaluate_payload(conn, rules, states, seq, payload_json)
