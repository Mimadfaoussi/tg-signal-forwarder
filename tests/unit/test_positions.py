from decimal import Decimal

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis
from publisher.positions import (
    ExecutionEvent,
    PositionTracker,
    normalize_pair,
    parse_execution_event,
)

# Real execution-bot log messages the operator provided, verbatim.

NEW_SIGNAL_DETECTED = """
🔔 New Signal Detected

Pair:  ADAUSDT
Entry: 0.2411 / 0.23699 (limit buys, 60/40)
TPs:   0.24378 / 0.24658 / 0.24948 / 0.25237 / 0.25527  (+2 capped)
SL:    0.2356 (1H close)
Buy:   $100 USDT (fixed)
Split: 50% / 20% / 10% / 10% / 10%
Executing automatically…
"""

ENTRY_LIMIT_BUYS_PLACED = """
⏳ Entry limit buys placed for *ADAUSDT*
  Now: `0.2410` (60%)
  Low : `0.2369` (40%)
  SL  : `0.2356` (1H close)
Waiting for fills…
"""

ENTRY_FILLED = "✅ Entry 1 filled at `0.02092` for *CGPTUSDT*.\nOCOs placed. Watching for Entry 2…"

TP1_HIT_PARTIAL = """
✅ TP1 hit for ADAUSDT!
  Filled price : 0.24370000000000003
  Quantity     : 124.3
  Avg buy      : 0.24100000000000002
  P&L          : +0.34 USDT (+0.56%)
  🔒 SL raised : 0.2411
  Remaining : 4 TP(s) still open
"""

STOP_MOVED_NOTIFICATION = (
    "🔁 *ADAUSDT*: TP1 hit — stop moved to `0.2411` (instant SL, was candle-close). "
    "4 OCO(s) protect the rest."
)

TP2_HIT_PARTIAL = """
✅ TP2 hit for ADAUSDT!
  Filled price : 0.24649999999999997
  Quantity     : 49.7
  Avg buy      : 0.24100000000000002
  P&L          : +0.61 USDT (+1.02%)
  🔒 SL kept   : 0.2411
  Remaining : 3 TP(s) still open
"""

SL_HIT = """
🔴 Stop Loss hit for ADAUSDT!
  Stop         : 0.2411 (exchange stop)
  Filled price : 0.2411
  Quantity     : 24.8
  Avg buy      : 0.24100000000000002
  P&L          : +0.15 USDT (+0.25%)
"""

ALL_TPS_FILLED = """
✅ TP3 hit for KITEUSDT!
  Filled price : 0.1212
  Quantity     : 147.9
  Avg buy      : 0.1013
  P&L          : +7.41 USDT (+12.35%)
  All TPs filled — trade complete!
"""

MANUAL_CANCEL = (
    "🚫 PHAUSDT trade #137 cancelled — sold 2038.0 PHA at market @ 0.0504.\n"
    "  P&L: +2.85 USDT (+2.86%)"
)

TRADE_FAILED_SYMBOL = "❌ ZETAUSDT trade failed: Symbol 'ZETAUSDT' not found on Binance."

TRADE_FAILED_BALANCE = (
    "❌ MANTAUSDT trade failed: Insufficient balance. Need 100.00 USDT, have 22.20 USDT."
)


def test_normalize_pair_matches_slash_and_no_slash_forms() -> None:
    assert normalize_pair("ADA/USDT") == normalize_pair("ADAUSDT") == "ADAUSDT"


class TestParseExecutionEvent:
    def test_new_signal_detected_is_not_recognized(self) -> None:
        assert parse_execution_event(NEW_SIGNAL_DETECTED) is None

    def test_entry_limit_buys_placed_is_not_recognized(self) -> None:
        assert parse_execution_event(ENTRY_LIMIT_BUYS_PLACED) is None

    def test_entry_filled_is_not_recognized(self) -> None:
        assert parse_execution_event(ENTRY_FILLED) is None

    def test_stop_moved_notification_is_not_recognized(self) -> None:
        assert parse_execution_event(STOP_MOVED_NOTIFICATION) is None

    def test_none_and_empty_text(self) -> None:
        assert parse_execution_event(None) is None
        assert parse_execution_event("") is None

    def test_unrelated_text_is_not_recognized(self) -> None:
        assert parse_execution_event("Good morning!") is None

    def test_partial_tp_hit_is_not_closing_but_has_pnl(self) -> None:
        event = parse_execution_event(TP1_HIT_PARTIAL)
        assert event == ExecutionEvent("ADAUSDT", False, Decimal("0.34"), "tp_hit")

        event2 = parse_execution_event(TP2_HIT_PARTIAL)
        assert event2 == ExecutionEvent("ADAUSDT", False, Decimal("0.61"), "tp_hit")

    def test_stop_loss_hit_closes_with_pnl(self) -> None:
        event = parse_execution_event(SL_HIT)
        assert event == ExecutionEvent("ADAUSDT", True, Decimal("0.15"), "stop_loss")

    def test_all_tps_filled_closes_with_pnl(self) -> None:
        event = parse_execution_event(ALL_TPS_FILLED)
        assert event == ExecutionEvent("KITEUSDT", True, Decimal("7.41"), "all_tps_filled")

    def test_manual_cancel_closes_with_pnl(self) -> None:
        event = parse_execution_event(MANUAL_CANCEL)
        assert event == ExecutionEvent("PHAUSDT", True, Decimal("2.85"), "cancelled")

    def test_trade_failed_closes_with_no_pnl(self) -> None:
        event = parse_execution_event(TRADE_FAILED_SYMBOL)
        assert event == ExecutionEvent("ZETAUSDT", True, None, "failed")

        event2 = parse_execution_event(TRADE_FAILED_BALANCE)
        assert event2 == ExecutionEvent("MANTAUSDT", True, None, "failed")


