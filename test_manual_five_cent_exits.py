"""Offline exchange lifecycle tests for fixed per-fill +5c targets."""
import copy
import json
import subprocess
import sys
import tomllib
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from kalshi import KalshiClient
from manual_limit_exits import ManualLimitExitWorker, ManualExitBlocked
from manual_exit_runner import ExitOnlyClient, ManualExitService, Settings
from test_manual_limit_exits import Exchange, T, auto_buy


def setup(tmp_path, *, dry_run=False, series=None, **env):
    e = Exchange()
    state = tmp_path / "state.json"
    state.write_text('{"markets": {}}')
    automatic = tmp_path / "state_take_profit.json"
    automatic.write_text('{"markets": {}}')
    config = Settings.read({"STATE_PATH": str(state), "MANUAL_EXIT_MODE": "monitored_reduce_only", **env})
    config = replace(config, dry_run=dry_run, series=series or config.series)
    events = []
    service = ManualExitService(e, config, clock=e.clock,
        emit=lambda event, **data: events.append((event, data)))
    return e, service, config, events


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("count", ["1", "10", "2.75"])
def test_five_cent_increment_is_per_contract_not_total_dollars(tmp_path, sign, count):
    e, s, _, _ = setup(tmp_path)
    e.buy(count, ".42", sign)
    e.bid = e.no_bid = D(".46")
    assert s.run_once() and not e.sent
    assert s.worker._plans[T][0]["raw_target"] == D(".47")
    e.bid = e.no_bid = D(".47")
    assert s.run_once() and s.run_once()
    assert e.held[T] == 0 and len(e.sent) == 1
    assert e.sent[0]["count"] == count
    assert e.sent[0]["price"] == ("0.4700" if sign == 1 else "0.5300")
    assert e.sent[0]["reduce_only"] is True


def test_later_partial_buy_keeps_each_fill_price(tmp_path):
    e, s, _, _ = setup(tmp_path)
    oid = e.make_order(count=10, price=".42")
    first = e.fill(oid, 4, ".40")
    assert s.run_once()
    second = e.fill(oid, 6, ".42")
    assert s.run_once()
    assert {p["fill_id"]: p["raw_target"] for p in s.worker._plans[T]} == {
        first: D(".45"), second: D(".47")}
    e.bid = D(".45")
    assert s.run_once() and s.run_once()
    assert e.held[T] == 6 and len(e.sent) == 1
    e.bid = D(".47")
    assert s.run_once() and s.run_once()
    assert e.held[T] == 0 and len(e.sent) == 2


