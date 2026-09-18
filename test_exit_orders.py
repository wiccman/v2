from decimal import Decimal
from datetime import datetime, timezone

import bot
from kalshi import KalshiClient


def test_entry_price_caps_depend_on_base_confidence():
    assert bot.entry_price_allowed("HIGH", Decimal("0.47")) is True
    assert bot.entry_price_allowed("HIGH", Decimal("0.48")) is False
    assert bot.entry_price_allowed("MODERATE", Decimal("0.30")) is True
    assert bot.entry_price_allowed("MODERATE", Decimal("0.31")) is False


class RecordingClient(KalshiClient):
    def __init__(self):
        self.calls = []

    def request(self, method, path, params=None, body=None, auth=False):
        self.calls.append((method, path, body, auth))
        return {"order_id": "tp-order"}


def test_yes_take_profit_is_resting_reduce_only_ask():
    client = RecordingClient()
    result = client.place_take_profit("MARKET", Decimal("2.5"), Decimal("0.40"), 12345)
    body = client.calls[0][2]
    assert result["order_id"] == "tp-order"
    assert body["side"] == "ask"
    assert body["count"] == "2.5"
    assert body["price"] == "0.4000"
    assert body["reduce_only"] is True
    assert body["time_in_force"] == "good_till_canceled"
    assert body["expiration_time"] == 12345


def test_no_take_profit_converts_outcome_price_to_yes_bid():
    client = RecordingClient()
    client.place_take_profit("MARKET", Decimal("-3"), Decimal("0.35"), 12345)
    body = client.calls[0][2]
    assert body["side"] == "bid"
    assert body["count"] == "3"
    assert body["price"] == "0.6500"
    assert body["reduce_only"] is True
    assert body["time_in_force"] == "good_till_canceled"


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

    def cancel(self, order_id):
        self.actions.append(("cancel", order_id))

    def close_position(self, ticker, held, yes_bid, yes_ask):
        self.actions.append(("close", ticker, held, yes_bid, yes_ask))
        return {"order_id": "stop-1"}


def test_manage_exit_places_and_tracks_resting_take_profit(monkeypatch):
    fake = ExitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    record = {}
    market = {"yes_ask_dollars": "0.22", "yes_bid_dollars": "0.20", "no_ask_dollars": "0.80", "no_bid_dollars": "0.78"}
    closed = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)

    assert bot.manage_exit(record, "MARKET", market, {}, closed) is True
    assert record["take_profit_order_id"] == "tp-1"
    assert record["take_profit_quantity"] == "2"
    assert record["take_profit_target"] == "0.23"
    assert fake.actions[0][:4] == ("take_profit", "MARKET", Decimal("2"), Decimal("0.23"))


def test_manage_exit_does_not_stop_out_at_low_bid(monkeypatch):
    fake = ExitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *args, **kwargs: None)
    record = {}
    market = {"yes_ask_dollars": "0.04", "yes_bid_dollars": "0.03", "no_ask_dollars": "0.97", "no_bid_dollars": "0.96"}
    closed = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)

    assert bot.manage_exit(record, "MARKET", market, {}, closed) is True
    assert fake.actions[0][0] == "take_profit"
    assert all(action[0] != "close" for action in fake.actions)
