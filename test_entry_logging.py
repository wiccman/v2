"""Entry route logs distinguish exchange acknowledgements from blocked attempts."""
import json
from decimal import Decimal as D

import pytest

import bot
from test_five_minute_exits import cycle_setup


@pytest.mark.parametrize("route,elapsed,prefix,accepted", [
    ("opening", 30, "OPENING_BIAS", "OPENING_BIAS_LIMIT"),
    ("late", 720, "LATE_BIAS", "LATE_BIAS_LIMIT"),
    ("historical", 120, "HISTORICAL_STRIKE", "HISTORICAL_STRIKE_ACKNOWLEDGED"),
])
@pytest.mark.parametrize("outcome", ["funding_wait", "side_block", "empty_response", "acknowledged"])
def test_route_logs_require_order_acknowledgement(monkeypatch, route, elapsed, prefix, accepted, outcome):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    events = []
    monkeypatch.setattr(bot, "write_log", lambda event, *args, **fields: events.append((event, fields)))
    monkeypatch.setattr(bot, "ENABLED", True)
    if outcome == "funding_wait":
        monkeypatch.setattr(fake, "market_cash", lambda ticker: {"exchange_index": 2, "cash_dollars": ".7843"})
    elif outcome == "side_block":
        record["trade_side"] = "NO"
    elif outcome == "empty_response":
        # A local deadline can expire during the cash lookup. An intent alone
        # does not prove that the exchange accepted an order.
        monkeypatch.setattr(fake, "place_entry", lambda *args, **kwargs: {})

    if route == "historical":
        bot.place_historical_strike_entries(record, "TEST", D("100010"), closed, state=state)
    else:
        bot.cycle(state)

    route_events = [(event, fields) for event, fields in events
                    if event in {accepted, prefix + "_NO_ORDER", prefix + "_RESTING"}]
    assert route_events
    for event, fields in route_events:
        order = json.loads(fields["details"])["order"]
        if outcome == "acknowledged" and order.get("order_id"):
            assert event == accepted
            assert D(fields["quantity"]) == 5
        else:
            assert event == prefix + "_NO_ORDER"
            assert not order.get("order_id")
    if outcome == "acknowledged":
        assert any(event == accepted for event, fields in route_events)
    else:
        assert fake.entries == []
        assert record["orders"] == []
        if outcome in {"funding_wait", "side_block"}:
            assert record["entry_intents"] == []