def test_better_partial_exit_does_not_lower_remaining_target(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.liquidity, e.bid = D(2), D(".60")
    assert s.run_once()
    e.bid = D(".46")
    assert s.run_once()
    assert e.held[T] == 8 and len(e.sent) == 1
    assert s.worker._plans[T][0]["raw_target"] == D(".47")


def test_manual_partial_sale_reduces_quantity_not_target(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    sale = e.make_order(count=3, price=".43", action="sell")
    e.fill(sale, 3)
    assert s.run_once()
    plan = s.worker._plans[T][0]
    assert plan["quantity"] == 7 and plan["raw_target"] == D(".47")
    e.bid = D(".47")
    assert s.run_once() and s.run_once()
    assert e.held[T] == 0 and e.sent[0]["count"] == "7"


def test_restart_and_duplicate_history_do_not_create_duplicate_sell(tmp_path):
    e, s, config, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.bid, e.lose_ack = D(".47"), True
    assert not s.run_once() and len(e.sent) == 1
    e.lose_ack = False
    e.history.append(copy.deepcopy(e.history[0]))
    recovered = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **k: None)
    assert recovered.run_once()
    assert e.held[T] == 0 and len(e.sent) == 1


@pytest.mark.parametrize("price,target,submits", [(".94", ".99", True), (".95", "1.00", False), (".96", "1.01", False)])
def test_unreachable_increment_is_never_silently_capped(tmp_path, price, target, submits):
    e, s, _, events = setup(tmp_path)
    e.buy(1, price)
    e.bid = D(".99")
    assert s.run_once()
    assert bool(e.sent) == submits
    if not submits:
        assert s.worker._plans[T][0]["raw_target"] == D(target)
        assert any(event == "MANUAL_TARGET_UNREACHABLE" for event, _ in events)


@pytest.mark.parametrize("sign", [1, -1])
def test_rounding_uses_exchange_grid_without_undercutting_five_cents(tmp_path, sign):
    e, s, _, _ = setup(tmp_path)
    e.buy("1.25", ".425", sign)
    e.price_ranges = [{"start": "0", "end": "1", "step": ".001"}]
    e.bid = e.no_bid = D(".475")
    assert s.run_once()
    assert e.sent[0]["price"] == ("0.4750" if sign == 1 else "0.5250")


def test_policy_cannot_reinterpret_old_total_profit_journal(tmp_path):
    e = Exchange()
    path = tmp_path / "manual.json"
    ManualLimitExitWorker(e, path, clock=e.clock)
    with pytest.raises(ManualExitBlocked, match="target policy"):
        ManualLimitExitWorker(e, path, clock=e.clock, price_increment=D(".05"))


def test_equivalent_increment_spelling_can_resume_same_journal(tmp_path):
    e = Exchange()
    path = tmp_path / "manual.json"
    ManualLimitExitWorker(e, path, clock=e.clock, price_increment=D(".050"))
    ManualLimitExitWorker(e, path, clock=e.clock, price_increment=D(".05"))


@pytest.mark.parametrize("ticker", [T, "KXETH15M-TEST"])
def test_exit_only_runner_covers_bitcoin_and_ether_defaults(tmp_path, ticker):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42", ticker=ticker)
    e.bid = D(".47")
    assert s.run_once() and s.run_once()
    assert e.held[ticker] == 0


def test_runner_does_not_adopt_other_markets_or_pre_activation_fills(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.clock_value -= 10
    e.buy(10, ".42")
    e.clock_value += 10
    e.buy(10, ".42", ticker="KXOTHER-TEST")
    e.bid = D(".99")
    assert s.run_once() and not e.sent


def test_runner_preserves_automated_inventory_and_both_old_journals(tmp_path):
    e, s, config, _ = setup(tmp_path)
    state = {"markets": {}}
    auto_buy(e, state)
    config.entry_state.write_text(json.dumps(state))
    before = {p: p.read_bytes() for p in (config.entry_state, config.automatic_exit_state)}
    e.buy(3, ".42")
    e.bid = D(".60")  # Even above the automatic target, only the manual lot exits.
    assert s.run_once() and s.run_once()
    assert e.held[T] == 10 and len(e.sent) == 1 and e.sent[0]["count"] == "3"
    assert all(p.read_bytes() == data for p, data in before.items())


def test_runner_stops_when_automatic_exit_acknowledgement_is_unknown(tmp_path):
    e, s, config, _ = setup(tmp_path)
    config.automatic_exit_state.write_text(json.dumps({"markets": {T: {"pending": {"client_id": "unknown"}}}}))
    e.buy(10, ".42")
    e.bid = D(".47")
    assert not s.run_once() and not e.sent


def test_runner_waits_for_exit_fill_history_after_ack(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.liquidity, e.bid = D(3), D(".47")
    assert s.run_once() and len(e.sent) == 1
    e.hidden_fills.add(e.history[-1]["fill_id"])
    assert not s.run_once() and len(e.sent) == 1
    e.hidden_fills.clear()
    assert s.run_once() and len(e.sent) == 2


def test_runner_does_not_compete_with_users_resting_sell(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.make_order(count=10, price=".60", action="sell")
    e.bid = D(".47")
    assert not s.run_once() and not e.sent and not e.cancelled


def test_dry_run_never_submits_at_target(tmp_path):
    e, s, _, events = setup(tmp_path, dry_run=True)
    e.buy(10, ".42")
    e.bid = D(".47")
    assert s.run_once() and not e.sent and e.held[T] == 10
    assert any(event == "MANUAL_DRY_RUN_TARGET" and data["target"] == "0.47" for event, data in events)


@pytest.mark.parametrize("env", [{}, {"MANUAL_EXITS_ENABLED": "true", "MANUAL_EXITS_DRY_RUN": "false", "MANUAL_EXTERNAL_ORDERS_ARE_MANUAL": "true"}])
def test_command_without_live_flag_is_always_dry_run(env):
    assert Settings.read(env).dry_run


@pytest.mark.parametrize("env", [
    {"TRADING_ENABLED": "true"}, {"MANUAL_EXIT_MODE": "resting"},
    {"MANUAL_EXIT_INCREMENT_CENTS": "0"}, {"MANUAL_EXIT_INCREMENT_CENTS": "NaN"},
    {"MANUAL_EXIT_INCREMENT_CENTS": "100"}, {"MANUAL_EXIT_INCREMENT_CENTS": "5.001"},
    {"MANUAL_EXIT_SERIES": "*"}, {"MANUAL_EXIT_SERIES": ""}, {"MANUAL_EXIT_SERIES": "KXBTC15M,KXBTC15M"},
    {"MANUAL_EXIT_POLL_SECONDS": "0"}, {"KALSHI_ENV": "demo"},
    {"STATE_PATH": "/tmp/x.json", "MANUAL_EXIT_STATE_PATH": "/tmp/x.json"},
])
def test_invalid_settings_fail_closed(env):
    with pytest.raises(ValueError):
        Settings.read(env)


def test_live_configuration_requires_every_explicit_gate():
    gates = {"MANUAL_EXITS_ENABLED": "true", "MANUAL_EXITS_DRY_RUN": "false", "MANUAL_EXTERNAL_ORDERS_ARE_MANUAL": "true"}
    assert not Settings.read(gates, live=True).dry_run
    for name in gates:
        with pytest.raises(ValueError):
            Settings.read({k: v for k, v in gates.items() if k != name}, live=True)


@pytest.mark.parametrize("dry_run", [True, False])
def test_client_blocks_entries_transfers_and_cancellations(monkeypatch, dry_run):
    calls = []
    monkeypatch.setattr(KalshiClient, "request", lambda *a, **k: calls.append((a, k)))
    client = ExitOnlyClient(dry_run=dry_run)
    client.request("GET", "/portfolio/positions")
    assert len(calls) == 1
    for method, path, body in [
        ("POST", "/portfolio/events/orders", {"reduce_only": False, "time_in_force": "immediate_or_cancel"}),
        ("POST", "/portfolio/events/orders", {"reduce_only": True, "time_in_force": "good_till_canceled"}),
        ("POST", "/portfolio/transfers", {}), ("DELETE", "/portfolio/events/orders/x", {}),
    ]:
        with pytest.raises(RuntimeError):
            client.request(method, path, body=body)
    protected = {"reduce_only": True, "time_in_force": "immediate_or_cancel"}
    if dry_run:
        with pytest.raises(RuntimeError):
            client.request("POST", "/portfolio/events/orders", body=protected)
    else:
        client.request("POST", "/portfolio/events/orders", body=protected)
    assert len(calls) == (1 if dry_run else 2)


def test_missing_ownership_journal_does_not_create_new_activation(tmp_path):
    config = Settings.read({"STATE_PATH": str(tmp_path / "missing.json")})
    with pytest.raises(FileNotFoundError):
        ManualExitService(Exchange(), config)
    assert not config.manual_state.exists()


def test_dry_run_and_live_have_distinct_default_activation_journals():
    env = {"MANUAL_EXITS_ENABLED": "true", "MANUAL_EXITS_DRY_RUN": "false", "MANUAL_EXTERNAL_ORDERS_ARE_MANUAL": "true"}
    preview, live = Settings.read(env), Settings.read(env, live=True)
    assert preview.manual_state != live.manual_state


def test_cannot_reuse_preview_journal_for_live_trading(tmp_path):
    e, _, config, _ = setup(tmp_path, dry_run=True)
    with pytest.raises(ManualExitBlocked, match="separate journals"):
        ManualExitService(e, replace(config, dry_run=False), clock=e.clock)


def test_closed_market_is_retired_without_assuming_sale(tmp_path):
    e, s, _, events = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    e.clock_value = e.close + 1
    assert s.run_once() and not e.sent and e.held[T] == 10
    assert s.worker.ledger(T)["window_closed"]
    reads = len(e.reads)
    assert s.run_once() and len(e.reads) == reads
    assert any(event == "MANUAL_WINDOW_CLOSED" for event, _ in events)


@pytest.mark.parametrize("manifest", ["Dockerfile", "railway.toml"])
def test_default_deployment_cannot_import_legacy_strategy_or_enable_live_mode(manifest):
    root = Path(__file__).resolve().parent
    if manifest == "Dockerfile":
        line = next(line for line in (root / manifest).read_text().splitlines() if line.startswith("CMD "))
        command = json.loads(line[4:])
    else:
        config = tomllib.loads((root / manifest).read_text())
        assert config["build"]["builder"] == "DOCKERFILE"
        assert "startCommand" not in config["deploy"], "Do not shadow the operator's Railway startup selection"
        line = next(line for line in (root / "Dockerfile").read_text().splitlines() if line.startswith("CMD "))
        command = json.loads(line[4:])
    assert command == ["python", "manual_exit_runner.py"]
    # Exercise the actual startup in a clean interpreter; old modules cannot
    # hide in pytest's import cache. No credentials, dotenv loading or network.
    harness = '''
import builtins, runpy, socket, sys
import dotenv
dotenv.load_dotenv = lambda *a, **k: None
original_import = builtins.__import__
def guarded_import(name, *a, **k):
    if name.split(".")[0] in {"bot", "hybrid_runtime"}:
        raise AssertionError("Manual-only startup imported the legacy strategy")
    return original_import(name, *a, **k)
builtins.__import__ = guarded_import
def offline(*a, **k):
    raise AssertionError("Startup attempted network access")
socket.socket.connect = socket.create_connection = offline
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-c", harness, *command[1:], "--check-config"],
        cwd=root, env={
            "TRADING_ENABLED": "false",
            "MODE": "live", "EXECUTION_STRATEGY": "strike_ruler",
            "DUAL_LIMIT_BUYS_ENABLED": "true", "OPENING_BIAS_ENABLED": "true",
            "HISTORICAL_STRIKE_ENABLED": "true", "PER_ORDER_PROFIT_DOLLARS": "0.20",
            "MANUAL_EXITS_ENABLED": "true", "MANUAL_EXITS_DRY_RUN": "false",
            "MANUAL_EXTERNAL_ORDERS_ARE_MANUAL": "true",
        }, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["automatic_entries"] is False and report["dry_run"] is True
    assert D(report["increment_cents"]) == 5
    assert report["series"] == ["KXBTC15M", "KXETH15M"]
    assert report["mode"] == "resting_limit"


def test_sample_environment_matches_manual_only_preview_defaults():
    from dotenv import dotenv_values
    env = dotenv_values(Path(__file__).with_name(".env.example"))
    settings = Settings.read(env)
    assert settings.dry_run and settings.increment == D(".05")
    assert settings.series == ("KXBTC15M", "KXETH15M")
    assert settings.execution_mode == "resting_limit"
    assert env["TRADING_ENABLED"] == "false"
    assert not {"EXECUTION_STRATEGY", "ENTRY_EXIT_PAIRS_CENTS", "PER_ORDER_PROFIT_DOLLARS"} & env.keys()
