"""Regression: settled Zone 1 fills remain in the same-ticker Kalshi history.

A subsequently filled Zone 2 position must receive its resting 98c target
and still honor the 21c stop, without adopting any outside inventory.
Purely offline fake exchange; no credentials, network or live orders.
"""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

import single_coin_minute9 as core
from test_single_coin_minute9 import START, StopExchange, fake_record


def setup_same_ticker(tmp_path, *, side="YES"):
    client = StopExchange(side=side, bid=".90")
    entry_price = D(".91")
    yes_cost = entry_price if side == "YES" else D(1) - entry_price
    client.rows[0]["yes_price_dollars"] = str(yes_cost)
    client.rows[0]["no_price_dollars"] = str(D(1) - yes_cost)
    client.rows[0]["created_time"] = datetime.fromtimestamp(
        START + 730, timezone.utc).isoformat()

    # Zone 1 bought and fully sold before Zone 2's independent entry.
    old_buy = dict(client.rows[0], order_id="old-zone1-buy", count_fp="2")
    old_buy["created_time"] = datetime.fromtimestamp(
        START + 580, timezone.utc).isoformat()
    old_sell = dict(client.rows[0], order_id="old-zone1-tp", count_fp="2")
    old_sell["created_time"] = datetime.fromtimestamp(
        START + 650, timezone.utc).isoformat()
    client.rows.extend([old_buy, old_sell])
    for order_id, client_id in [("old-zone1-buy", "old-buy-cid"),
                                ("old-zone1-tp", "old-tp-cid")]:
        client.orders[order_id] = {
            "ticker": client.ticker, "order_id": order_id,
            "client_order_id": client_id, "status": "executed",
            "fill_count_fp": "2",
        }
    previous = {
        "ticker": client.ticker, "entry_zone": core.ZONE1,
        "entry_kind": "regular",
        "buy": {"client_id": "old-buy-cid", "order_id": "old-zone1-buy"},
        "sells": [{"client_id": "old-tp-cid", "order_id": "old-zone1-tp"}],
        "outcome": {"status": "win", "bought": "2", "sold": "2",
                    "remaining_at_settlement": "0",
                    "completed_at": START + 650, "counted": True},
    }
    zone2 = fake_record(client, fully_sold=False)
    zone2.update(entry_kind="late_basket_rank", entry_zone=core.ZONE2,
                 take_profit_target=".98")
    journal = core.Journal(tmp_path / "zone2-after-prior-zone1.json")
    journal.data["windows"][core.zone_record_key(client.ticker, core.ZONE1)] = previous
    journal.data["windows"][core.zone_record_key(client.ticker, core.ZONE2)] = zone2
    journal.save()
    return client, journal, previous, zone2


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_prior_closed_zone1_fills_do_not_block_zone2_98c_tp_and_stop(tmp_path, side):
    client, journal, _, zone2 = setup_same_ticker(tmp_path, side=side)
    assert core.verified_previous_zone1_fill_ids(
        journal, zone2, client.rows) == {"old-zone1-buy", "old-zone1-tp"}

    core.reconcile_exit(client, journal, zone2, START + 733)
    assert len(client.submissions) == 1
    assert client.submissions[0]["limit"] == D(".98")
    assert client.submissions[0]["signed"] == (D(2) if side == "YES" else D(-2))
    assert zone2["sells"][0]["purpose"] == "take_profit"
    assert zone2["sells"][0]["execution"] == "gtc"

    # Restart does not duplicate the TP, and a 21c adverse bid cancels it.
    again = core.Journal(journal.path)
    record = again.data["windows"][core.zone_record_key(client.ticker, core.ZONE2)]
    core.reconcile_exit(client, again, record, START + 735)
    assert len(client.submissions) == 1
    client.bid = D(".70")
    core.reconcile_exit(client, again, record, START + 737)
    assert client.orders["exit-1"]["status"] == "canceled"
    assert D(record["stop_armed"]["threshold"]) == D(".70")
    core.reconcile_exit(client, again, record, START + 739)
    assert len(client.submissions) == 2
    assert record["sells"][-1]["purpose"] == "stop_loss"
    assert client.submissions[-1]["limit"] == D(".70")


def test_prior_zone1_fills_do_not_block_zone2_verified_pnl(tmp_path):
    client, journal, _, zone2 = setup_same_ticker(tmp_path)
    core.reconcile_exit(client, journal, zone2, START + 733)
    client.orders["exit-1"]["status"] = "executed"
    client.orders["exit-1"]["fill_count_fp"] = "2"
    client.rows.append({
        "ticker": client.ticker, "order_id": "exit-1", "count_fp": "2",
        "yes_price_dollars": ".98", "no_price_dollars": ".02",
        "fee_cost": "0.01",
        "created_time": datetime.fromtimestamp(START + 740, timezone.utc).isoformat(),
    })
    core.verify_completed_trade(client, journal, zone2, START + 745)
    assert zone2["outcome"]["status"] == "win"
    assert zone2["outcome"]["bought"] == "2"
    assert zone2["outcome"]["sold"] == "2"
    assert zone2["outcome"]["remaining_at_settlement"] == "0"


@pytest.mark.parametrize("unsafe", ["foreign_fill", "incomplete_prior_exit",
                                     "wrong_prior_quantity"])
def test_foreign_or_unverified_history_still_blocks_zone2_exit(tmp_path, unsafe):
    client, journal, previous, zone2 = setup_same_ticker(tmp_path)
    if unsafe == "foreign_fill":
        client.rows.append(dict(client.rows[0], order_id="manual", count_fp="1"))
    elif unsafe == "incomplete_prior_exit":
        previous["outcome"]["sold"] = "1"
    elif unsafe == "wrong_prior_quantity":
        client.rows[-2]["count_fp"] = "1"
    journal.save()
    assert not core.verified_previous_zone1_fill_ids(
        journal, zone2, client.rows) or unsafe == "foreign_fill"
    core.reconcile_exit(client, journal, zone2, START + 733)
    assert not client.submissions
    assert not zone2["sells"]
