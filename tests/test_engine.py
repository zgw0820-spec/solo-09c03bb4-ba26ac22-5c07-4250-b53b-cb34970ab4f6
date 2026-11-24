"""RFC 4862 §5.5.3 未认证通告规则的引擎单元测试。"""
import pytest

from app.engine import PioEvent, TWO_HOURS, simulate, validate_valid_lifetime

T0 = 1_000_000  # 基准纪元秒，避免与 None 混淆


def ev(seq, t, vl, router="R1"):
    return PioEvent(seq=seq, received_at=t, valid_lifetime=vl, router=router)


def rules(result):
    return [(s.rule, s.changed) for s in result.steps]


# ---------------------------------------------------------------- 5.5.3(d)

def test_d_creates_address_when_none_and_nonzero():
    r = simulate([ev(0, T0, 100)], None)
    assert r.expiry == T0 + 100
    assert r.created_by is not None
    assert rules(r) == [("d", True)]
    assert r.last_change.seq == 0


def test_d_zero_lifetime_does_not_create():
    r = simulate([ev(0, T0, 0)], None)
    assert r.expiry is None
    assert r.last_change is None
    assert rules(r) == [("d", False)]


def test_d_then_e_after_creation():
    r = simulate([ev(0, T0, 100), ev(1, T0 + 50, 3600)], None)
    # 创建：到期 T0+100；第二条时剩余 50s，寿命 3600 > 剩余 → e1 重设
    assert r.expiry == T0 + 50 + 3600
    assert [s.rule for s in r.steps] == ["d", "e1"]
    assert r.last_change.seq == 1


# ---------------------------------------------------------------- 5.5.3(e).1

def test_e1_lifetime_over_two_hours_resets_even_if_shorter_than_remaining():
    # 剩余 3h，收到 7201s（>2h 但 <剩余）→ 仍重设（DoS 保护只对 ≤2h 的寿命生效）
    r = simulate([ev(0, T0, 7201)], T0 + 3 * TWO_HOURS)
    assert r.expiry == T0 + 7201
    assert rules(r) == [("e1", True)]


def test_e1_lifetime_greater_than_remaining_resets():
    # 剩余 1h，收到 2h（>剩余，不 >2h）→ e1 重设
    r = simulate([ev(0, T0, TWO_HOURS)], T0 + TWO_HOURS // 2)
    assert r.expiry == T0 + TWO_HOURS
    assert rules(r) == [("e1", True)]


def test_e1_equal_value_but_remaining_le_two_hours_falls_to_e2():
    # 剩余 3600s，收到 3600s：3600 > 3600 不成立、也不 >7200 → 落 e2 忽略
    r = simulate([ev(0, T0, 3600)], T0 + 3600)
    assert rules(r) == [("e2", False)]
    assert r.expiry == T0 + 3600
    assert r.last_change is None


# ---------------------------------------------------------------- 5.5.3(e).2

def test_e2_ignores_short_lifetime_when_remaining_le_two_hours():
    r = simulate([ev(0, T0, 600)], T0 + TWO_HOURS)  # 剩余恰好 7200
    assert r.expiry == T0 + TWO_HOURS
    assert rules(r) == [("e2", False)]
    assert r.last_change is None


def test_e2_protects_against_zero_lifetime_near_expiry():
    r = simulate([ev(0, T0, 0)], T0 + 100)  # 剩余 100s
    assert r.expiry == T0 + 100
    assert rules(r) == [("e2", False)]


# ---------------------------------------------------------------- 5.5.3(e).3

def test_e3_truncates_to_two_hours():
    # 剩余 3h，收到 3600s（≤2h 且 <剩余）→ 截断为 接收+2h
    r = simulate([ev(0, T0, 3600)], T0 + 3 * TWO_HOURS)
    assert r.expiry == T0 + TWO_HOURS
    assert rules(r) == [("e3", True)]
    assert r.last_change.seq == 0


def test_e3_zero_lifetime_truncates_when_remaining_over_two_hours():
    r = simulate([ev(0, T0, 0)], T0 + 3 * TWO_HOURS)
    assert r.expiry == T0 + TWO_HOURS
    assert rules(r) == [("e3", True)]


# ---------------------------------------------------------------- 乱序重放

def test_out_of_order_events_applied_by_receive_time_then_seq():
    events = [
        ev(2, T0 + 200, 86400, "R2"),
        ev(0, T0, 100, "R1"),
        ev(1, T0 + 100, 500, "R1"),
    ]
    r = simulate(events, None)
    assert [s.seq for s in r.steps] == [0, 1, 2]
    # seq0 形成 T0+100；seq1 时剩余 0，500>0 → e1 → T0+100+500=T0+600；
    # seq2 时剩余 400s，86400>7200 → e1
    assert r.expiry == T0 + 200 + 86400


def test_same_receive_time_orders_by_seq():
    events = [ev(1, T0, 100), ev(0, T0, 200)]
    r = simulate(events, None)
    assert [s.seq for s in r.steps] == [0, 1]


def test_snapshot_expiry_unchanged_without_events():
    r = simulate([], T0 + 999)
    assert r.expiry == T0 + 999
    assert r.last_change is None
    assert r.steps == []


def test_expired_address_then_long_lifetime_renews():
    # 地址在 T0-10 已过期；T0 收到长寿命 → e1（寿命>2h）重新给到期
    r = simulate([ev(0, T0, 10000)], T0 - 10)
    assert r.expiry == T0 + 10000
    assert rules(r) == [("e1", True)]


def test_last_change_tracks_only_changing_advertisement():
    events = [
        ev(0, T0, 10000),        # 剩余 3h，寿命 >2h → e1 重设为 T0+10000，last=0
        ev(1, T0 + 100, 600),    # 剩余 9900s >2h，短寿命 → e3 截断到 T0+100+7200，last=1
        ev(2, T0 + 200, 10),     # 剩余 7100s ≤2h → e2 忽略，last 不变
    ]
    r = simulate(events, T0 + 3 * TWO_HOURS)
    assert [s.rule for s in r.steps] == ["e1", "e3", "e2"]
    assert r.last_change.seq == 1
    assert r.expiry == T0 + 100 + TWO_HOURS


# ---------------------------------------------------------------- 校验

@pytest.mark.parametrize("bad", ["abc", "", None, "-1", "4294967295", "4294967296"])
def test_validate_valid_lifetime_rejects(bad):
    with pytest.raises(ValueError):
        validate_valid_lifetime(bad)


@pytest.mark.parametrize("good", ["0", "7200", 0, 4294967294])
def test_validate_valid_lifetime_accepts(good):
    assert validate_valid_lifetime(good) == int(good)
