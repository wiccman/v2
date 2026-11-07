"""Offline tests for the separate 15-minute coin markets (not Coin Race)."""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from single_coin_minute9 import (Journal, DEFAULT_SERIES, plan_single_coin,
    starting_streak, next_streak, trade_budget, budgeted_quantity,
    apply_new_outcomes, verify_completed_trade, exit_decision, reconcile_exit,
    window_entry_count, run_pass)

START = 1793763000  # 15-minute-aligned contract window
CLOSE = START + 900

def market(yes=".72", no=".29", series="KXBTC15M", status="active"):
    return {
        "ticker": f"{series}-26NOV031500-00",
        "status": status,
        "yes_ask_dollars": yes, "no_ask_dollars": no,
        "close_time": datetime.fromtimestamp(CLOSE, timezone.utc).isoformat(),
    }

@pytest.mark.parametrize("series", DEFAULT_SERIES)
def test_each_coin_is_independent(series):
    p = plan_single_coin(market(series=series), START + 550, series)
    assert p and p["series"] == series and p["side"] == "YES"

def test_no_coin_race_or_basket_selection():
    assert "KXCRYPTOLEAD15M" not in DEFAULT_SERIES
    assert plan_single_coin(market(series="KXBTC15M"), START + 550, "KXETH15M") is None

@pytest.mark.parametrize("price", [".59", ".60", ".72", ".90", ".91"])
def test_yes_boundaries_are_inclusive(price):
    p = plan_single_coin(market(yes=price, no=".01"), START + 550, "KXBTC15M")
    assert p and p["side"] == "YES" and D(p["ask"]) == D(price)

@pytest.mark.parametrize("price", [".01", ".58", ".5899", ".9101", ".92", ".99"])
def test_no_buy_outside_price_band(price):
    assert plan_single_coin(market(yes=price, no=".01"), START + 550, "KXBTC15M") is None

@pytest.mark.parametrize("price", [".59", ".72", ".91"])
def test_no_side_can_qualify_independently(price):
    p = plan_single_coin(market(yes=".01", no=price), START + 550, "KXBTC15M")
    assert p and p["side"] == "NO" and D(p["ask"]) == D(price)

@pytest.mark.parametrize("elapsed", [0, 539.9, 600, 610, 890])
def test_outside_minute_nine_rejected(elapsed):
    assert plan_single_coin(market(), START + elapsed, "KXBTC15M") is None

@pytest.mark.parametrize("elapsed", [540, 550, 599.9])
def test_inside_minute_nine_allowed(elapsed):
    assert plan_single_coin(market(), START + elapsed, "KXBTC15M")

def test_both_sides_in_range_skips_ambiguity():
    assert plan_single_coin(market(yes=".61", no=".60"), START + 550, "KXBTC15M") is None

def test_closed_market_skips():
    assert plan_single_coin(market(status="closed"), START + 550, "KXBTC15M") is None

def test_price_rounding_never_exceeds_91():
    p = plan_single_coin(market(yes=".9001", no=".01"), START + 550, "KXBTC15M")
    assert p["limit"] == ".91" or p["limit"] == "0.91"

def test_journal_persists_one_order_per_coin(tmp_path):
    state = tmp_path / "minute9.json"
    first = Journal(state)
    first.data["windows"]["KXBTC15M-xyz"] = {"buy": {"client_id": "unique-btc"}}
    first.data["windows"]["KXETH15M-xyz"] = {"buy": {"client_id": "unique-eth"}}
    first.save()
    assert len(Journal(state).data["windows"]) == 2

def test_older_preview_half_budget_state_migrates_to_reduced_mode(tmp_path):
    path = tmp_path / "old_streak.json"
    path.write_text('{"schema":1,"windows":{},"streak":{"consecutive_wins":3,"half_budget":true}}')
    migrated = Journal(path)
    assert migrated.data["streak"] == {"consecutive_wins": 3, "reduced_budget": True}
    assert trade_budget(migrated.data["streak"], D("20")) == D("20") / D(3)
    assert Journal(path).data == migrated.data


def test_corrupt_journal_refused(tmp_path):
    state = tmp_path / "minute9.json"
    state.write_text('{"windows": {}}')
    with pytest.raises(RuntimeError):
        Journal(state)


