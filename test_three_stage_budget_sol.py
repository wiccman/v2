"""Tests for the new three-stage Zone 1 sizing and SOL half budget."""
from decimal import Decimal as D

import pytest
import single_coin_minute9 as core
import late_basket_minute9 as late
from test_late_basket_minute9 import Feed, scenario as late_scenario
from test_cash_checked_minute9 import CashExchange
from test_single_coin_minute9 import START


def cycle(statuses):
    current = core.new_budget_cycle(START)
    for result in statuses:
        current = core.advance_budget_cycle(current, result)
    return current


def active(cycle_state):
    return core.trade_budget(core.starting_streak(), D("6.66"),
                             budget_cycle=cycle_state)


@pytest.mark.parametrize("outcomes,phase,wins,budget", [
    ([], "full", 0, "6.66"),
    (["win"], "full", 1, "6.66"),
    (["win", "win"], "full", 2, "6.66"),
    (["win", "win", "win"], "third", 0, "2.22"),
    (["win", "loss", "win"], "full", 1, "6.66"),
    (["win", "win", "win", "win", "win"], "third", 0, "2.22"),
    (["win", "win", "win", "loss"], "half", 0, "3.33"),
    (["win", "win", "win", "loss", "win"], "half", 1, "3.33"),
    (["win", "win", "win", "loss", "win", "win"], "full", 0, "6.66"),
    (["win", "win", "win", "loss", "win", "loss"], "half", 0, "3.33"),
    (["loss"], "full", 0, "6.66"),
    (["flat"], "full", 0, "6.66"),
])
def test_zone1_budget_stages(outcomes, phase, wins, budget):
    state = cycle(outcomes)
    assert state["phase"] == phase
    assert state["phase_wins"] == wins
    assert active(state) == D(budget)


def test_full_complete_cycle_and_repeat():
    state = cycle(["win"] * 3 + ["loss"] + ["win"] * 2 + ["win"] * 3)
    assert state["phase"] == "third"
    assert state["phase_wins"] == 0


@pytest.mark.parametrize("phase,amount,sol", [
    ("full", "6.66", "3.33"),
    ("third", "2.22", "1.11"),
    ("half", "3.33", "1.665"),
])
def test_zone1_sol_is_always_half_of_stage(phase, amount, sol):
    state = core.new_budget_cycle(START)
    state["phase"] = phase
    budget = active(state)
    assert budget == D(amount)
    assert core.budget_for_series(budget, "KXSOL15M") == D(sol)
    assert core.budget_for_series(budget, "KXETH15M") == budget
    assert core.budget_for_series(budget, "KXXRP15M") == budget
    assert core.budget_for_series(budget, "KXHYPE15M") == budget


def test_new_budget_cycle_ignores_old_trades_and_recovers_late_results(tmp_path):
    j = core.Journal(tmp_path / "policy.json")
    old = {"ticker": "KXETH15M-OLD", "buy": {"created_at": START - 600},
           "outcome": {"status": "win", "completed_at": START + 200}}
    j.data["windows"]["old"] = old
    core.ensure_budget_cycle(j, now=START)
    assert j.data["budget_cycle_v2"]["phase"] == "full"
    assert j.data["budget_cycle_v2"]["phase_wins"] == 0
    # These three results can reconcile in any order, without corrupting stage.
    for i, completed in [(3, 300), (1, 100), (2, 200)]:
        j.data["windows"][str(i)] = {
            "ticker": f"KXETH15M-{i}", "buy": {"created_at": START + 1},
            "outcome": {"status": "win", "completed_at": START + completed}}
        core.rebuild_budget_cycle(j)
    assert j.data["budget_cycle_v2"]["phase"] == "third"
    assert core.Journal(j.path).data["budget_cycle_v2"] == j.data["budget_cycle_v2"]
    # One late Zone 2 loss never changes Zone 1 phase.
    j.data["windows"]["late"] = {
        "ticker": "KXSOL15M-LATE", "entry_kind": "late_basket_rank",
        "buy": {"created_at": START + 200}, "outcome": {
            "status": "loss", "completed_at": START + 400}}
    core.rebuild_budget_cycle(j)
    assert j.data["budget_cycle_v2"]["phase"] == "third"
    # First new regular loss after moving to one-third enters half.
    j.data["windows"]["new-loss"] = {
        "ticker": "KXXRP15M-LOSS", "buy": {"created_at": START + 220},
        "outcome": {"status": "loss", "completed_at": START + 450}}
    core.rebuild_budget_cycle(j)
    assert j.data["budget_cycle_v2"]["phase"] == "half"
    # Idempotent at restart; no count duplicates.
    assert core.ensure_budget_cycle(core.Journal(j.path), now=START + 999)["phase"] == "half"


