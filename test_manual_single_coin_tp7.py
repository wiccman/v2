"""Offline exchange lifecycle checks for the requested manual +7c profile."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest
from dotenv import dotenv_values

from manual_exit_runner import ManualExitService, Settings
from test_resting_manual_exits import RestingExchange

PROFILE = Path(__file__).with_name("manual_single_coin_tp7.preview.env")
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M")


def setup(tmp_path, *, dry_run=False):
    env = dict(dotenv_values(PROFILE))
    entry = tmp_path / "entry.json"
    auto = tmp_path / "automatic_exit.json"
    entry.write_text('{"markets": {}}')
    auto.write_text('{"markets": {}}')
    env.update(STATE_PATH=str(entry), AUTOMATIC_EXIT_STATE_PATH=str(auto),
               MANUAL_EXIT_STATE_PATH=str(tmp_path / "manual.json"))
    settings = replace(Settings.read(env), dry_run=dry_run)
    exchange, events = RestingExchange(), []
    service = ManualExitService(exchange, settings, clock=exchange.clock,
        emit=lambda event, **data: events.append((event, data)))
    return exchange, service, settings, events


def test_profile_remains_read_only_and_rejects_live_activation():
    env = dict(dotenv_values(PROFILE))
    settings = Settings.read(env)
    assert settings.dry_run and settings.increment == D(".07")
    assert settings.series == SERIES and settings.stop_loss_cents == 0
    assert settings.respect_manual_edits
    with pytest.raises(ValueError, match="Live mode requires"):
        Settings.read(env, live=True)


@pytest.mark.parametrize("series", SERIES)
@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("order_type", ["limit", "market"])
def test_manual_fill_posts_seven_cent_tp_and_restart_does_not_duplicate(
        tmp_path, series, sign, order_type):
    e, service, settings, events = setup(tmp_path)
    ticker = series + "-TEST"
    buy = e.buy("2.75", ".40", sign, ticker=ticker)
    e.remote[buy]["type"] = order_type
    assert service.run_once(), events
    assert len(e.sent) == 1
    sell = e.sent[0]
    assert sell["ticker"] == ticker and sell["count"] == "2.75"
    assert sell["price"] == ("0.4700" if sign == 1 else "0.5300")
    assert sell["time_in_force"] == "good_till_canceled"
    assert sell["expiration_time"] == int(e.close)
    restarted = ManualExitService(e, settings, clock=e.clock, emit=lambda *a, **k: None)
    assert restarted.run_once() and len(e.sent) == 1 and not e.cancelled


def test_partial_fills_use_each_execution_price_and_only_filled_quantity(tmp_path):
    e, service, _, events = setup(tmp_path)
    buy = e.make_order(count=10, price=".43")
    e.fill(buy, 4, ".40")
    e.fill(buy, 2, ".42")
    assert service.run_once(), events
    assert [(o["count"], o["price"]) for o in e.sent] == [("4", "0.4700"), ("2", "0.4900")]
    e.fill(buy, 4, ".43")
    assert service.run_once() and len(e.sent) == 3
    assert e.sent[-1]["count"] == "4" and e.sent[-1]["price"] == "0.5000"


@pytest.mark.parametrize("ticker", ["KXCRYPTOLEAD15M-TEST", "KXWTI15M-TEST", "KXBTC-TEST"])
def test_outside_series_are_not_adopted(tmp_path, ticker):
    e, service, _, events = setup(tmp_path)
    e.buy(ticker=ticker)
    assert not service.worker.allowed(ticker)
    assert service.run_once(), events
    assert not e.sent and not e.cancelled


@pytest.mark.parametrize("entry,expected", [(".92", "0.9900"), (".93", None), (".96", None)])
def test_unreachable_target_is_never_silently_lowered(tmp_path, entry, expected):
    e, service, _, _ = setup(tmp_path)
    e.buy(price=entry)
    service.run_once()
    assert [o["price"] for o in e.sent] == ([] if expected is None else [expected])


def test_read_only_profile_never_submits_even_after_manual_fill(tmp_path):
    e, service, _, events = setup(tmp_path, dry_run=True)
    e.buy(price=".40")
    assert service.run_once(), events
    assert not e.sent and not e.cancelled


def test_lost_ack_remains_reserved_across_restart(tmp_path):
    e, service, settings, _ = setup(tmp_path)
    e.buy(price=".40")
    e.lose_ack = e.hide_post = True
    assert not service.run_once() and len(e.sent) == 1
    restarted = ManualExitService(e, settings, clock=e.clock, emit=lambda *a, **k: None)
    assert not restarted.run_once() and len(e.sent) == 1


def test_existing_manual_sell_covers_inventory_without_duplicate(tmp_path):
    e, service, _, events = setup(tmp_path)
    e.buy(10, ".40")
    e.make_order(count=10, price=".47", action="sell")
    assert service.run_once(), events
    assert not e.sent and not e.cancelled