def test_three_wins_trigger_third_budget_and_repeat_after_loss():
    s = starting_streak()
    for wins in (1, 2):
        s = next_streak(s, "win")
        assert s == {"consecutive_wins": wins, "reduced_budget": False}
    s = next_streak(s, "win")
    assert s == {"consecutive_wins": 3, "reduced_budget": True}
    for _ in range(6):
        s = next_streak(s, "win")
        assert s["reduced_budget"] and trade_budget(s, D("20")) == D("20") / D(3)
    s = next_streak(s, "loss")
    assert s == starting_streak()
    for _ in range(3):
        s = next_streak(s, "win")
    assert s["reduced_budget"]


def test_loss_before_streak_resets_progress():
    s = next_streak(next_streak(starting_streak(), "win"), "win")
    assert next_streak(s, "loss") == starting_streak()
    assert next_streak(starting_streak(), "win")["consecutive_wins"] == 1


def test_flat_interrupts_streak_but_does_not_restore_half_mode():
    s = next_streak(starting_streak(), "win")
    assert next_streak(s, "flat") == starting_streak()
    s = next_streak(next_streak(next_streak(starting_streak(), "win"), "win"), "win")
    assert next_streak(s, "flat")["reduced_budget"] is True


def test_budget_uses_one_third_of_base_dollars_not_contracts():
    base = D("20")
    normal = budgeted_quantity(trade_budget(starting_streak(), base), D(".72"))
    s = starting_streak()
    for _ in range(3):
        s = next_streak(s, "win")
    reduced = budgeted_quantity(trade_budget(s, base), D(".72"))
    assert normal == 26 and reduced == 8
    assert trade_budget(s, D("20")) > D("6.66")
    assert trade_budget(s, D("20")) < D("6.67")
    assert budgeted_quantity(D(".60"), D(".59")) == 0
    assert budgeted_quantity(D("10"), D(".91")) == 10
    assert budgeted_quantity(D("1000"), D(".59"), cap=8) == 8


def test_advance_outcomes_once_and_persist_after_restart(tmp_path):
    state = tmp_path / "journal.json"
    j = Journal(state)
    for name, status, time_s in [
        ("btc", "win", 100), ("eth", "win", 102), ("xrp", "win", 104),
    ]:
        j.data["windows"][name] = {
            "ticker": name,
            "outcome": {"status": status, "completed_at": time_s, "counted": False}
        }
    j.save()
    apply_new_outcomes(j)
    assert j.data["streak"]["reduced_budget"] is True
    apply_new_outcomes(Journal(state))  # No double-counting after restart
    assert Journal(state).data["streak"] == j.data["streak"]
    j = Journal(state)
    j.data["windows"]["sol"] = {"ticker": "sol",
        "outcome": {"status": "win", "completed_at": 107, "counted": False}}
    j.data["windows"]["hype"] = {"ticker": "hype",
        "outcome": {"status": "loss", "completed_at": 108, "counted": False}}
    j.save()
    apply_new_outcomes(j)
    assert Journal(state).data["streak"] == starting_streak()


def test_no_fill_does_not_break_three_win_streak(tmp_path):
    j = Journal(tmp_path / "no_fill.json")
    j.data["streak"] = next_streak(next_streak(starting_streak(), "win"), "win")
    j.data["windows"]["miss"] = {"ticker": "miss",
        "outcome": {"status": "no_fill", "completed_at": 200, "counted": False}}
    apply_new_outcomes(j)
    assert j.data["streak"] == {"consecutive_wins": 2, "reduced_budget": False}


def test_invalid_streak_fails_closed(tmp_path):
    state = tmp_path / "broken.json"
    j = Journal(state)
    j.data["streak"]["consecutive_wins"] = 100
    j.save()
    with pytest.raises(ValueError):
        Journal(state)
    with pytest.raises(ValueError):
        next_streak(starting_streak(), "pending")


class FakeTradeClient:
    def __init__(self, *, result=None, fees="0.01", fully_sold=True, side="YES"):
        self.ticker = "KXBTC15M-TEST"
        self.side = side
        self.market_status = "active" if fully_sold else "finalized"
        self.result = result
        self.fees = fees
        self.orders = {
            "entry": {"order_id": "entry", "ticker": self.ticker,
                      "status": "executed", "fill_count_fp": "2"},
        }
        if fully_sold:
            self.orders["exit"] = {
                "order_id": "exit", "ticker": self.ticker,
                "status": "executed", "fill_count_fp": "2"
            }
        def fill(order_id, price, at):
            yes_price = D(price) if side == "YES" else D("1") - D(price)
            return {"order_id": order_id, "ticker": self.ticker,
                "count_fp": "2", "yes_price_dollars": str(yes_price),
                "no_price_dollars": str(D("1") - yes_price),
                "fee_cost": fees,
                "created_time": datetime.fromtimestamp(at, timezone.utc).isoformat()}
        self.rows = [fill("entry", ".72", START + 550)]
        if fully_sold:
            self.rows.append(fill("exit", ".77", START + 570))

    def market(self, ticker):
        assert ticker == self.ticker
        return {"ticker": ticker, "status": self.market_status,
                "result": self.result, "close_time": datetime.fromtimestamp(CLOSE, timezone.utc).isoformat()}

    def order(self, order_id, ticker):
        assert ticker == self.ticker
        return self.orders[order_id]

    def all_fills(self, ticker):
        return list(self.rows)


