from datetime import datetime, timezone
from decimal import Decimal

import pytest
import bot


class InventoryClient:
    def __init__(self, side, regular, historical):
        self.side = side
        self.regular = Decimal(regular)
        self.historical = Decimal(historical)
        self.resting = []
        self.submitted = []
        self.cancelled = []
        self.executions = []

    def market(self, ticker):
        return {"yes_ask_dollars": "0.46", "yes_bid_dollars": "0.45",
                "no_ask_dollars": "0.46", "no_bid_dollars": "0.45"}

    def positions(self, ticker):
        held = self.regular + self.historical
        if self.side == "NO":
            held = -held
        return [{"ticker": ticker, "position_fp": str(held)}]

    def orders(self, ticker, status):
        return list(self.resting)

    def fills(self, ticker):
        return [
            {"order_id": order_id, "outcome_side": self.side,
             "count_fp": str(quantity), "yes_price_dollars": "0.25",
             "no_price_dollars": "0.25"}
            for order_id, quantity in (
                ("regular-entry", self.regular), ("historical-entry", self.historical)
            )
        ]

    def place_take_profit(self, ticker, held, target, expiration_time):
        order_id = f"exit-{len(self.submitted)}"
        self.submitted.append((held, target))
        if len(self.submitted) == 1 and self.regular > 0:
            self.regular -= abs(held)
        else:
            self.historical -= abs(held)
        return {"order_id": order_id}

    def cancel(self, order_id, ticker):
        self.cancelled.append(order_id)
        self.resting = [item for item in self.resting if item["order_id"] != order_id]
        return {"order_id": order_id, "reduced_by": "1.00"}


def setup(monkeypatch, side, regular, historical):
    fake = InventoryClient(side, regular, historical)
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "EXIT_PRICE", Decimal("0.45"))
    record = {"historical_strike_orders": [{"order_id": "historical-entry", "side": side}]}
    market = fake.market("MARKET")
    closed = datetime(2030, 1, 1, tzinfo=timezone.utc)
    return fake, record, market, closed


def manage_both(record, market, closed):
    held = bot.position("MARKET")
    reserved, excluded, average = bot.historical_inventory(record, "MARKET", held)
    bot.manage_exit(record, "MARKET", market, {}, closed, reserved, excluded)
    bot.manage_historical_take_profit(record, "MARKET", held, reserved, closed, average)


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("regular,historical", [("2", "3"), ("1.25", "3.08"), ("5", "0"), ("0", "5")])
def test_exit_quantities_partition_inventory(monkeypatch, side, regular, historical):
    fake, record, market, closed = setup(monkeypatch, side, regular, historical)
    manage_both(record, market, closed)
    sign = Decimal("1") if side == "YES" else Decimal("-1")
    expected = []
    if Decimal(regular):
        expected.append((sign * Decimal(regular), Decimal("0.45")))
    if Decimal(historical):
        expected.append((sign * Decimal(historical), Decimal("0.45")))
    assert fake.submitted == expected
    assert sum(abs(quantity) for quantity, _ in fake.submitted) == Decimal(regular) + Decimal(historical)
    # After IOC fills, the next poll sees no holdings and cannot sell twice.
    manage_both(record, market, closed)
    assert fake.submitted == expected
    assert fake.cancelled == []


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_replaces_legacy_oversized_order_after_restart(monkeypatch, side):
    fake, record, market, closed = setup(monkeypatch, side, "2", "3")
    # v0.8.0 recorded two contracts while actually submitting all five.
    record.update(take_profit_order_id="legacy-exit", take_profit_side=side,
                  take_profit_quantity="2", take_profit_target="0.29")
    fake.resting = [{"order_id": "legacy-exit"}]
    reserved, excluded, average = bot.historical_inventory(record, "MARKET", bot.position("MARKET"))
    bot.manage_exit(record, "MARKET", market, {}, closed, reserved, excluded)
    assert fake.submitted == []  # reconcile cancellation before new orders
    manage_both(record, market, closed)
    assert fake.cancelled == ["legacy-exit"]
    sign = Decimal("1") if side == "YES" else Decimal("-1")
    assert fake.submitted == [(sign * 2, Decimal("0.45")), (sign * 3, Decimal("0.45"))]

