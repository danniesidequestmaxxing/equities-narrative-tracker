"""Golden fixtures for the path-dependent outcome scorer (M4).

Integer-friendly prices so expected R is exact and reviewable by hand.
"""

from decimal import Decimal as D

from narrative_tracker.scorer import score_call
from narrative_tracker.scorer.types import (
    Adjustment,
    AdjKind,
    Bar,
    Call,
    CloseReason,
    Direction,
    TerminalEvent,
)

DAY = 86400


def day(n: int) -> int:
    return n * DAY


def bar(n, o, h, l, c) -> Bar:
    return Bar(day(n), D(str(o)), D(str(h)), D(str(l)), D(str(c)))


FLAT_BENCH = [Bar(day(n), D("400"), D("400"), D("400"), D("400")) for n in range(12)]


def call(direction, entry, stop, targets, *, t0=day(1), horizon_days=10) -> Call:
    return Call(
        "t", "ACME", direction, t0, D(str(entry)), D(str(stop)),
        tuple(D(str(x)) for x in targets), horizon_days * DAY,
    )


def test_f1_gap_through_stop():
    bars = [bar(1, 100, 101, 99, 100), bar(2, 92, 93, 90, 91), bar(3, 91, 96, 90, 95)]
    out = score_call(call(Direction.LONG, 100, 95, [110]), bars, FLAT_BENCH)
    assert out.reason is CloseReason.STOP
    assert out.realized_r == D("-1.6")  # fill at the gapped OPEN, not the stop
    assert out.entry_fill == D("92")


def test_f2_same_bar_stop_first():
    bars = [bar(1, 100, 101, 99, 100), bar(2, 100, 106, 94, 100)]
    out = score_call(call(Direction.LONG, 100, 95, [105]), bars, FLAT_BENCH)
    assert out.reason is CloseReason.STOP and out.realized_r == D("-1.0")


def test_f2b_subbars_resolve_target_first():
    bars = [bar(1, 100, 101, 99, 100), bar(2, 100, 106, 94, 100)]

    def loader(symbol, ts):
        return [
            Bar(ts, D("105"), D("106"), D("104"), D("105")),
            Bar(ts, D("95"), D("96"), D("94"), D("95")),
        ]

    out = score_call(call(Direction.LONG, 100, 95, [105]), bars, FLAT_BENCH, subbar_loader=loader)
    assert out.reason is CloseReason.TARGET and out.realized_r == D("1.0")


def test_f3_split_invariance():
    bars = [
        bar(1, 100, 103, 99, 101), bar(2, 101, 104, 100, 102),
        bar(3, 51, 53, 50, 52), bar(4, 52, 58, 51, 57), bar(5, 58, 66, 57, 64),
    ]
    ledger = [Adjustment(day(3), AdjKind.SPLIT, D("2"))]
    out = score_call(call(Direction.LONG, 100, 90, [130]), bars, FLAT_BENCH, ledger=ledger)
    assert out.reason is CloseReason.TARGET and out.realized_r == D("3.0")


def test_f4_dividend_credit():
    bars = [
        bar(1, 100, 101, 99, 100), bar(2, 100, 103, 99, 101), bar(3, 101, 104, 100, 102),
        bar(4, 102, 106, 101, 105), bar(5, 105, 108, 104, 107), bar(6, 107, 112, 106, 110),
    ]
    ledger = [Adjustment(day(3), AdjKind.DIVIDEND, D("2"))]
    out = score_call(call(Direction.LONG, 100, 95, [110]), bars, FLAT_BENCH, ledger=ledger)
    assert out.reason is CloseReason.TARGET and out.realized_r == D("2.4")


def test_f5_expiry_mark_out():
    bars = [
        bar(1, 100, 101, 99, 100), bar(2, 100, 103, 98, 101), bar(3, 100, 103, 98, 101),
        bar(4, 100, 103, 98, 101), bar(5, 100, 103, 98, 101), bar(6, 103, 105, 102, 104),
    ]
    out = score_call(call(Direction.LONG, 100, 90, [120], horizon_days=5), bars, FLAT_BENCH)
    assert out.reason is CloseReason.EXPIRY and out.realized_r == D("0.4")
    assert out.mae_r <= out.realized_r <= out.mfe_r


def test_f6_delist_to_zero():
    bars = [bar(1, 100, 102, 98, 100), bar(2, 100, 101, 96, 97), bar(3, 97, 99, 95.01, 96), bar(4, 96, 96, 96, 96)]
    out = score_call(call(Direction.LONG, 100, 95, [130]), bars, FLAT_BENCH, terminal=TerminalEvent(day(4), D("0")))
    assert out.reason is CloseReason.TERMINAL and out.realized_r == D("-20.0")


def test_f7_never_hit_flat():
    bars = [bar(1, 100, 101, 99, 100)] + [bar(n, 100, 102, 99, 101) for n in range(2, 6)] + [bar(6, 101, 102, 99, 101)]
    out = score_call(call(Direction.LONG, 100, 90, [110]), bars, FLAT_BENCH)
    assert out.reason is CloseReason.EXPIRY and out.realized_r == D("0.1")


def test_f8_short_gap_through_target():
    bars = [bar(1, 100, 101, 99, 100), bar(2, 86, 88, 84, 86)]
    out = score_call(call(Direction.SHORT, 100, 105, [90]), bars, FLAT_BENCH)
    assert out.reason is CloseReason.TARGET and out.realized_r == D("2.8")


def test_f9_no_look_ahead_on_signal_bar():
    # D1 high already >= target, but the fill is D2 -> D1 must NOT count.
    bars = [bar(1, 100, 106, 99, 100), bar(2, 100, 101, 99, 100)]
    out = score_call(call(Direction.LONG, 100, 95, [105], horizon_days=1), bars, FLAT_BENCH)
    assert out.reason is not CloseReason.TARGET


def test_f10_ma_cash_out():
    bars = [bar(1, 100, 101, 99, 100), bar(2, 100, 103, 99, 102)] + [bar(n, 102, 105, 101, 103) for n in range(3, 8)]
    out = score_call(call(Direction.LONG, 100, 90, [130]), bars, FLAT_BENCH, terminal=TerminalEvent(day(7), D("118")))
    assert out.reason is CloseReason.TERMINAL and out.realized_r == D("1.8")


def test_property_monotone_up_never_stops():
    bars = [bar(1, 100, 100, 100, 100)] + [bar(n, 100 + n, 101 + n, 99 + n, 100 + n) for n in range(2, 9)]
    out = score_call(call(Direction.LONG, 100, 95, [130]), bars, FLAT_BENCH)
    assert out.reason is not CloseReason.STOP
