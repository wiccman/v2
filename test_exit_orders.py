from decimal import Decimal
from datetime import datetime, timezone

import bot
from kalshi import KalshiClient


def test_entry_limits_are_32_and_39_cents_for_both_confidences():
    for confidence in ("HIGH", "MODERATE"):
        assert bot.entry_price_allowed(confidence, Decimal("0.32"))
        assert bot.entry_price_allowed(confidence, Decimal("0.39"))
        for price in ("0.24", "0.26", "0.30", "0.47", "0.70"):
            assert not bot.entry_price_allowed(confidence, Decimal(price))
    assert not bot.entry_price_allowed("NONE", Decimal("0.32"))


class DualLimitClient:
    def __init__(self):
        self.entries = []
        self.resting = []
        self.cancelled = []

    def place_entry(self, ticker, side, quantity, price, expiration_time, **kwargs):
        order_id = f"dual-{side.lower()}-{price}"
        self.entries.append((ticker, side, quantity, price, expiration_time))
        self.resting.append({"order_id": order_id})
        return {"order_id": order_id}

    def orders(self, ticker, status):
        return list(self.resting)

    def cancel(self, order_id, ticker):
        self.cancelled.append(order_id)
        return {"order_id": order_id, "reduced_by": "1.00"}


def test_dual_limit_buys_post_both_levels_until_six_minute_deadline(monkeypatch):
    fake = DualLimitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "save_state", lambda state: None)
    record = {"dual_limit_orders": []}
    closed = datetime.fromtimestamp(2000, tz=timezone.utc)

    assert bot.place_dual_limit_buys(record, "MARKET", closed, now_timestamp=1000, state={"markets": {"MARKET": record}}) is True
    assert [entry[1] for entry in fake.entries] == ["YES", "YES", "NO", "NO"]
    assert [entry[2] for entry in fake.entries] == [Decimal("0.60"), Decimal("0.49")] * 2
    assert [entry[3] for entry in fake.entries] == [Decimal("0.32"), Decimal("0.39")] * 2
    assert sum(entry[2] * entry[3] for entry in fake.entries) <= bot.BUDGET
    assert all(entry[4] == 1460 for entry in fake.entries)
    assert record["dual_limit_orders"] == ["dual-yes-0.32", "dual-yes-0.39", "dual-no-0.32", "dual-no-0.39"]


def test_dual_limit_buys_cancel_unfilled_orders_after_five_minutes(monkeypatch):
    fake = DualLimitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "save_state", lambda state: None)
    record = {
        "dual_limit_orders": ["dual-yes", "dual-no"],
        "dual_limit_cancel_at": 1300,
    }
    fake.resting = [{"order_id": "dual-yes"}, {"order_id": "dual-no"}]

    assert bot.cancel_expired_dual_limits(record, "MARKET", now_timestamp=1299) is False
    assert fake.cancelled == []
    assert bot.cancel_expired_dual_limits(record, "MARKET", now_timestamp=1300) is True
    assert fake.cancelled == ["dual-yes", "dual-no"]
    assert record["dual_limit_orders"] == []


def test_historical_strike_reaction_uses_approach_side():
    assert bot.strike_reaction_side(Decimal("99975"), Decimal("100000")) == "NO"
    assert bot.strike_reaction_side(Decimal("100025"), Decimal("100000")) == "YES"
    assert bot.strike_reaction_side(Decimal("100000"), Decimal("100000")) is None


def test_historical_strike_touch_no_longer_posts_entries(monkeypatch):
    fake = DualLimitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "save_state", lambda state: None)
    record = {
        "historical_strikes": ["100000", "101000", "102000"],
        "historical_last_spot": "99950",
        "historical_triggered_strikes": [],
        "historical_strike_orders": [],
    }
    closed = datetime.fromtimestamp(2000, tz=timezone.utc)

    assert bot.place_historical_strike_entries(
        record, "MARKET", Decimal("99980"), closed, now_timestamp=1000, state={"markets": {"MARKET": record}},
    ) is False
    assert fake.entries == []
    assert record["historical_triggered_strikes"] == []
    assert record["historical_strike_orders"] == []


class HistoricalExitClient:
    def market(self, ticker):
        return {"yes_ask_dollars": "0.46", "yes_bid_dollars": "0.45"}

    def __init__(self):
        self.actions = []

    def orders(self, ticker, status):
        return []

    def place_take_profit(self, ticker, held, target, expiration_time):
        self.actions.append((ticker, held, target, expiration_time))
        return {"order_id": "historical-tp"}

    def cancel(self, order_id, ticker):
        self.actions.append(("cancel", order_id))
        return {"order_id": order_id, "reduced_by": "1.00"}

    def fills(self, ticker):
        return [
            {"order_id": "historical-entry", "count_fp": "3.08"},
            {"order_id": "historical-tp-old", "count_fp": "1.00"},
            {"order_id": "regular-entry", "count_fp": "2.00"},
        ]