def fake_record(client, *, fully_sold=True):
    return {"ticker": client.ticker, "side": client.side, "quantity": "2",
            "buy": {"client_id": "entry-id", "order_id": "entry"},
            "sells": ([{"client_id": "exit-id", "order_id": "exit",
                        "quantity": "2"}] if fully_sold else [])}


def test_full_verified_take_profit_counts_net_win(tmp_path):
    j = Journal(tmp_path / "wins.json")
    client = FakeTradeClient(fully_sold=True, fees=".01")
    r = fake_record(client)
    j.data["windows"][client.ticker] = r
    verify_completed_trade(client, j, r, START + 575)
    assert r["outcome"]["status"] == "win"
    assert D(r["outcome"]["net_pnl_dollars"]) == D(".08")
    apply_new_outcomes(j)
    assert j.data["streak"]["consecutive_wins"] == 1


def test_market_settlement_loss_resets_reduced_budget(tmp_path):
    j = Journal(tmp_path / "settlement.json")
    client = FakeTradeClient(result="no", fully_sold=False, side="YES")
    r = fake_record(client, fully_sold=False)
    j.data["windows"][client.ticker] = r
    for _ in range(3):
        j.data["streak"] = next_streak(j.data["streak"], "win")
    verify_completed_trade(client, j, r, CLOSE + 15)
    assert r["outcome"]["status"] == "loss"
    apply_new_outcomes(j)
    assert j.data["streak"] == starting_streak()


def test_market_settlement_win_qualifies_as_win(tmp_path):
    j = Journal(tmp_path / "settlement_win.json")
    client = FakeTradeClient(result="yes", fully_sold=False, side="YES")
    r = fake_record(client, fully_sold=False)
    j.data["windows"][client.ticker] = r
    verify_completed_trade(client, j, r, CLOSE + 15)
    assert r["outcome"]["status"] == "win"


def test_fee_can_turn_gross_win_into_net_loss(tmp_path):
    j = Journal(tmp_path / "fees.json")
    client = FakeTradeClient(fully_sold=True, fees=".06")
    r = fake_record(client)
    j.data["windows"][client.ticker] = r
    verify_completed_trade(client, j, r, START + 575)
    assert r["outcome"]["status"] == "loss"


def test_missing_fee_does_not_count_unverified_result(tmp_path):
    j = Journal(tmp_path / "fee_missing.json")
    client = FakeTradeClient(fully_sold=True)
    client.rows[-1].pop("fee_cost")
    r = fake_record(client)
    j.data["windows"][client.ticker] = r
    with pytest.raises(KeyError):
        verify_completed_trade(client, j, r, START + 575)
    assert "outcome" not in r


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_eleven_cent_stop_threshold_has_priority_on_either_side(side):
    # Price is expressed in cents of the held outcome, irrespective of side.
    assert exit_decision(D(".72"), D(".62")) == (None, None)
    assert exit_decision(D(".72"), D(".61")) == ("stop_loss", D(".61"))
    assert exit_decision(D(".72"), D(".60")) == ("stop_loss", D(".60"))
    assert exit_decision(D(".72"), D(".77")) == ("take_profit", D(".77"))


def test_gap_through_stop_uses_executable_bid_not_fictional_eleven_cent_fill():
    assert exit_decision(D(".75"), D(".44")) == ("stop_loss", D(".44"))
    assert exit_decision(D(".75"), D("0")) == ("stop_loss", None)
    assert exit_decision(D(".75"), D(".83"), stop_armed=True) == ("stop_loss", D(".83"))


