from decimal import Decimal as D
import pytest
import single_coin_minute9 as core
from kalshi import KalshiClient
from test_single_coin_minute9 import StopExchange, fake_record, START

@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_resting_before_target_no_duplicates_restart_cancel_race(tmp_path, side):
    c = StopExchange(side=side, bid='.60')
    j = core.Journal(tmp_path/'j.json')
    r = fake_record(c, fully_sold=False)
    j.data['windows'][c.ticker] = r
    core.reconcile_exit(c,j,r,START+570)
    assert len(c.submissions)==1
    assert c.submissions[0]['limit']==D('.77')
    assert r['sells'][0]['execution']=='gtc'
    j = core.Journal(j.path); r = j.data['windows'][c.ticker]
    core.reconcile_exit(c,j,r,START+571)
    assert len(c.submissions)==1
    c.bid=D('.30')
    core.reconcile_exit(c,j,r,START+572)
    assert len(c.submissions)==1 and r['stop_armed']
    # TP fills one contract during cancellation; stop must only sell one.
    c.simulate_partial_fill(quantity='1',price='.77')
    c.bid=D('.60') # Stop remains latched despite price recovery.
    core.reconcile_exit(c,j,r,START+573)
    assert len(c.submissions)==2
    assert abs(c.submissions[-1]['signed'])==1
    assert r['sells'][-1]['purpose']=='stop_loss'

@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("entry", [".72", ".725", ".91"])
def test_resting_tp_cancels_at_exact_21c_drop_then_exits_remainder(tmp_path, side, entry):
    c = StopExchange(side=side, bid=str(D(entry) - D(".21") + D(".01")))
    yes_cost = D(entry) if side == "YES" else D("1") - D(entry)
    c.rows[0]["yes_price_dollars"] = str(yes_cost)
    c.rows[0]["no_price_dollars"] = str(D("1") - yes_cost)
    journal = core.Journal(tmp_path / f"entry-{side}-{entry}.json")
    record = fake_record(c, fully_sold=False)
    journal.data["windows"][c.ticker] = record
    threshold = D(entry) - D(".21")
    core.reconcile_exit(c, journal, record, START + 570)
    assert len(c.submissions) == 1
    assert record["sells"][0]["execution"] == "gtc"
    core.reconcile_exit(c, journal, record, START + 571)
    assert "stop_armed" not in record
    assert c.orders["exit-1"]["status"] == "resting"
    c.bid = threshold
    core.reconcile_exit(c, journal, record, START + 572)
    assert c.orders["exit-1"]["status"] == "canceled"
    assert D(record["stop_armed"]["threshold"]) == threshold
    assert len(c.submissions) == 1  # Wait for cancel and fill receipt.
    journal = core.Journal(journal.path)
    record = journal.data["windows"][c.ticker]
    core.reconcile_exit(c, journal, record, START + 573)
    assert len(c.submissions) == 2
    assert record["sells"][-1]["purpose"] == "stop_loss"
    assert c.submissions[-1]["limit"] == threshold
    assert abs(c.submissions[-1]["signed"]) == 2


@pytest.mark.parametrize('side',['YES','NO'])
def test_lost_resting_ack_never_duplicates(tmp_path,side):
    c=StopExchange(side=side,bid='.60'); c.lost_ack=True
    j=core.Journal(tmp_path/'j.json');r=fake_record(c,fully_sold=False)
    j.data['windows'][c.ticker]=r
    with pytest.raises(TimeoutError): core.reconcile_exit(c,j,r,START+570)
    core.reconcile_exit(c,j,r,START+571)
    assert len(c.submissions)==1

@pytest.mark.parametrize('qty,book,price',[(2,'ask','0.7700'),(-2,'bid','0.2300')])
def test_gtc_wire_payload(qty,book,price):
    c=object.__new__(KalshiClient); sent=[]
    c.request=lambda *a,**k: sent.append(k['body']) or {'order_id':'tp'}
    c.place_resting_take_profit('TEST',qty,D('.77'),12345,client_order_id='id')
    p=sent[0]
    assert p['time_in_force']=='good_till_canceled'
    assert p['side']==book and p['price']==price
    assert p['count']=='2' and p['expiration_time']==12345
    assert p['reduce_only'] is False


