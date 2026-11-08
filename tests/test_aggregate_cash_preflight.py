"""Regression checks for market-shard-capped preflight with venue authority."""
from decimal import Decimal as D
from unittest.mock import Mock

import pytest

import cash_checked_minute9 as guard
from kalshi import KalshiAPIError


class Journal:
    def __init__(self):
        self.data = {"windows": {
            "TEST-1": {
                "ticker": "TEST-1",
                "window_start_ms": 0,
                "side": "YES",
                "quantity": "10",
                "buy": {"client_id": "cid", "limit": "0.70"},
                "sells": [],
            }
        }}
    def save(self):
        pass


@pytest.mark.parametrize('zone', [guard.core.ZONE1, guard.core.ZONE2])
def test_market_cash_resizes_dispatch_within_budget(monkeypatch, zone):
    monkeypatch.setattr(guard.core.time, "time", lambda: 1000)
    journal = Journal()
    record = journal.data['windows'].pop('TEST-1')
    record['entry_zone'] = zone
    record_key = guard.core.zone_record_key('TEST-1', zone)
    journal.data['windows'][record_key] = record
    client = Mock(spec=['market', 'balance', 'place_entry'])
    client.market.return_value = {"ticker": "TEST-1", "exchange_index": 2}
    client.balance.return_value = {
        "balance_dollars": "25.88",
        "balance_breakdown": [
            {"exchange_index": 0, "balance": "24.8011"},
            {"exchange_index": 2, "balance": "1.0789"},
        ],
    }
    client.place_entry.return_value = {"order": {"order_id": "oid"}}
    result = guard.CashCheckedClient(client, journal, guard.CashState()).place_entry(
        "TEST-1", "YES", 10, D(".70"), 1100, client_order_id="cid", ioc=True
    )
    assert result["order"]["order_id"] == "oid"
    assert client.place_entry.call_count == 1
    assert client.place_entry.call_args.args[2] == 1
    assert journal.data["windows"][record_key]["quantity"] == "1"
    assert journal.data["windows"][record_key]["cash_preflight"]["cash_check_scope"] == "market_exchange_cash"


def test_insufficient_total_cash_stops_before_post(monkeypatch):
    monkeypatch.setattr(guard.core.time, "time", lambda: 1000)
    journal = Journal()
    client = Mock(spec=['market', 'balance', 'place_entry'])
    client.market.return_value = {"ticker": "TEST-1", "exchange_index": 2}
    client.balance.return_value = {"balance_dollars": "0.50", "balance_breakdown": [{"exchange_index": 2, "balance": "0.50"}]}
    with pytest.raises(guard.EntryNotSubmitted, match="insufficient_total_cash"):
        guard.CashCheckedClient(client, journal, guard.CashState()).place_entry(
            "TEST-1", "YES", 10, D(".70"), 1100, client_order_id="cid", ioc=True
        )
    client.place_entry.assert_not_called()
    assert guard.release_proven_no_order(journal.data["windows"]["TEST-1"])


def test_venue_insufficient_balance_is_terminal_and_does_not_retry(monkeypatch):
    monkeypatch.setattr(guard.core.time, "time", lambda: 1000)
    journal = Journal()
    client = Mock(spec=['market', 'balance', 'place_entry'])
    client.market.return_value = {"ticker": "TEST-1", "exchange_index": 2}
    client.balance.return_value = {"balance_dollars": "25.88", "balance_breakdown": [{"exchange_index": 2, "balance": "1.0789"}]}
    client.place_entry.side_effect = KalshiAPIError(400, "insufficient", code="insufficient_balance")
    with pytest.raises(guard.EntryNotSubmitted, match="exchange_rejected_insufficient_balance"):
        guard.CashCheckedClient(client, journal, guard.CashState()).place_entry(
            "TEST-1", "YES", 10, D(".70"), 1100, client_order_id="cid", ioc=True
        )
    assert client.place_entry.call_count == 1
    assert guard.release_proven_no_order(journal.data["windows"]["TEST-1"])


def test_high_total_cash_and_underfunded_market_prevent_unfillable_order(monkeypatch):
    monkeypatch.setattr(guard.core.time, "time", lambda: 1000)
    journal = Journal()
    client = Mock(spec=['market', 'balance', 'place_entry'])
    client.market.return_value = {"ticker": "TEST-1", "exchange_index": 2}
    client.balance.return_value = {"balance_dollars": "44.6587",
                                   "balance_breakdown": [
                                       {"exchange_index": 0, "balance": "44.0679"},
                                       {"exchange_index": 2, "balance": "0.5908"},
                                   ]}
    with pytest.raises(guard.EntryNotSubmitted, match="insufficient_market_cash"):
        guard.CashCheckedClient(client, journal, guard.CashState()).place_entry(
            "TEST-1", "YES", 10, D(".70"), 1100, client_order_id="cid", ioc=True
        )
    client.place_entry.assert_not_called()
    assert guard.release_proven_no_order(journal.data["windows"]["TEST-1"])
