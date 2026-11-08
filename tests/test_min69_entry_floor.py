from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

import single_coin_minute9 as core


def market(yes_ask, no_ask):
    return {
        "ticker": "KXBTC15M-26NOV072015-15",
        "status": "open",
        "close_time": "2026-11-08T01:15:00+00:00",
        "yes_ask_dollars": str(yes_ask),
        "no_ask_dollars": str(no_ask),
    }


def eligible_time():
    close = core.parse_time("2026-11-08T01:15:00+00:00")
    return close - 900 + 550


def test_68c_is_below_new_entry_floor():
    assert core.MIN_PRICE == D(".69")
    assert core.plan_single_coin(market(".68", ".33"), eligible_time(), "KXBTC15M") is None


def test_69c_is_eligible_boundary():
    plan = core.plan_single_coin(market(".69", ".32"), eligible_time(), "KXBTC15M")
    assert plan is not None
    assert plan["side"] == "YES"
    assert D(plan["limit"]) == D(".69")


def test_budget_guard_uses_69c_floor():
    with pytest.raises(ValueError):
        core.budgeted_quantity(D("10"), D(".68"))
    assert core.budgeted_quantity(D("10"), D(".69")) >= 1