def test_cancel_failure_and_lag_never_allow_second_sell(tmp_path):
    c=StopExchange(bid='.60');j=core.Journal(tmp_path/'j.json')
    r=fake_record(c,fully_sold=False);j.data['windows'][c.ticker]=r
    core.reconcile_exit(c,j,r,START+570)
    c.bid=D('.30')
    def failed_cancel(*a): raise TimeoutError('cancel unknown')
    c.cancel=failed_cancel
    with pytest.raises(TimeoutError):core.reconcile_exit(c,j,r,START+571)
    assert r['stop_armed'] and len(c.submissions)==1
    c.cancel=lambda *a: None
    core.reconcile_exit(c,j,r,START+572)
    assert len(c.submissions)==1
    # Terminal receipt knows about a fill before fill history catches up.
    c.orders['exit-1'].update(status='canceled',fill_count_fp='1')
    core.reconcile_exit(c,j,r,START+573)
    assert len(c.submissions)==1



@pytest.mark.parametrize("side", ["YES", "NO"])
def test_observed_mae_survives_recovery_restart_and_emits_for_win(tmp_path, monkeypatch, side):
    events = []
    monkeypatch.setattr(core, "event",
                        lambda name, **fields: events.append((name, fields)))
    client = StopExchange(side=side, bid=".66")  # 72c fill, 6c underwater.
    journal = core.Journal(tmp_path / f"mae-{side}.json")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    journal.save()
    core.reconcile_exit(client, journal, record, START + 570)
    assert len(client.submissions) == 1
    assert D(record["observed_mae"]["mae_cents_lower_bound"]) == D("6")
    client.bid = D(".60")  # Winner temporarily fell 12c underwater.
    core.reconcile_exit(client, journal, record, START + 572)
    assert D(record["observed_mae"]["lowest_bid"]) == D(".60")
    reopened = core.Journal(journal.path)
    record = reopened.data["windows"][client.ticker]
    client.bid = D(".69")  # Price recovers; do NOT reset the low water mark.
    core.reconcile_exit(client, reopened, record, START + 574)
    assert D(record["observed_mae"]["mae_cents_lower_bound"]) == D("12")
    assert len(client.submissions) == 1  # No change to TP order management.
    client.simulate_partial_fill("2", ".77")
    core.verify_completed_trade(client, reopened, record, START + 575)
    assert record["outcome"]["status"] == "win"
    reported = [row for name, row in events if name == "M9_TRADE_MAE"]
    assert len(reported) == 1
    assert D(reported[0]["mae_cents_lower_bound"]) == D(12)
    assert reported[0]["coverage"] == "polled_executable_bid_not_continuous"
    core.verify_completed_trade(client, reopened, record, START + 577)
    assert len([1 for name, _ in events if name == "M9_TRADE_MAE"]) == 1
    assert core.Journal(journal.path).data["windows"][client.ticker]["observed_mae"] == record["observed_mae"]


def test_mae_low_water_tracks_stop_gap_without_changing_stop_exit(tmp_path):
    client = StopExchange(side="YES", bid=".50")
    journal = core.Journal(tmp_path / "mae_stop_gap.json")
    record = fake_record(client, fully_sold=False)
    journal.data["windows"][client.ticker] = record
    core.reconcile_exit(client, journal, record, START + 570)
    assert record["stop_armed"]
    assert record["sells"][0]["purpose"] == "stop_loss"
    assert client.submissions[0]["limit"] == D(".50")
    assert D(record["observed_mae"]["mae_cents_lower_bound"]) == D("22")


def test_no_observed_mae_on_missing_or_invalid_quotes(tmp_path):
    journal = core.Journal(tmp_path / "invalid_mae.json")
    record = {"ticker": "KXETH15M-TEST", "entry_zone": core.ZONE1}
    assert core.observe_trade_mae(journal, record, D(".72"), D("NaN"), START + 570) is False
    assert core.observe_trade_mae(journal, record, D(".72"), D("-0.1"), START + 570) is False
    assert core.observe_trade_mae(journal, record, D(".72"), D(".60"), float("nan")) is False
    assert "observed_mae" not in record