def test_historical_inventory_reserves_only_unexited_strategy_quantity(monkeypatch):
    fake = HistoricalExitClient()
    monkeypatch.setattr(bot, "client", fake)
    record = {
        "historical_strike_orders": [{"order_id": "historical-entry", "side": "YES"}],
        "historical_take_profit_orders": [{"order_id": "historical-tp-old", "side": "YES"}],
    }

    quantity, excluded, average_entry = bot.historical_inventory(record, "MARKET", Decimal("4.08"))
    assert quantity == Decimal("2.08")
    assert excluded == {"historical-entry", "historical-tp-old"}
    assert average_entry is None


def test_retired_single_tier_helper_uses_first_pair_target(monkeypatch):
    fake = HistoricalExitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "save_state", lambda state: None)
    record = {"historical_take_profit_orders": []}
    closed = datetime.fromtimestamp(2000, tz=timezone.utc)

    assert bot.manage_historical_take_profit(
        record, "MARKET", Decimal("3.08"), Decimal("3.08"), closed, Decimal("0.24"),
    ) is True
    assert fake.actions == [("MARKET", Decimal("3.08"), Decimal("0.39"), 2000.0)]
    assert record["historical_take_profit_target"] == "0.39"
    assert record["historical_take_profit_order_id"] == "historical-tp"


class RecordingClient(KalshiClient):
    def __init__(self):
        self.calls = []

    def request(self, method, path, params=None, body=None, auth=False):
        self.calls.append((method, path, body, auth))
        return {"order_id": "tp-order"}


def test_yes_take_profit_is_ioc_reduce_only_ask():
    client = RecordingClient()
    result = client.place_take_profit("MARKET", Decimal("2.5"), Decimal("0.40"), 12345)
    body = client.calls[0][2]
    assert result["order_id"] == "tp-order"
    assert body["side"] == "ask"
    assert body["count"] == "2.5"
    assert body["price"] == "0.4000"
    assert body["reduce_only"] is True
    assert body["time_in_force"] == "immediate_or_cancel"
    assert "expiration_time" not in body
    assert body["post_only"] is False


def test_no_take_profit_converts_outcome_price_to_yes_bid():
    client = RecordingClient()
    client.place_take_profit("MARKET", Decimal("-3"), Decimal("0.35"), 12345)
    body = client.calls[0][2]
    assert body["side"] == "bid"
    assert body["count"] == "3"
    assert body["price"] == "0.6500"
    assert body["reduce_only"] is True
    assert body["time_in_force"] == "immediate_or_cancel"


class ExitClient:
    def __init__(self, bid="0.20"):
        self.held = Decimal("2")
        self.resting = []
        self.actions = []

    def positions(self, ticker):
        return [{"ticker": ticker, "position_fp": str(self.held)}]

    def orders(self, ticker, status):
        return list(self.resting)

    def fills(self, ticker):
        return [{
            "created_time": "2026-01-01T00:00:00Z",
            "outcome_side": "yes",
            "count_fp": "2",
            "yes_price_dollars": "0.20",
        }]

    def place_take_profit(self, ticker, held, target, expiration_time):
        self.actions.append(("take_profit", ticker, held, target, expiration_time))
        return {"order_id": "tp-1"}

    def cancel(self, order_id, ticker):
        self.actions.append(("cancel", order_id))
        return {"order_id": order_id, "reduced_by": "1.00"}

    def close_position(self, ticker, held, yes_bid, yes_ask):
        self.actions.append(("close", ticker, held, yes_bid, yes_ask))
        return {"order_id": "stop-1"}


def test_manage_exit_submits_ioc_when_target_reachable(monkeypatch):
    fake = ExitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "save_state", lambda state: None)
    record = {}
    market = {"yes_ask_dollars": "0.46", "yes_bid_dollars": "0.45", "no_ask_dollars": "0.80", "no_bid_dollars": "0.78"}
    closed = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)

    assert bot.manage_exit(record, "MARKET", market, {}, closed) is True
    assert record["take_profit_order_id"] == "tp-1"
    assert record["take_profit_quantity"] == "2"
    assert record["take_profit_target"] == "0.39"
    assert fake.actions[0][:4] == ("take_profit", "MARKET", Decimal("2"), Decimal("0.39"))


def test_manage_exit_does_not_stop_out_at_low_bid(monkeypatch):
    fake = ExitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "save_state", lambda state: None)
    record = {}
    market = {"yes_ask_dollars": "0.04", "yes_bid_dollars": "0.03", "no_ask_dollars": "0.97", "no_bid_dollars": "0.96"}
    closed = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)

    assert bot.manage_exit(record, "MARKET", market, {}, closed) is False
    assert fake.actions == []
    assert all(action[0] != "close" for action in fake.actions)