@pytest_asyncio.fixture
async def redis():
    r = fakeaioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_open_and_capacity(redis) -> None:
    clock = FakeClock()
    tracker = PositionTracker(redis, max_concurrent=2, clock=clock)

    assert await tracker.is_at_capacity() is False

    await tracker.open("ADA/USDT", "-1001:1")
    assert await tracker.is_at_capacity() is False

    await tracker.open("PHA/USDT", "-1001:2")
    assert await tracker.is_at_capacity() is True


@pytest.mark.asyncio
async def test_disabled_when_zero(redis) -> None:
    tracker = PositionTracker(redis, max_concurrent=0, clock=FakeClock())

    for i in range(10):
        await tracker.open(f"PAIR{i}/USDT", f"-1001:{i}")

    assert await tracker.is_at_capacity() is False


@pytest.mark.asyncio
async def test_safety_net_expires_stale_slots(redis) -> None:
    clock = FakeClock(start=1_000_000.0)
    tracker = PositionTracker(redis, max_concurrent=1, clock=clock)

    await tracker.open("ADA/USDT", "-1001:1")
    assert await tracker.is_at_capacity() is True

    clock.now += 8 * 24 * 3600  # 8 days later: past the 7-day safety net
    assert await tracker.is_at_capacity() is False


@pytest.mark.asyncio
async def test_partial_fill_accumulates_but_does_not_close(redis) -> None:
    tracker = PositionTracker(redis, max_concurrent=5, clock=FakeClock())
    await tracker.open("ADA/USDT", "-1001:1")

    result = await tracker.apply_event(parse_execution_event(TP1_HIT_PARTIAL))

    assert result is None
    assert await tracker.is_at_capacity() is False  # still open, cap not hit at 1/5 anyway


@pytest.mark.asyncio
async def test_closing_event_sums_all_prior_partial_pnl(redis) -> None:
    tracker = PositionTracker(redis, max_concurrent=5, clock=FakeClock())
    await tracker.open("ADA/USDT", "-1001:1")

    assert await tracker.apply_event(parse_execution_event(TP1_HIT_PARTIAL)) is None
    assert await tracker.apply_event(parse_execution_event(TP2_HIT_PARTIAL)) is None

    result = await tracker.apply_event(parse_execution_event(SL_HIT))

    assert result is not None
    assert result.signal_id == "-1001:1"
    assert result.reason == "stop_loss"
    # 0.34 (TP1) + 0.61 (TP2) + 0.15 (SL) = 1.10
    assert result.pnl_usdt == Decimal("1.10")


@pytest.mark.asyncio
async def test_all_tps_filled_with_no_prior_partials(redis) -> None:
    tracker = PositionTracker(redis, max_concurrent=5, clock=FakeClock())
    await tracker.open("KITE/USDT", "-1002:5")

    result = await tracker.apply_event(parse_execution_event(ALL_TPS_FILLED))

    assert result is not None
    assert result.signal_id == "-1002:5"
    assert result.pnl_usdt == Decimal("7.41")
    assert result.reason == "all_tps_filled"


@pytest.mark.asyncio
async def test_trade_failed_closes_with_no_pnl_but_releases_slot(redis) -> None:
    tracker = PositionTracker(redis, max_concurrent=1, clock=FakeClock())
    await tracker.open("ZETA/USDT", "-1003:9")
    assert await tracker.is_at_capacity() is True

    result = await tracker.apply_event(parse_execution_event(TRADE_FAILED_SYMBOL))

    assert result is not None
    assert result.signal_id == "-1003:9"
    assert result.pnl_usdt is None
    assert result.reason == "failed"
    assert await tracker.is_at_capacity() is False


@pytest.mark.asyncio
async def test_closing_event_for_untracked_pair_is_a_safe_no_op(redis) -> None:
    tracker = PositionTracker(redis, max_concurrent=5, clock=FakeClock())

    result = await tracker.apply_event(parse_execution_event(SL_HIT))

    assert result is None


@pytest.mark.asyncio
async def test_slot_is_released_after_close(redis) -> None:
    tracker = PositionTracker(redis, max_concurrent=1, clock=FakeClock())
    await tracker.open("ADA/USDT", "-1001:1")
    assert await tracker.is_at_capacity() is True

    await tracker.apply_event(parse_execution_event(SL_HIT))

    assert await tracker.is_at_capacity() is False
