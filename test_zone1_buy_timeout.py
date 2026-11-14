"""Zone 1 120-second unfilled buy cancellation, with no live API calls."""
from decimal import Decimal as D

import pytest
import single_coin_minute9 as core
from test_single_coin_minute9 import START, CLOSE, StopExchange, fake_record


class PendingBuyExchange(StopExchange):
    def __init__(self, *, bought="0", quantity="2"):
        super().__init__(side="YES", bid=".75")
        self.orders["entry"].update(status="pending",
                                    fill_count_fp=str(bought),
                                    remaining_count_fp=str(D(quantity) - D(bought)))
        self.rows[0]["count_fp"] = str(bought)
        if D(bought) == 0:
            self.rows.clear()
        self.held = D(bought)
        self.cancels = []
        self.cancel_fail = False

    def cancel(self, order_id, ticker):
        self.cancels.append((order_id, ticker))
        if self.cancel_fail:
            raise TimeoutError("unknown cancel acknowledgement")
        self.orders[order_id].update(status="canceled", remaining_count_fp="0")
        return {"order_id": order_id}


@pytest.fixture
def state(tmp_path):
    j = core.Journal(tmp_path / "zone1_timeout.json")
    c = PendingBuyExchange()
    record = fake_record(c, fully_sold=False)
    record["window_start_ms"] = START * 1000
    record["entry_zone"] = core.ZONE1
    record["buy"]["created_at"] = START + 540
    j.data["windows"][c.ticker] = record
    j.save()
    return c, j, record


def replay(client, journal, timestamp):
    # Read existing owned orders only; never submit fresh entries.
    core.run_pass(client, journal, (), D("6.66"), 100, True, timestamp)


def test_untouched_pending_zone1_buy_not_canceled_before_exact_two_minutes(state):
    c, j, record = state
    replay(c, j, START + 659.999)
    assert c.cancels == [] and "zone1_cancel" not in record
    assert record.get("outcome") is None


def test_pending_zero_fill_cancels_at_two_minutes_and_proves_no_fill(state):
    c, j, record = state
    replay(c, j, START + 660)
    assert c.cancels == [("entry", c.ticker)]
    assert record["zone1_cancel"]["reason"] == "unfilled_remainder_after_120s"
    assert record["outcome"]["status"] == "no_fill"
    assert core.entry_slot_released(record)
    assert core.window_entry_count(j, START * 1000) == 0
    assert core.Journal(j.path).data == j.data
    replay(c, j, START + 662)
    assert len(c.cancels) == 1  # Terminal order cannot cancel again.


def test_pending_partial_fill_cancels_only_unfilled_remainder_then_places_tp(state):
    c, j, record = state
    c.orders["entry"]["fill_count_fp"] = "1"
    c.orders["entry"]["remaining_count_fp"] = "1"
    c.rows.append({"order_id": "entry", "ticker": c.ticker, "count_fp": "1",
                   "yes_price_dollars": ".72", "no_price_dollars": ".28",
                   "fee_cost": ".01", "ts": START+555})
    c.held = D(1)
    replay(c, j, START + 660)
    assert c.cancels == [("entry", c.ticker)]
    assert record.get("outcome") is None
    assert core.window_entry_count(j, START * 1000) == 1
    # No duplicate entry; the bought contract is still owned and managed.
    replay(c, j, START + 662)
    assert record["sells"][0]["purpose"] == "take_profit"
    assert record["sells"][0]["execution"] == "gtc"
    assert c.submissions[-1]["limit"] == D(".77")
    assert len(c.cancels) == 1


def test_cancel_timeout_is_durable_and_retry_is_rate_limited_after_restart(state):
    c, j, record = state
    c.cancel_fail = True
    replay(c, j, START + 660)
    assert len(c.cancels) == 1
    assert record["zone1_cancel"]["attempted_at"] == START + 660
    assert "outcome" not in record
    c.cancel_fail = False
    reopened = core.Journal(j.path)
    replay(c, reopened, START + 669)
    assert len(c.cancels) == 1
    replay(c, reopened, START + 675)
    assert len(c.cancels) == 2
    assert reopened.data["windows"][c.ticker]["outcome"]["status"] == "no_fill"


def test_zone2_buy_is_never_canceled_by_zone1_timeout(state):
    c, j, record = state
    record["entry_zone"] = core.ZONE2
    record["entry_kind"] = "late_basket_rank"
    assert not core.cancel_stale_zone1_buy(c, j, record, START + 750)
    assert c.cancels == []


def test_unconfirmed_order_identity_never_cancels_someone_else(state):
    c, j, record = state
    c.orders["entry"]["client_order_id"] = "outside-client"
    replay(c, j, START + 670)
    assert c.cancels == [] and not record.get("zone1_cancel")


def test_unresolved_ack_does_not_authorize_cancellation(state):
    c, j, record = state
    record["buy"].pop("order_id")
    c.orders["entry"]["client_order_id"] = "other-client"
    replay(c, j, START + 670)
    assert c.cancels == []
    assert "outcome" not in record


def test_fully_filled_but_stale_status_is_not_cancelled(state):
    c, j, record = state
    c.orders["entry"]["fill_count_fp"] = "2"
    c.orders["entry"]["remaining_count_fp"] = "0"
    c.rows.append({"order_id": "entry", "ticker": c.ticker, "count_fp": "2",
                   "yes_price_dollars": ".72", "no_price_dollars": ".28",
                   "fee_cost": ".01", "ts": START+555})
    c.held = D(2)
    replay(c, j, START + 670)
    assert c.cancels == [] and record.get("outcome") is None


def test_closed_window_stale_order_still_canceled(state):
    c, j, record = state
    replay(c, j, CLOSE + 5)
    assert c.cancels == [("entry", c.ticker)]
    assert record["outcome"]["status"] == "no_fill"


def test_invalid_timestamp_fails_closed(state):
    c, j, record = state
    record["buy"]["created_at"] = float("nan")
    replay(c, j, START + 670)
    assert c.cancels == []


def test_core_entry_is_still_immediate_or_cancel_for_regular_buy():
    # The watchdog doesn't convert a price-taking IOC into a risky resting buy.
    from pathlib import Path
    source = Path(core.__file__).read_text()
    assert 'client_order_id=intent["client_id"], ioc=True)' in source
