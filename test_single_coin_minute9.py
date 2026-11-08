"""Offline tests for the separate 15-minute coin markets (not Coin Race)."""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from single_coin_minute9 import (Journal, DEFAULT_SERIES, plan_single_coin,
    starting_streak, next_streak, entry_min_price, trade_budget, budgeted_quantity,
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

@pytest.mark.parametrize("price", [".69", ".70", ".72", ".88", ".89"])
def test_yes_boundaries_are_inclusive(price):
    p = plan_single_coin(market(yes=price, no=".01"), START + 550, "KXBTC15M")
    assert p and p["side"] == "YES" and D(p["ask"]) == D(price)

@pytest.mark.parametrize("price", [".01", ".58", ".68", ".6899", ".90", ".91", ".9101", ".92", ".99"])
def test_no_buy_outside_price_band(price):
    assert plan_single_coin(market(yes=price, no=".01"), START + 550, "KXBTC15M") is None

@pytest.mark.parametrize("price", [".69", ".72", ".89"])
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
    assert plan_single_coin(market(yes=".71", no=".70"), START + 550, "KXBTC15M") is None

def test_closed_market_skips():
    assert plan_single_coin(market(status="closed"), START + 550, "KXBTC15M") is None

def test_price_rounding_never_exceeds_89():
    p = plan_single_coin(market(yes=".8801", no=".01"), START + 550, "KXBTC15M")
    assert p["limit"] == ".89" or p["limit"] == "0.89"

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
    assert trade_budget(migrated.data["streak"], D("20")) == D("20")
    assert Journal(path).data == migrated.data


def test_corrupt_journal_refused(tmp_path):
    state = tmp_path / "minute9.json"
    state.write_text('{"windows": {}}')
    with pytest.raises(RuntimeError):
        Journal(state)


def test_two_full_budget_wins_then_small_until_first_loss():
    s = starting_streak()
    assert trade_budget(s, D("6.66")) == D("2.22")
    # Small mode does not grow after any number of wins.
    for _ in range(12):
        s = next_streak(s, "win")
        assert trade_budget(s, D("6.66")) == D("2.22")
    # First small-budget loss moves to full allocation.
    s = next_streak(s, "loss")
    assert s == {"consecutive_wins": 0, "reduced_budget": True,
                 "full_budget_wins": 0}
    assert trade_budget(s, D("6.66")) == D("6.66")
    s = next_streak(s, "win")
    assert s["full_budget_wins"] == 1 and trade_budget(s, D("6.66")) == D("6.66")
    s = next_streak(s, "win")
    assert s == {"consecutive_wins": 2, "reduced_budget": False}
    assert trade_budget(s, D("6.66")) == D("2.22")
    for _ in range(20):
        s = next_streak(s, "win")
        assert trade_budget(s, D("6.66")) == D("2.22")
    assert trade_budget(next_streak(s, "loss"), D("6.66")) == D("6.66")


def test_two_win_full_phase_requires_consecutive_wins():
    full = next_streak(starting_streak(), "loss")
    one_win = next_streak(full, "win")
    assert next_streak(one_win, "flat") == full
    assert next_streak(one_win, "loss") == full
    assert next_streak(one_win, "win")["reduced_budget"] is False
    assert next_streak(full, "win")["full_budget_wins"] == 1
    assert next_streak(starting_streak(), "flat") == starting_streak()

def test_loss_resets_global_wins_and_restarts_full_budget_phase():
    s = next_streak(next_streak(starting_streak(), "win"), "win")
    assert next_streak(s, "loss") == {
        "consecutive_wins": 0, "reduced_budget": True, "full_budget_wins": 0}
    assert next_streak(starting_streak(), "win")["consecutive_wins"] == 1

def test_ten_consecutive_wins_unlock_60c_floor_and_loss_restores_69c():
    s = starting_streak()
    assert entry_min_price(s) == D(".69")
    for wins in range(1, 10):
        s = next_streak(s, "win")
        assert s["consecutive_wins"] == wins
        assert entry_min_price(s) == D(".69")
    s = next_streak(s, "win")
    assert s["consecutive_wins"] == 10
    assert s["reduced_budget"] is False  # small phase stays until a loss
    assert entry_min_price(s) == D(".60")
    # Additional wins keep the unlocked floor without overflowing persisted state.
    s = next_streak(s, "win")
    assert s["consecutive_wins"] == 10
    assert entry_min_price(s) == D(".60")
    assert entry_min_price(next_streak(s, "loss")) == D(".69")


def test_flat_breaks_ten_win_streak_without_changing_small_budget():
    s = starting_streak()
    for _ in range(10):
        s = next_streak(s, "win")
    assert entry_min_price(s) == D(".60")
    flat = next_streak(s, "flat")
    assert flat == starting_streak()
    assert entry_min_price(flat) == D(".69")

def test_relaxed_floor_allows_60c_but_never_below_it():
    s = starting_streak()
    for _ in range(10):
        s = next_streak(s, "win")
    floor = entry_min_price(s)
    assert plan_single_coin(market(yes=".60", no=".01"), START + 550,
                            "KXBTC15M", min_price=floor)
    assert plan_single_coin(market(yes=".5999", no=".01"), START + 550,
                            "KXBTC15M", min_price=floor) is None
    assert budgeted_quantity(D("10"), D(".60"), min_price=floor) > 0


def test_flat_resets_full_budget_win_counter_without_budget_change():
    s = next_streak(starting_streak(), "loss")
    s = next_streak(s, "win")
    assert s["full_budget_wins"] == 1
    flat = next_streak(s, "flat")
    assert flat == next_streak(starting_streak(), "loss")
    assert trade_budget(flat, D("6.66")) == D("6.66")


def test_budget_uses_one_third_until_loss_then_full_dollars():
    base = D("20")
    small = budgeted_quantity(trade_budget(starting_streak(), base), D(".72"))
    s = next_streak(starting_streak(), "loss")
    full = budgeted_quantity(trade_budget(s, base), D(".72"))
    assert small == 8 and full == 26
    assert trade_budget(s, D("20")) == D("20")
    s = next_streak(next_streak(s, "win"), "win")
    assert trade_budget(s, D("20")) == D("20") / D(3)
    assert budgeted_quantity(D(".70"), D(".69")) == 0
    assert budgeted_quantity(D("10"), D(".89")) == 10
    assert budgeted_quantity(D("1000"), D(".69"), cap=8) == 8

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
    assert j.data["streak"]["reduced_budget"] is False
    apply_new_outcomes(Journal(state))  # No double-counting after restart
    assert Journal(state).data["streak"] == j.data["streak"]
    j = Journal(state)
    j.data["windows"]["sol"] = {"ticker": "sol",
        "outcome": {"status": "win", "completed_at": 107, "counted": False}}
    j.data["windows"]["hype"] = {"ticker": "hype",
        "outcome": {"status": "loss", "completed_at": 108, "counted": False}}
    j.save()
    apply_new_outcomes(j)
    assert Journal(state).data["streak"] == next_streak(starting_streak(), "loss")


def test_late_fractional_sol_win_cannot_undo_newer_xrp_loss(tmp_path):
    """Regression for SOL 06:45 arriving after the later XRP loss."""
    state = tmp_path / "chronological.json"
    j = Journal(state)
    j.data["windows"] = {
        "old-eth": {"ticker": "old-eth", "outcome": {
            "status": "win", "completed_at": 100, "counted": True}},
        "new-xrp": {"ticker": "new-xrp", "outcome": {
            "status": "loss", "completed_at": 300, "counted": True}},
    }
    j.data["streak"] = next_streak(starting_streak(), "loss")
    j.save()
    j.data["windows"]["late-sol"] = {"ticker": "late-sol", "outcome": {
        "status": "win", "completed_at": 200, "counted": False}}
    apply_new_outcomes(j)
    assert j.data["streak"] == next_streak(starting_streak(), "loss")
    assert trade_budget(j.data["streak"], D("6.66")) == D("6.66")
    assert j.data["windows"]["late-sol"]["outcome"]["counted"] is True
    assert Journal(state).data["streak"] == j.data["streak"]


def test_late_loss_recomputes_later_winning_run(tmp_path):
    j = Journal(tmp_path / "late_loss.json")
    j.data["windows"] = {
        f"win-{i}": {"ticker": f"win-{i}", "outcome": {
            "status": "win", "completed_at": n, "counted": True}}
        for i, n in enumerate((100, 200, 300))
    }
    j.data["streak"] = next_streak(next_streak(next_streak(
        starting_streak(), "win"), "win"), "win")
    j.data["windows"]["late-loss"] = {"ticker": "late-loss", "outcome": {
        "status": "loss", "completed_at": 250, "counted": False}}
    apply_new_outcomes(j)
    assert j.data["streak"]["consecutive_wins"] == 1
    assert j.data["streak"]["reduced_budget"] is True
    assert j.data["streak"]["full_budget_wins"] == 1


def test_restart_repairs_poisoned_historical_streak_without_recounting(tmp_path):
    path = tmp_path / "poisoned.json"
    j = Journal(path)
    j.data["windows"] = {
        "old-sol": {"ticker": "old-sol", "outcome": {
            "status": "win", "completed_at": 100, "counted": True}},
        "new-xrp": {"ticker": "new-xrp", "outcome": {
            "status": "loss", "completed_at": 200, "counted": True}},
    }
    j.data["streak"] = {"consecutive_wins": 1, "reduced_budget": True,
                         "full_budget_wins": 1}
    j.save()
    reopened = Journal(path)
    assert reopened.data["streak"] == next_streak(starting_streak(), "loss")
    assert all(v["outcome"]["counted"] for v in reopened.data["windows"].values())
    assert Journal(path).data == reopened.data


def test_no_fill_only_keeps_legacy_seeded_streak(tmp_path):
    j = Journal(tmp_path / "seeded.json")
    j.data["streak"] = next_streak(starting_streak(), "loss")
    j.data["windows"]["miss"] = {"ticker": "miss", "outcome": {
        "status": "no_fill", "completed_at": 200, "counted": False}}
    apply_new_outcomes(j)
    assert j.data["streak"] == next_streak(starting_streak(), "loss")


@pytest.mark.parametrize("invalid", [None, True, -1, float("nan"), float("inf"), "200"])
def test_invalid_historical_completion_time_fails_closed(tmp_path, invalid):
    j = Journal(tmp_path / "bad-history.json")
    j.data["windows"]["bad"] = {"ticker": "bad", "outcome": {
        "status": "win", "completed_at": invalid, "counted": False}}
    before = j.data["streak"].copy()
    with pytest.raises(RuntimeError, match="completion time"):
        apply_new_outcomes(j)
    assert j.data["streak"] == before
    assert j.data["windows"]["bad"]["outcome"]["counted"] is False


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
            "entry": {"order_id": "entry", "client_order_id": "entry-id", "ticker": self.ticker,
                      "status": "executed", "fill_count_fp": "2"},
        }
        if fully_sold:
            self.orders["exit"] = {
                "order_id": "exit", "client_order_id": "exit-id", "ticker": self.ticker,
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


@pytest.mark.parametrize("count", ["0", "0.01", "0.25", "0.75", "1.25", "3.00"])
def test_fractional_contract_quantities_are_exact(count):
    from single_coin_minute9 import filled_quantity
    rows = [{"order_id": "owned", "count_fp": count}]
    qty, matched = filled_quantity(rows, "owned")
    assert qty == D(count) and matched == rows
    # Never adopt another order's fill as bot inventory.
    assert filled_quantity(rows, "outside") == (D(0), [])


@pytest.mark.parametrize("value", [None, True, False, "NaN", "Infinity", "-0.01", "0.005",
                                  "1.001", "bad", "", {}, []])
def test_invalid_or_unverifiable_fill_quantity_fails_closed(value):
    from single_coin_minute9 import filled_quantity
    with pytest.raises(RuntimeError, match="Unsupported or invalid fill quantity"):
        filled_quantity([{"order_id": "entry", "count_fp": value}], "entry")


def test_missing_fill_count_cannot_be_silently_zero():
    from single_coin_minute9 import filled_quantity
    with pytest.raises(RuntimeError, match="Unsupported or invalid fill quantity"):
        filled_quantity([{"order_id": "entry"}], "entry")
    assert filled_quantity([], "entry") == (D(0), [])


def test_partial_fractional_fills_sum_exactly():
    from single_coin_minute9 import filled_quantity
    rows = [{"order_id": "entry", "count_fp": ".25"},
            {"order_id": "entry", "count_fp": ".50"},
            {"order_id": "outside", "count_fp": "2"}]
    assert filled_quantity(rows, "entry") == (D(".75"), rows[:2])


def test_fractional_full_exit_reconciles_pnl_and_restart(tmp_path):
    state = tmp_path / "fractional_full.json"
    journal = Journal(state)
    client = FakeTradeClient(fully_sold=True)
    client.rows[0]["count_fp"] = "1.25"
    client.rows[1]["count_fp"] = "1.25"
    client.orders["entry"]["fill_count_fp"] = "1.25"
    client.orders["exit"]["fill_count_fp"] = "1.25"
    record = fake_record(client)
    journal.data["windows"][client.ticker] = record
    journal.save()
    journal = Journal(state)
    record = journal.data["windows"][client.ticker]
    verify_completed_trade(client, journal, record, START + 575)
    assert record["outcome"]["status"] == "win"
    assert D(record["outcome"]["bought"]) == D("1.25")
    assert D(record["outcome"]["sold"]) == D("1.25")
    assert D(record["outcome"]["net_pnl_dollars"]) == D(".0425")
    apply_new_outcomes(journal)
    assert Journal(state).data["streak"]["consecutive_wins"] == 1


def test_fractional_remainder_settlement_is_not_lost(tmp_path):
    journal = Journal(tmp_path / "fractional_settlement.json")
    client = FakeTradeClient(result="yes", fully_sold=True)
    client.market_status = "settled"
    client.rows[0]["count_fp"] = "1.25"
    client.rows[1]["count_fp"] = ".25"
    client.orders["entry"]["fill_count_fp"] = "1.25"
    client.orders["exit"]["fill_count_fp"] = ".25"
    record = fake_record(client)
    journal.data["windows"][client.ticker] = record
    verify_completed_trade(client, journal, record, CLOSE + 5)
    assert record["outcome"]["status"] == "win"
    assert D(record["outcome"]["remaining_at_settlement"]) == D("1.00")
    assert D(record["outcome"]["net_pnl_dollars"]) == D(".2725")


def test_fractional_partial_stop_retries_only_remaining_after_receipt(tmp_path):
    journal = Journal(tmp_path / "fractional_stop.json")
    client = StopExchange(side="YES", bid=".51")
    client.rows[0]["count_fp"] = ".75"
    client.orders["entry"]["fill_count_fp"] = ".75"
    client.held = D(".75")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    reconcile_exit(client, journal, record, START + 570)
    assert len(client.submissions) == 1
    assert abs(client.submissions[0]["signed"]) == D(".75")
    client.simulate_partial_fill(".25", ".51")
    client.bid = D(".75")
    saved = Journal(journal.path)
    record = saved.data["windows"][client.ticker]
    reconcile_exit(client, saved, record, START + 571)
    assert len(client.submissions) == 2
    assert abs(client.submissions[-1]["signed"]) == D(".50")
    assert record["sells"][-1]["purpose"] == "stop_loss"


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


def test_market_settlement_loss_returns_to_full_budget(tmp_path):
    j = Journal(tmp_path / "settlement.json")
    client = FakeTradeClient(result="no", fully_sold=False, side="YES")
    r = fake_record(client, fully_sold=False)
    j.data["windows"][client.ticker] = r
    for _ in range(3):
        j.data["streak"] = next_streak(j.data["streak"], "win")
    verify_completed_trade(client, j, r, CLOSE + 15)
    assert r["outcome"]["status"] == "loss"
    apply_new_outcomes(j)
    assert j.data["streak"] == next_streak(starting_streak(), "loss")


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


@pytest.mark.parametrize("raw,expected", [
    ("21", D(".21")), ("1", D(".01")), ("99", D(".99")),
])
def test_explicit_entry_relative_stop_setting_parses_integer_cents(raw, expected):
    from single_coin_minute9 import stop_drop_from_cents
    assert stop_drop_from_cents(raw) == expected


@pytest.mark.parametrize("invalid", ["", "0", "100", "21.0", "-21", "2I", "21c", " 21", "21 ", None])
def test_invalid_entry_relative_stop_setting_refused(invalid):
    from single_coin_minute9 import stop_drop_from_cents
    with pytest.raises(ValueError, match="M9_STOP_DROP_CENTS"):
        stop_drop_from_cents(invalid)


def test_entry_relative_21c_stop_and_durable_journal(tmp_path):
    import single_coin_minute9 as mod
    assert mod.STOP_DROP == D(".21")
    assert mod.exit_decision(D(".75"), D(".55")) == (None, None)
    assert mod.exit_decision(D(".75"), D(".54")) == ("stop_loss", D(".54"))
    assert mod.exit_decision(D(".75"), D(".53")) == ("stop_loss", D(".53"))
    journal = Journal(tmp_path / "stop-entry-relative.json")
    client = StopExchange(side="YES", bid=".51")  # actual entry cost .72
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    journal.save()
    mod.reconcile_exit(client, journal, record, START + 574)
    assert D(record["stop_armed"]["threshold"]) == D(".51")
    assert D(record["stop_armed"]["entry_cost"]) == D(".72")
    assert record["sells"][0]["purpose"] == "stop_loss"
    assert client.submissions[0]["limit"] == D(".51")
    reopened = Journal(journal.path).data["windows"][client.ticker]
    assert D(reopened["stop_armed"]["threshold"]) == D(".51")


@pytest.mark.parametrize("entry,target", [
    (".60", ".70"), (".72", ".82"), (".725", ".83"),
    (".80", ".90"), (".89", ".99"),
])
def test_stop_threshold_always_21c_below_actual_fill(entry, target):
    cost, tp = D(entry), D(target)
    trigger = cost - D(".21")
    assert exit_decision(cost, trigger + D(".0001")) == (None, None)
    assert exit_decision(cost, trigger) == ("stop_loss", trigger)
    assert exit_decision(cost, trigger - D(".01")) == ("stop_loss", trigger - D(".01"))
    assert exit_decision(cost, tp) == ("take_profit", tp)


def test_gap_below_21c_stop_uses_executable_bid_not_a_guaranteed_fill():
    assert exit_decision(D(".75"), D(".32")) == ("stop_loss", D(".32"))
    assert exit_decision(D(".75"), D("0")) == ("stop_loss", None)
    assert exit_decision(D(".75"), D(".83"), stop_armed=True) == ("stop_loss", D(".83"))


class StopExchange(FakeTradeClient):
    def __init__(self, *, side="YES", bid=".44"):
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
        self.orders[order_id] = {"order_id": order_id, "client_order_id": client_order_id, "ticker": ticker,
                                 "status": "pending", "fill_count_fp": "0"}
        return {"order": {"order_id": order_id}}

    def place_resting_take_profit(self, *args, **kwargs):
        result = self.place_take_profit(*args, **kwargs)
        self.orders[result["order"]["order_id"]]["status"] = "resting"
        return result

    def place_resting_stop(self, *args, **kwargs):
        result = self.place_take_profit(*args, **kwargs)
        self.orders[result["order"]["order_id"]]["status"] = "resting"
        return result

    def cancel(self, order_id, ticker):
        self.orders[order_id]["status"] = "canceled"

    def simulate_partial_fill(self, quantity="1", price=".44"):
        name = "exit-1"
        self.orders[name] = {"ticker": self.ticker, "order_id": name,
                             "client_order_id": self.orders[name]["client_order_id"],
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
@pytest.mark.parametrize("entry", [".59", ".72", ".725", ".80", ".91"])
def test_stop_submits_one_reduce_only_ioc_and_latches_across_restart(tmp_path, side, expected_sign, entry):
    journal = Journal(tmp_path / f"stop-{side}.json")
    threshold = D(entry) - D(".21")
    client = StopExchange(side=side, bid=str(threshold))
    yes_price = D(entry) if side == "YES" else D(1) - D(entry)
    client.rows[0]["yes_price_dollars"] = str(yes_price)
    client.rows[0]["no_price_dollars"] = str(D(1) - yes_price)
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    journal.save()
    reconcile_exit(client, journal, record, START + 574)
    assert D(record["stop_armed"]["threshold"]) == threshold
    assert record["sells"][0]["purpose"] == "stop_loss"
    assert client.submissions == [
        {"ticker": client.ticker, "signed": D(expected_sign),
         "limit": threshold, "client_id": record["sells"][0]["client_id"]}
    ]
    assert Journal(journal.path).data["windows"][client.ticker]["stop_armed"]
    # Unknown exit status cannot send a second order.
    reconcile_exit(client, journal, record, START + 575)
    assert len(client.submissions) == 1


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_gtc_stop_is_triggered_then_resting_and_repriced_only_after_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("M9_STOP_ORDER_MODE", "gtc")
    journal = Journal(tmp_path / "gtc-stop.json")
    client = StopExchange(bid=".44")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    journal.save()
    reconcile_exit(client, journal, record, START + 570)
    assert record["sells"][0]["purpose"] == "stop_loss"
    assert record["sells"][0]["execution"] == "reduce_only_gtc"
    assert client.orders["exit-1"]["status"] == "resting"
    assert client.submissions[0]["limit"] == D(".44")

    # A resting stop remains the sole owned sell while the bid is unchanged.
    reconcile_exit(client, journal, record, START + 571)
    assert len(client.submissions) == 1
    assert client.orders["exit-1"]["status"] == "resting"

    # If the bid drops, first cancel the old order, then verify terminal
    # status and exact zero fills before submitting a lower GTC stop.
    client.bid = D(".40")
    reconcile_exit(client, journal, record, START + 572)
    assert client.orders["exit-1"]["status"] == "canceled"
    assert len(client.submissions) == 1
    reconcile_exit(client, journal, record, START + 573)
    assert len(client.submissions) == 2
    assert client.submissions[-1]["limit"] == D(".40")
    assert record["sells"][1]["execution"] == "reduce_only_gtc"
    assert client.orders["exit-2"]["status"] == "resting"


def test_gtc_stop_no_order_before_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("M9_STOP_ORDER_MODE", "gtc")
    journal = Journal(tmp_path / "gtc-not-triggered.json")
    client = StopExchange(bid=".65")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    reconcile_exit(client, journal, record, START + 570)
    assert record["sells"][0]["purpose"] == "take_profit"
    assert record["sells"][0]["execution"] == "gtc"
    assert "stop_armed" not in record


def test_partial_stop_fill_retries_only_remainder_even_after_quote_recovers(tmp_path, side):
    journal = Journal(tmp_path / f"partial-{side}.json")
    client = StopExchange(side=side, bid=".44")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    reconcile_exit(client, journal, record, START + 571)
    client.simulate_partial_fill("1", ".44")
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
    client = StopExchange(bid=".44")
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


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("bid", [".72", ".61", ".52", ".515"])
def test_stop_not_sent_before_threshold_and_tp_unchanged(tmp_path, side, bid):
    journal = Journal(tmp_path / "not_yet.json")
    client = StopExchange(side=side, bid=bid)
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    reconcile_exit(client, journal, record, START + 570)
    assert len(client.submissions) == 1 and "stop_armed" not in record
    client.bid = D(".77")
    reconcile_exit(client, journal, record, START + 571)
    assert len(client.submissions) == 1
    assert record["sells"][0]["purpose"] == "take_profit"
    assert client.submissions[0]["limit"] == D(".77")


def test_stop_execution_realized_loss_resets_win_streak(tmp_path):
    journal = Journal(tmp_path / "realized-loss.json")
    client = FakeTradeClient(fully_sold=True, fees=".01")
    # A stop exit at 44c following a 72c entry loses 28c/contract.
    client.rows[-1]["yes_price_dollars"] = ".44"
    client.rows[-1]["no_price_dollars"] = ".56"
    record = fake_record(client)
    record["stop_armed"] = {"at": START + 565, "threshold": ".45", "trigger_bid": ".44"}
    record["sells"][0]["purpose"] = "stop_loss"
    journal.data["windows"][client.ticker] = record
    for _ in range(3):
        journal.data["streak"] = next_streak(journal.data["streak"], "win")
    verify_completed_trade(client, journal, record, START + 585)
    assert record["outcome"]["status"] == "loss"
    apply_new_outcomes(journal)
    assert journal.data["streak"] == next_streak(starting_streak(), "loss")


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

    def all_fills(self, ticker):
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
    assert j.data["windows"][bot.posted[0][0]]["quantity"] == "8"  # one-third starting allocation
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


@pytest.mark.parametrize("foreign_kind", ["long", "short", "resting", "past_fill"])
def test_external_market_is_untouched_and_free_coin_still_enters(tmp_path, monkeypatch, foreign_kind):
    import single_coin_minute9 as mod
    class MixedAccount(PreviewCoinClient):
        def positions(self, ticker):
            if ticker.startswith("KXBTC15M-") and foreign_kind in {"long", "short"}:
                return [{"ticker": ticker, "position_fp": "2" if foreign_kind == "long" else "-2"}]
            return []

        def all_orders(self, ticker, status=None):
            return [{"order_id": "manual-order"}] if ticker.startswith("KXBTC15M-") and foreign_kind == "resting" else []

        def all_fills(self, ticker):
            return [{"order_id": "manual-fill"}] if ticker.startswith("KXBTC15M-") and foreign_kind == "past_fill" else []

        def cancel(self, *args, **kwargs):
            pytest.fail("Outside orders must never be canceled")

        def place_take_profit(self, *args, **kwargs):
            pytest.fail("Outside inventory must never receive bot exits")

    client = MixedAccount()
    journal = Journal(tmp_path / "mixed.json")
    monkeypatch.setattr(mod.time, "time", lambda: START + 550)
    run_pass(client, journal, client.series, D("20"), 100, True, START + 550)
    assert len(client.posted) == 1
    assert client.posted[0][0].startswith("KXETH15M-")
    assert list(journal.data["windows"]) == [client.posted[0][0]]


def test_external_position_appearing_during_quote_blocks_entry(tmp_path, monkeypatch):
    import single_coin_minute9 as mod
    class LateManualPosition(PreviewCoinClient):
        occupied = False

        def market(self, ticker):
            self.occupied = True
            return super().market(ticker)

        def positions(self, ticker):
            return [{"ticker": ticker, "position_fp": "2"}] if self.occupied else []

    client = LateManualPosition()
    journal = Journal(tmp_path / "late-manual.json")
    monkeypatch.setattr(mod.time, "time", lambda: START + 550)
    run_pass(client, journal, ("KXBTC15M",), D("20"), 100, True, START + 550)
    assert not client.posted and not journal.data["windows"]


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_foreign_fills_block_exits_even_when_net_quantity_matches(tmp_path, side):
    journal = Journal(tmp_path / "foreign-fill.json")
    client = StopExchange(side=side, bid=".45")
    client.rows.append(dict(client.rows[0], order_id="outside-order"))
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    reconcile_exit(client, journal, record, START + 574)
    assert not client.submissions and not record["sells"]
    assert "stop_armed" not in record


@pytest.mark.parametrize("field,value", [
    ("client_order_id", "outside-client"), ("client_order_id", None),
    ("order_id", "outside-order"), ("ticker", "OTHER-MARKET"),
])
def test_saved_order_must_match_bot_ownership(tmp_path, field, value):
    journal = Journal(tmp_path / "ownership.json")
    client = StopExchange(bid=".45")
    client.orders["entry"][field] = value
    record = fake_record(client, fully_sold=False)
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        reconcile_exit(client, journal, record, START + 574)
    assert not client.submissions


def test_partial_entry_read_model_cannot_exit_incomplete_quantity(tmp_path):
    journal = Journal(tmp_path / "lagged-entry.json")
    client = StopExchange(bid=".45")
    client.orders["entry"]["fill_count_fp"] = "3"
    record = fake_record(client, fully_sold=False)
    record["quantity"] = "3"
    reconcile_exit(client, journal, record, START + 574)
    assert not client.submissions


@pytest.mark.parametrize("bid", [".45", ".77"])
def test_preview_never_exits_positions_from_existing_journal(tmp_path, bid):
    journal = Journal(tmp_path / "existing-live.json")
    client = StopExchange(bid=bid)
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    journal.save()
    before = journal.path.read_text()
    run_pass(client, journal, (), D("20"), 100, False, START + 574)
    assert not client.submissions
    assert journal.path.read_text() == before


def test_outside_trade_results_do_not_affect_win_streak(tmp_path):
    journal = Journal(tmp_path / "outside-results.json")
    client = FakeTradeClient(fully_sold=True)
    client.rows.append(dict(client.rows[-1], order_id="manual-sale"))
    record = fake_record(client)
    journal.data["windows"][client.ticker] = record
    verify_completed_trade(client, journal, record, START + 585)
    apply_new_outcomes(journal)
    assert "outcome" not in record
    assert journal.data["streak"] == starting_streak()