def test_no_fill_and_pre_policy_recovery_not_counted(tmp_path):
    j = core.Journal(tmp_path / "no_fill.json")
    core.ensure_budget_cycle(j, now=START)
    j.data["windows"]["no"] = {
        "ticker": "KXETH15M-NO", "buy": {"created_at": START + 1},
        "outcome": {"status": "no_fill", "completed_at": START + 10}}
    j.data["windows"]["prior"] = {
        "ticker": "KXXRP15M-PRIOR", "buy": {"created_at": START - 10},
        "outcome": {"status": "loss", "completed_at": START + 20}}
    core.rebuild_budget_cycle(j)
    assert j.data["budget_cycle_v2"] == core.new_budget_cycle(START)


def test_zone2_sol_full_stage_half_of_zone2_half(tmp_path, monkeypatch):
    # Force SOL as the only eligible top-two coin (BTC first), and set
    # half allocation relative to Zone 2 as well as relative to Zone 1.
    from test_late_basket_minute9 import Feed
    from test_single_coin_minute9 import START
    now = [START + 730]
    monkeypatch.setattr(core.time, "time", lambda: now[0])
    journal = core.Journal(tmp_path / "zone2-sol.json")
    core.ensure_budget_cycle(journal, now=START + 600)
    client = CashExchange()
    client.initial = {s: ".91" for s in core.DEFAULT_SERIES}
    client.fresh = dict(client.initial)
    feed = Feed(now, ["KXBTC15M", "KXSOL15M", "KXETH15M", "KXHYPE15M", "KXXRP15M"])
    late.run_late_pass(client, journal, core.DEFAULT_SERIES,
                       D("6.66"), 100, True, now[0], provider=feed,
                       clock=lambda: now[0], sol_blocked=lambda _: False)
    assert len(client.posted) == 1
    assert client.posted[0][0].startswith("KXSOL15M-")
    assert client.posted[0][2] == 1
    assert late.half_budget_reservation(journal, START) <= D("1.665")


@pytest.mark.parametrize("phase", ["third", "half"])
def test_zone2_sol_skips_if_halved_late_budget_cannot_buy_one(tmp_path, monkeypatch, phase):
    from test_late_basket_minute9 import Feed
    now = [START + 730]
    monkeypatch.setattr(core.time, "time", lambda: now[0])
    journal = core.Journal(tmp_path / (phase+".json"))
    core.ensure_budget_cycle(journal, now=START + 600)
    journal.data["budget_cycle_v2"]["phase"] = phase
    journal.data["budget_cycle_v2"]["phase_wins"] = 0
    journal.save()
    client = CashExchange()
    client.initial = {s: ".91" for s in core.DEFAULT_SERIES}
    client.fresh = dict(client.initial)
    feed = Feed(now, ["KXBTC15M", "KXSOL15M", "KXETH15M", "KXHYPE15M", "KXXRP15M"])
    late.run_late_pass(client, journal, core.DEFAULT_SERIES,
                       D("6.66"), 100, True, now[0], provider=feed,
                       clock=lambda: now[0], sol_blocked=lambda _: False)
    assert client.posted == []


def test_sol_zone2_budget_cap_protects_against_repeated_attempts(tmp_path, monkeypatch):
    from test_late_basket_minute9 import Feed
    now = [START + 730]
    monkeypatch.setattr(core.time, "time", lambda: now[0])
    journal = core.Journal(tmp_path / "repeat.json")
    core.ensure_budget_cycle(journal, now=START + 600)
    client = CashExchange()
    client.initial = {s: ".91" for s in core.DEFAULT_SERIES}
    client.fresh = dict(client.initial)
    feed = Feed(now, ["KXBTC15M", "KXSOL15M", "KXETH15M", "KXHYPE15M", "KXXRP15M"])
    for i in range(3):
        now[0] += 1
        late.run_late_pass(client, core.Journal(journal.path), core.DEFAULT_SERIES,
                           D("6.66"), 100, True, now[0], provider=feed,
                           clock=lambda: now[0], sol_blocked=lambda _: False)
    assert len(client.posted) == 1