class StopExchange(FakeTradeClient):
    def __init__(self, *, side="YES", bid=".60"):
        super().__init__(side=side, fully_sold=False)
        self.market_status = "active"
        self.bid = D(bid)
        self.held = D(2) if side == "YES" else D(-2)
        self.submissions = []
        self.lost_ack = False

    def market(self, ticker):
        result = super().market(ticker)
        result[self.side.lower() + "_bid_dollars"] = str(self.bid)
        return result

    def positions(self, ticker):
        return [{"ticker": ticker, "position_fp": str(self.held), "subaccount_number": 0}]

    def all_orders(self, ticker, status=None):
        return []

    def place_take_profit(self, ticker, signed_quantity, outcome_price,
                          expiration_time=None, *, client_order_id=None):
        self.submissions.append({"ticker": ticker, "signed": D(signed_quantity),
                                 "limit": D(outcome_price), "client_id": client_order_id})
        if self.lost_ack:
            raise TimeoutError("simulated lost acknowledgement")
        order_id = f"exit-{len(self.submissions)}"
        # An accepted IOC order can remain pending while the venue's fill view
        # catches up. A pending order must never be treated as a confirmed exit.
        self.orders[order_id] = {"order_id": order_id, "ticker": ticker,
                                 "status": "pending", "fill_count_fp": "0"}
        return {"order": {"order_id": order_id}}

    def simulate_partial_fill(self, quantity="1", price=".60"):
        name = "exit-1"
        self.orders[name] = {"ticker": self.ticker, "order_id": name,
                             "status": "canceled", "fill_count_fp": quantity}
        yes_price = D(price) if self.side == "YES" else D(1) - D(price)
        self.rows.append({"order_id": name, "ticker": self.ticker,
                          "count_fp": quantity, "yes_price_dollars": str(yes_price),
                          "no_price_dollars": str(D(1)-yes_price),
                          "fee_cost": "0.01",
                          "created_time": datetime.fromtimestamp(START + 574, timezone.utc).isoformat()})
        q = D(quantity)
        self.held -= q if self.side == "YES" else -q


@pytest.mark.parametrize("side,expected_sign", [("YES", 2), ("NO", -2)])
def test_stop_submits_one_reduce_only_ioc_and_latches_across_restart(tmp_path, side, expected_sign):
    journal = Journal(tmp_path / f"stop-{side}.json")
    client = StopExchange(side=side, bid=".60")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    journal.save()
    reconcile_exit(client, journal, record, START + 574)
    assert record["stop_armed"]["threshold"] == "0.61"
    assert record["sells"][0]["purpose"] == "stop_loss"
    assert client.submissions == [
        {"ticker": client.ticker, "signed": D(expected_sign),
         "limit": D(".60"), "client_id": record["sells"][0]["client_id"]}
    ]
    assert Journal(journal.path).data["windows"][client.ticker]["stop_armed"]
    # Unknown exit status cannot send a second order.
    reconcile_exit(client, journal, record, START + 575)
    assert len(client.submissions) == 1


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_partial_stop_fill_retries_only_remainder_even_after_quote_recovers(tmp_path, side):
    journal = Journal(tmp_path / f"partial-{side}.json")
    client = StopExchange(side=side, bid=".60")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    reconcile_exit(client, journal, record, START + 571)
    client.simulate_partial_fill("1", ".60")
    client.bid = D(".75")
    saved = Journal(journal.path)
    record = saved.data["windows"][client.ticker]
    reconcile_exit(client, saved, record, START + 575)
    assert len(client.submissions) == 2
    second = client.submissions[-1]
    assert abs(second["signed"]) == D(1)
    assert second["limit"] == D(".75")
    assert record["sells"][1]["purpose"] == "stop_loss"


def test_unacknowledged_stop_not_repeated(tmp_path):
    journal = Journal(tmp_path / "unknown.json")
    client = StopExchange(bid=".60")
    client.lost_ack = True
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    with pytest.raises(TimeoutError):
        reconcile_exit(client, journal, record, START + 570)
    persisted = Journal(journal.path).data["windows"][client.ticker]
    assert persisted["stop_armed"]
    assert len(persisted["sells"]) == 1
    # Without acknowledged order identity it waits, not selling the lot twice.
    reconcile_exit(client, journal, record, START + 571)
    assert len(client.submissions) == 1


def test_stop_not_sent_before_threshold_and_tp_unchanged(tmp_path):
    journal = Journal(tmp_path / "not_yet.json")
    client = StopExchange(bid=".62")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    reconcile_exit(client, journal, record, START + 570)
    assert not client.submissions and "stop_armed" not in record
    client.bid = D(".77")
    reconcile_exit(client, journal, record, START + 571)
    assert len(client.submissions) == 1
    assert record["sells"][0]["purpose"] == "take_profit"
    assert client.submissions[0]["limit"] == D(".77")


