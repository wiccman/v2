"""Read-only historical order recovery: exact identities, no optimistic zero-fills."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from kalshi import KalshiAPIError, KalshiClient
import single_coin_minute9 as core


OLD = datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp()
NEW = datetime(2026, 11, 8, tzinfo=timezone.utc).timestamp()
CUTOFF = "2026-11-07T00:00:00Z"
TICKER = "KXETH15M-26NOV061200-00"


def mock_archive(monkeypatch, *, archived=None, cutoff=CUTOFF, live=None, historic_fills=None):
    client = KalshiClient()
    calls = []
    archived = [] if archived is None else archived
    live = [] if live is None else live
    historic_fills = [] if historic_fills is None else historic_fills

    def request(method, path, params=None, **kw):
        assert method == "GET"
        calls.append((path, dict(params or {})))
        if path == "/historical/cutoff":
            return {"orders_updated_ts": cutoff}
        if path.startswith("/portfolio/orders/"):
            raise KalshiAPIError(404, "live order not visible")
        if path == "/portfolio/orders":
            return {"orders": live}
        if path == "/historical/orders":
            assert params["ticker"] == TICKER and params["subaccount"] == 0
            return {"orders": archived}
        if path == "/portfolio/fills":
            return {"fills": []}
        if path == "/historical/fills":
            assert params["ticker"] == TICKER and params["subaccount"] == 0
            return {"fills": historic_fills}
        raise AssertionError((method, path, params))
    monkeypatch.setattr(client, "request", request)
    return client, calls


def test_exact_archived_id_recovers_terminal_partial_fill(monkeypatch):
    saved = {"order_id": "OWNED", "client_id": "CLIENT", "created_at": OLD}
    row = {"ticker": TICKER, "order_id": "OWNED", "client_order_id": "CLIENT",
           "status": "canceled", "fill_count_fp": "0.25", "remaining_count_fp": "0",
           "subaccount_number": 0}
    fills = [{"ticker": TICKER, "fill_id": "F1", "order_id": "OWNED", "count_fp": "0.25"}]
    client, calls = mock_archive(monkeypatch, archived=[row], historic_fills=fills)
    recovered = core.find_order(client, TICKER, saved)
    assert recovered["fill_count_fp"] == "0.25"
    assert client.all_fills(TICKER) == fills
    assert "historical" in " ".join(path for path, _ in calls)


def test_historical_client_id_finds_lost_ack_once(monkeypatch):
    intent = {"client_id": "CLIENT", "created_at": OLD}
    owned = {"ticker": TICKER, "order_id": "OWNED",
             "client_order_id": "CLIENT", "status": "canceled", "subaccount_number": 0}
    client, _ = mock_archive(monkeypatch, archived=[
        {"ticker": TICKER, "order_id": "DIFFERENT", "client_order_id": "OTHER"},
        owned])
    assert core.find_order(client, TICKER, intent) == owned
    assert intent["order_id"] == "OWNED"


def test_new_404_never_reads_historical_orders(monkeypatch):
    intent = {"order_id": "UNKNOWN", "client_id": "CLIENT", "created_at": NEW}
    client, calls = mock_archive(monkeypatch)
    assert core.find_order(client, TICKER, intent) is None
    assert not any(path == "/historical/orders" for path, _ in calls)


def test_archived_missing_is_still_unknown_not_zero_fill(monkeypatch):
    intent = {"order_id": "UNKNOWN", "client_id": "CLIENT", "created_at": OLD}
    client, calls = mock_archive(monkeypatch)
    assert core.find_order(client, TICKER, intent) is None
    assert intent["order_id"] == "UNKNOWN"
    assert any(path == "/historical/orders" for path, _ in calls)


def test_wrong_client_id_never_becomes_owned(monkeypatch):
    intent = {"order_id": "OWNED", "client_id": "EXPECTED", "created_at": OLD}
    bad = {"ticker": TICKER, "order_id": "OWNED", "client_order_id": "OUTSIDE",
           "status": "executed", "subaccount_number": 0}
    client, _ = mock_archive(monkeypatch, archived=[bad])
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        core.find_order(client, TICKER, intent)


def test_historical_fills_deduplicate_overlap(monkeypatch):
    client, _ = mock_archive(monkeypatch, historic_fills=[
        {"ticker": TICKER, "fill_id": "F1", "order_id": "OWNED", "count_fp": "0.25"},
        {"ticker": TICKER, "fill_id": "F2", "order_id": "OWNED", "count_fp": "0.25"}])
    def request(method, path, params=None, **kw):
        if path == "/portfolio/fills":
            return {"fills": [{"ticker": TICKER, "fill_id": "F1", "order_id": "OWNED", "count_fp": "0.25"}]}
        if path == "/historical/fills":
            return {"fills": [
                {"ticker": TICKER, "fill_id": "F1", "order_id": "OWNED", "count_fp": "0.25"},
                {"ticker": TICKER, "fill_id": "F2", "order_id": "OWNED", "count_fp": "0.25"}]}
        raise AssertionError(path)
    monkeypatch.setattr(client, "request", request)
    client._archived_tickers.add(TICKER)
    rows = client.all_fills(TICKER)
    assert [r["fill_id"] for r in rows] == ["F1", "F2"]
    assert sum(Decimal(r["count_fp"]) for r in rows) == Decimal("0.50")


def test_historical_fills_reject_conflicting_order_id(monkeypatch):
    client, _ = mock_archive(monkeypatch)
    def request(method, path, params=None, **kw):
        if path == "/portfolio/fills":
            return {"fills": [{"ticker": TICKER, "fill_id": "F1", "order_id": "A"}]}
        if path == "/historical/fills":
            return {"fills": [{"ticker": TICKER, "fill_id": "F1", "order_id": "B"}]}
        raise AssertionError(path)
    monkeypatch.setattr(client, "request", request)
    client._archived_tickers.add(TICKER)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        client.all_fills(TICKER)


def test_archive_fallback_never_swallows_401(monkeypatch):
    client = KalshiClient()
    def request(method, path, params=None, **kw):
        raise KalshiAPIError(401, "no auth")
    monkeypatch.setattr(client, "request", request)
    with pytest.raises(KalshiAPIError) as caught:
        core.find_order(client, TICKER,
                        {"order_id": "OWNED", "client_id": "CLIENT", "created_at": OLD})
    assert caught.value.status_code == 401


def test_historical_market_read_on_archive_404(monkeypatch):
    client = KalshiClient()
    paths = []
    def request(method, path, **kw):
        paths.append(path)
        if path == "/markets/" + TICKER:
            raise KalshiAPIError(404, "market moved")
        if path == "/historical/markets/" + TICKER:
            return {"market": {"ticker": TICKER, "status": "settled", "result": "yes"}}
        raise AssertionError(path)
    monkeypatch.setattr(client, "request", request)
    assert client.market(TICKER)["result"] == "yes"
    assert paths == ["/markets/" + TICKER, "/historical/markets/" + TICKER]
