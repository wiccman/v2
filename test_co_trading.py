from decimal import Decimal
import pytest
from co_trading import manual_buy_fills, gross_profit_limit, AmbiguousManualFill


def fill(fid, oid, action="buy", side="yes"):
    return dict(fill_id=fid, order_id=oid, ticker="KXBTC15M-TEST",
                action=action, side=side, count_fp="5",
                yes_price_dollars="0.60", no_price_dollars="0.40")


def test_bot_entries_are_not_classified_as_manual():
    rows = [fill("a", "bot"), fill("b", "manual"), fill("c", "exit", "sell")]
    assert [x["order_id"] for x in manual_buy_fills(rows, {"bot"}, {"exit"})] == ["manual"]


def test_duplicate_fill_does_not_duplicate_inventory():
    rows = [fill("a", "manual"), fill("a", "manual")]
    assert len(manual_buy_fills(rows, set())) == 1


def test_missing_identity_fails_closed():
    with pytest.raises(AmbiguousManualFill):
        manual_buy_fills([dict(fill_id="a", ticker="T", action="buy")], set())


def test_target_is_total_profit_not_per_contract():
    assert gross_profit_limit(".60", 10, ".20") == Decimal(".62")
    assert gross_profit_limit(".60", 3, ".20") == Decimal(".67")


def test_unreachable_target_rejected():
    with pytest.raises(ValueError):
        gross_profit_limit(".98", 2, ".20")