def test_stop_execution_realized_loss_resets_win_streak(tmp_path):
    journal = Journal(tmp_path / "realized-loss.json")
    client = FakeTradeClient(fully_sold=True, fees=".01")
    # A stop exit at 60c following a 72c entry loses 12c/contract.
    client.rows[-1]["yes_price_dollars"] = ".60"
    client.rows[-1]["no_price_dollars"] = ".40"
    record = fake_record(client)
    record["stop_armed"] = {"at": START + 565, "threshold": ".61", "trigger_bid": ".60"}
    record["sells"][0]["purpose"] = "stop_loss"
    journal.data["windows"][client.ticker] = record
    for _ in range(3):
        journal.data["streak"] = next_streak(journal.data["streak"], "win")
    verify_completed_trade(client, journal, record, START + 585)
    assert record["outcome"]["status"] == "loss"
    apply_new_outcomes(journal)
    assert journal.data["streak"] == starting_streak()


class PreviewCoinClient:
    def __init__(self):
        self.signals = 0
        self.posted = []
        self.series = ("KXBTC15M", "KXETH15M", "KXSOL15M")

    def markets(self, *, series_ticker, status, limit):
        assert status == "open"
        return [market(yes=".75", no=".24", series=series_ticker)]

    def positions(self, ticker):
        return []

    def all_orders(self, ticker, status=None):
        return []

    def market(self, ticker):
        return market(yes=".75", no=".24", series=ticker.split("-")[0])

    def place_entry(self, ticker, side, quantity, limit, expiry, **kwargs):
        self.posted.append((ticker, side, quantity, limit))
        return {"order": {"order_id": "entry-1"}}


def test_one_global_entry_in_five_single_coin_markets(tmp_path, monkeypatch):
    import single_coin_minute9 as mod
    bot = PreviewCoinClient()
    j = Journal(tmp_path / "cap.json")
    events = []
    monkeypatch.setattr(mod, "event", lambda name, **data: events.append((name, data)))
    # Freeze clock so the single minute-9 window remains eligible.
    monkeypatch.setattr(mod.time, "time", lambda: START + 550)
    run_pass(bot, j, bot.series, D("20"), 100, False, START + 550)
    preview = [e for e in events if e[0] == "M9_DRY_RUN_SIGNAL"]
    assert len(preview) == 1
    assert preview[0][1]["series"] == "KXBTC15M"
    # The actual live run records a durable one-window attempt, no other coin buy.
    run_pass(bot, j, bot.series, D("20"), 100, True, START + 550)
    assert len(bot.posted) == 1
    assert window_entry_count(j, START * 1000) == 1
    assert list(j.data["windows"]) == [bot.posted[0][0]]
    assert j.data["windows"][bot.posted[0][0]]["quantity"] == "25"
    # Even across a restart, the pending first attempt blocks other coin entries.
    from single_coin_minute9 import Journal as Reopened
    reopened = Reopened(j.path)
    run_pass(bot, reopened, bot.series, D("20"), 100, True, START + 550)
    assert len(bot.posted) == 1


def test_more_than_one_coin_trade_requires_explicit_override(tmp_path, monkeypatch):
    import single_coin_minute9 as mod
    bot = PreviewCoinClient()
    j = Journal(tmp_path / "cap-two.json")
    monkeypatch.setattr(mod.time, "time", lambda: START + 550)
    run_pass(bot, j, bot.series, D("20"), 100, True, START + 550,
             max_entries_per_window=2)
    assert len(bot.posted) == 2
    assert window_entry_count(j, START * 1000) == 2


def test_window_attempts_are_exactly_scoped(tmp_path):
    j = Journal(tmp_path / "cap-scope.json")
    j.data["windows"]["a"] = {"ticker": "a", "window_start_ms": START * 1000,
                                "buy": {"client_id": "unconfirmed"}}
    j.data["windows"]["b"] = {"ticker": "b", "window_start_ms": (START+900) * 1000,
                                "buy": {"client_id": "confirmed"}}
    j.save()
    assert window_entry_count(Journal(j.path), START * 1000) == 1
    assert window_entry_count(Journal(j.path), (START+900) * 1000) == 1
    with pytest.raises(ValueError):
        window_entry_count(j, START * 1000 + 123)