def test_zone2_mae_low_water_has_own_zone_label(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(core, "event", lambda name, **fields: events.append((name, fields)))
    journal = core.Journal(tmp_path / "zone2_mae.json")
    record = {"ticker": "KXETH15M-TEST", "entry_zone": core.ZONE2,
              "entry_kind": "late_basket_rank"}
    assert core.observe_trade_mae(journal, record, ".91", ".85", START + 800)
    assert D(record["observed_mae"]["mae_cents_lower_bound"]) == D(6)
    assert events[-1][1]["entry_zone"] == core.ZONE2



def make_sol_stop_exchange(*, side="YES", bid=".20"):
    c = StopExchange(side=side, bid=bid)
    c.ticker = "KXSOL15M-TEST"
    for order in c.orders.values():
        order["ticker"] = c.ticker
    for row in c.rows:
        row["ticker"] = c.ticker
    return c


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("zone", [core.ZONE1, core.ZONE2])
def test_sol_absolute_20c_emergency_trigger_executes_both_zones(
        tmp_path, monkeypatch, side, zone):
    captured = []
    monkeypatch.setattr(core, "event",
                        lambda name, **fields: captured.append((name, fields)))
    client = make_sol_stop_exchange(side=side, bid=".20")
    journal = core.Journal(tmp_path / f"sol-kill-{zone}-{side}.json")
    record = fake_record(client, fully_sold=False)
    record["series"] = "KXSOL15M"
    record["entry_zone"] = zone
    journal.data["windows"][core.zone_record_key(client.ticker, zone)] = record
    core.reconcile_exit(client, journal, record, START + 570)
    assert D(record["sol_20c_kill"]["threshold"]) == D(".20")
    assert record["stop_armed"]
    assert record["sells"][0]["purpose"] == "stop_loss"
    assert record["sells"][0]["execution"] == "reduce_only_ioc"
    assert client.submissions[0]["limit"] == D(".20")
    assert client.submissions[0]["signed"] == (D(2) if side == "YES" else D(-2))
    assert len([n for n, _ in captured if n == "M9_SOL_20C_KILL_TRIGGERED"]) == 1
    # Unknown/pending acknowledgement never duplicates a sell or the trigger.
    journal = core.Journal(journal.path)
    record = journal.data["windows"][core.zone_record_key(client.ticker, zone)]
    core.reconcile_exit(client, journal, record, START + 572)
    assert len(client.submissions) == 1
    assert len([n for n, _ in captured if n == "M9_SOL_20C_KILL_TRIGGERED"]) == 1


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_sol_20c_emergency_cancels_existing_resting_tp_before_selling(
        tmp_path, side):
    client = make_sol_stop_exchange(side=side, bid=".60")
    journal = core.Journal(tmp_path / f"sol-tp-cancel-{side}.json")
    record = fake_record(client, fully_sold=False)
    record["series"] = "KXSOL15M"
    journal.data["windows"][client.ticker] = record
    core.reconcile_exit(client, journal, record, START + 570)
    assert len(client.submissions) == 1
    assert record["sells"][0]["execution"] == "gtc"
    assert "sol_20c_kill" not in record
    client.bid = D(".20")
    core.reconcile_exit(client, journal, record, START + 572)
    assert client.orders["exit-1"]["status"] == "canceled"
    assert record["stop_armed"] and record["sol_20c_kill"]
    assert len(client.submissions) == 1
    core.reconcile_exit(client, journal, record, START + 574)
    assert len(client.submissions) == 2
    assert record["sells"][-1]["purpose"] == "stop_loss"
    assert client.submissions[-1]["limit"] == D(".20")


def test_sol_absolute_kill_waits_when_no_executable_bid(tmp_path):
    client = make_sol_stop_exchange(bid="0")
    journal = core.Journal(tmp_path / "sol-zero.json")
    record = fake_record(client, fully_sold=False)
    record["series"] = "KXSOL15M"
    journal.data["windows"][client.ticker] = record
    core.reconcile_exit(client, journal, record, START + 570)
    assert record["sol_20c_kill"]
    assert record["stop_armed"]
    assert client.submissions == []  # No buyer; no fabricated fill or zero-price sell.


def test_sol_marker_does_not_fire_for_eth_even_at_20c(tmp_path):
    c = StopExchange(side="YES", bid=".20")
    c.ticker = "KXETH15M-TEST"
    for order in c.orders.values():
        order["ticker"] = c.ticker
    for row in c.rows:
        row["ticker"] = c.ticker
    j = core.Journal(tmp_path / "eth-stop.json")
    r = fake_record(c, fully_sold=False)
    r["series"] = "KXETH15M"
    j.data["windows"][c.ticker] = r
    core.reconcile_exit(c, j, r, START + 570)
    assert "sol_20c_kill" not in r
    assert r["sells"][0]["purpose"] == "stop_loss"  # Normal 21c entry-relative stop.


def test_sol_marker_exact_20c_boundary(tmp_path):
    c = make_sol_stop_exchange(bid=".201")
    j = core.Journal(tmp_path / "boundary.json")
    r = fake_record(c, fully_sold=False)
    r["series"] = "KXSOL15M"
    j.data["windows"][c.ticker] = r
    assert core.maybe_arm_sol_20c_kill(j, r, D(".201"), START + 570) is False
    assert "sol_20c_kill" not in r
    assert core.maybe_arm_sol_20c_kill(j, r, D(".20"), START + 572) is True
    assert D(r["sol_20c_kill"]["trigger_bid"]) == D(".20")

@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("entry,target", [(".72", ".82"), (".725", ".83"), (".89", ".99")])
def test_new_zone1_rests_at_plus_ten_and_keeps_stop_priority(tmp_path, side, entry, target):
    c = StopExchange(side=side, bid=str(D(entry) - D(".01")))
    yes_price = D(entry) if side == "YES" else D("1") - D(entry)
    c.rows[0]["yes_price_dollars"] = str(yes_price)
    c.rows[0]["no_price_dollars"] = str(D("1") - yes_price)
    j = core.Journal(tmp_path / f"new-ten-{side}-{entry}.json")
    r = fake_record(c, fully_sold=False)
    r["take_profit_spread"] = str(core.PROFIT)
    r["entry_zone"] = core.ZONE1
    j.data["windows"][c.ticker] = r
    j.save()
    core.reconcile_exit(c, j, r, START + 570)
    assert len(c.submissions) == 1
    assert c.submissions[0]["limit"] == D(target)
    assert r["sells"][0]["execution"] == "gtc"
    reopened = core.Journal(j.path)
    r = reopened.data["windows"][c.ticker]
    core.reconcile_exit(c, reopened, r, START + 571)
    assert len(c.submissions) == 1
    c.bid = D(entry) - D(".21")
    core.reconcile_exit(c, reopened, r, START + 572)
    assert r["stop_armed"] and c.orders["exit-1"]["status"] == "canceled"
    assert len(c.submissions) == 1
    core.reconcile_exit(c, reopened, r, START + 573)
    assert len(c.submissions) == 2
    assert r["sells"][-1]["purpose"] == "stop_loss"
    assert r["sells"][-1]["execution"] == "reduce_only_ioc"


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_prechange_zone1_keeps_five_cent_target_even_after_upgrade(tmp_path, side):
    c = StopExchange(side=side, bid=".60")
    j = core.Journal(tmp_path / f"legacy-five-{side}.json")
    r = fake_record(c, fully_sold=False)
    assert "take_profit_spread" not in r
    j.data["windows"][c.ticker] = r
    j.save()
    core.reconcile_exit(c, j, r, START + 570)
    assert c.submissions[0]["limit"] == D(".77")
    assert r["sells"][0]["execution"] == "gtc"


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_five_cent_configuration_preserves_older_ten_cent_regular_positions(
        tmp_path, monkeypatch, side):
    """The new default must not change an existing position's stored target."""
    monkeypatch.setattr(core, "PROFIT", D(".05"))
    for spread, target in [(".05", ".77"), (".10", ".82")]:
        client = StopExchange(side=side, bid=".60")
        journal = core.Journal(tmp_path / f"{side}-{spread}.json")
        record = fake_record(client, fully_sold=False)
        record["entry_zone"] = core.ZONE1
        record["take_profit_spread"] = spread
        journal.data["windows"][client.ticker] = record
        journal.save()
        core.reconcile_exit(client, journal, record, START + 570)
        assert len(client.submissions) == 1
        assert client.submissions[0]["limit"] == D(target)
        assert record["sells"][0]["execution"] == "gtc"
        reopened = core.Journal(journal.path)
        core.reconcile_exit(client, reopened, reopened.data["windows"][client.ticker], START + 571)
        assert len(client.submissions) == 1


def test_five_cent_runtime_configuration_import_and_invalid_guard():
    import os
    import subprocess
    import sys

    for cents, expected in [("5", "0.05"), ("10", "0.10")]:
        env = dict(os.environ, M9_TP_SPREAD_CENTS=cents)
        run = subprocess.run(
            [sys.executable, "-c", "import single_coin_minute9 as m; print(m.PROFIT)"],
            env=env, capture_output=True, text=True, check=True)
        assert run.stdout.strip() == expected
    env = dict(os.environ, M9_TP_SPREAD_CENTS="0")
    bad = subprocess.run(
        [sys.executable, "-c", "import single_coin_minute9"],
        env=env, capture_output=True, text=True)
    assert bad.returncode != 0 and "M9_TP_SPREAD_CENTS" in bad.stderr
