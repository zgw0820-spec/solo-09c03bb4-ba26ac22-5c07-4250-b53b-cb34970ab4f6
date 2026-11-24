"""RFC 4862 第 5.5.3 节未认证 PIO 规则的纯函数模拟引擎。

输入只依赖“旧前缀”的有效 SLAAC Prefix Information Option（A 标志置位、
非链路本地前缀、有限有效寿命）。通告可能乱序补报，引擎先按
``(接收时刻, 序号)`` 排序再逐条应用规则。

规则（RFC 4862 §5.5.3，未认证通告，只建模 valid lifetime）：

* §5.5.3(d)：当前没有该前缀的 SLAAC 地址且收到的有效寿命非 0 →
  按通告寿命形成新地址，到期时刻 = 接收时刻 + 有效寿命。
* §5.5.3(e).1：收到的有效寿命 > 2 小时，或 > RemainingLifetime →
  到期时刻重设为 接收时刻 + 收到的有效寿命。
* §5.5.3(e).2：RemainingLifetime ≤ 2 小时 → 忽略本条（不改变到期时刻）。
* §5.5.3(e).3：其余情况（剩余 > 2 小时且收到的寿命较短）→
  到期时刻截断为 接收时刻 + 2 小时。
* 有效寿命为 0：(d) 不会形成新地址；在 (e) 中按 0 秒走上述分支，
  即通常落入 (e).3（剩余 > 2h 时截断到 2h）或 (e).2（剩余 ≤ 2h 时忽略）。
"""
from dataclasses import dataclass, field

TWO_HOURS = 7200  # RFC 4862 §5.5.3(e) 的 2 小时阈值（秒）
MAX_FINITE_VALID_LIFETIME = 0xFFFFFFFF - 1  # 0xFFFFFFFF 是“无限寿命”，题目只允许有限寿命


@dataclass(frozen=True)
class PioEvent:
    """一条旧前缀 PIO 通告（已通过形式校验）。"""

    seq: int
    received_at: int          # UTC 纪元秒
    valid_lifetime: int       # 有限、非负
    router: str
    row_id: int | None = None  # 对应 pio_records.id，供网页层回溯


@dataclass
class StepTrace:
    """单条通告应用后的依据链，用于页面“依据”展示。"""

    row_id: int | None
    seq: int
    router: str
    received_at: int
    valid_lifetime: int
    rule: str                 # d / e1 / e2 / e3
    changed: bool
    expiry_before: int | None
    expiry_after: int | None
    remaining_before: int | None
    detail: str


@dataclass
class SimResult:
    expiry: int | None = None
    last_change: PioEvent | None = None
    steps: list[StepTrace] = field(default_factory=list)
    created_by: PioEvent | None = None


def validate_valid_lifetime(value) -> int:
    """校验并返回有限的有效寿命秒数（0 .. 2^32-2）。"""
    try:
        v = int(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"有效寿命必须是整数秒，收到 {value!r}")
    if v < 0:
        raise ValueError("有效寿命不能为负数")
    if v > MAX_FINITE_VALID_LIFETIME:
        raise ValueError(
            f"有效寿命 {v} 超过有限寿命上限 {MAX_FINITE_VALID_LIFETIME}；"
            "0xFFFFFFFF(4294967295) 表示无限寿命，本核查只接受有限寿命"
        )
    return v


def simulate(events: list[PioEvent], snapshot_expiry: int | None) -> SimResult:
    """按 RFC 4862 §5.5.3 重放一个终端的全部通告。

    ``snapshot_expiry`` 为快照时该终端单个旧地址的到期时刻；
    快照无旧地址时传 None（到期时刻由 §5.5.3(d) 首次形成）。
    引擎不做时间窗口过滤，窗口校验在存储层完成。
    """
    result = SimResult(expiry=snapshot_expiry)
    ordered = sorted(events, key=lambda e: (e.received_at, e.seq))

    for ev in ordered:
        before = result.expiry
        remaining = None if before is None else before - ev.received_at

        if before is None:
            # §5.5.3(d)：无此前缀地址，寿命非 0 才形成
            if ev.valid_lifetime == 0:
                rule, changed, after = "d", False, None
                detail = "当前无该前缀地址，有效寿命为 0，§5.5.3(d) 不形成地址"
            else:
                after = ev.received_at + ev.valid_lifetime
                rule, changed = "d", True
                result.created_by = ev
                result.last_change = ev
                detail = f"§5.5.3(d) 形成旧地址，到期 = 接收时刻 + {ev.valid_lifetime}s"
        else:
            # §5.5.3(e)：地址已存在。注意 RFC 用剩余寿命参与判断，
            # 已过期地址的剩余值为负，仍按文本分支处理。
            if ev.valid_lifetime > TWO_HOURS or ev.valid_lifetime > remaining:
                after = ev.received_at + ev.valid_lifetime
                rule, changed = "e1", after != before
                if changed:
                    result.last_change = ev
                detail = (
                    f"§5.5.3(e).1：收到寿命 {ev.valid_lifetime}s > 7200s "
                    f"或 > 剩余 {remaining}s，重设到期 = 接收时刻 + {ev.valid_lifetime}s"
                )
            elif remaining <= TWO_HOURS:
                after, rule, changed = before, "e2", False
                detail = (
                    f"§5.5.3(e).2：剩余 {remaining}s ≤ 7200s 且通告未认证，"
                    "忽略有效寿命更新"
                )
            else:
                after = ev.received_at + TWO_HOURS
                rule, changed = "e3", after != before
                if changed:
                    result.last_change = ev
                detail = (
                    f"§5.5.3(e).3：剩余 {remaining}s > 7200s 且收到寿命较短，"
                    "到期截断为 接收时刻 + 7200s"
                )

        result.expiry = after
        result.steps.append(
            StepTrace(
                row_id=ev.row_id,
                seq=ev.seq,
                router=ev.router,
                received_at=ev.received_at,
                valid_lifetime=ev.valid_lifetime,
                rule=rule,
                changed=changed,
                expiry_before=before,
                expiry_after=after,
                remaining_before=remaining,
                detail=detail,
            )
        )

    return result
