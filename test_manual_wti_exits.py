"""Offline WTI coverage, fixed-point pricing, and journal scope migration."""
import copy
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from manual_exit_runner import ManualExitService, Settings
from test_manual_limit_exits import T as BTC
from test_resting_manual_exits import RestingExchange, exit_ids


ETH = "KXETH15M-TEST"
WTI = "KXWTI15M-26OCT291915-15"
CRYPTO_SERIES = ("KXBTC15M", "KXETH15M")
WTI_SERIES = (*CRYPTO_SERIES, "KXWTI15M")
WTI_PRICE_RANGES = [
    {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
    {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
    {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
]


def setup(tmp_path, *, series=WTI_SERIES):
    exchange = RestingExchange()
    exchange.bid = exchange.no_bid = D(".01")
    exchange.price_ranges = copy.deepcopy(WTI_PRICE_RANGES)
    entry_path = tmp_path / "state.json"
    entry_path.write_text('{"markets": {}}')
    entry_path.with_name("state_take_profit.json").write_text('{"markets": {}}')
    settings = replace(Settings.read({"STATE_PATH": str(entry_path)}),
                       series=series, dry_run=False)
    events = []
    service = ManualExitService(exchange, settings, clock=exchange.clock,
        emit=lambda event, **data: events.append((event, data)))
    return exchange, service, settings, events


@pytest.mark.parametrize("sign", [1, -1])
def test_wti_manual_fill_posts_five_cent_limit_for_fractional_quantity(tmp_path, sign):
    exchange, service, _, _ = setup(tmp_path)
    exchange.buy("2.75", ".42", sign, ticker=WTI)

    assert service.run_once()
    assert len(exchange.sent) == 1
    order = exchange.sent[0]
    assert order["ticker"] == WTI
    assert order["side"] == ("ask" if sign > 0 else "bid")
    assert order["price"] == ("0.4700" if sign > 0 else "0.5300")
    assert order["count"] == "2.75"
    assert order["time_in_force"] == "good_till_canceled"
    assert exchange.held[WTI] == sign * D("2.75")
    assert service.run_once() and len(exchange.sent) == 1

    # The posted order fills on the exchange without another worker invocation.
    exchange.fill(exit_ids(exchange)[0], "2.75", ".47")
    assert exchange.held[WTI] == 0
    assert service.run_once() and len(exchange.sent) == 1


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("fill_price,target", [(".099", ".150"), (".949", ".999")])
def test_wti_tapered_grid_preserves_precision_and_five_cent_minimum(tmp_path, sign, fill_price, target):
    exchange, service, _, _ = setup(tmp_path)
    exchange.buy("1.25", fill_price, sign, ticker=WTI)

    assert service.run_once() and len(exchange.sent) == 1
    order = exchange.sent[0]
    outcome_price = D(order["price"]) if sign > 0 else 1 - D(order["price"])
    assert outcome_price == D(target)
    assert outcome_price - D(fill_price) >= D(".05")
    assert order["count"] == "1.25"


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("fill_price", [".950", ".960"])
def test_wti_unreachable_five_cent_target_is_not_capped_or_submitted(tmp_path, sign, fill_price):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy("1.25", fill_price, sign, ticker=WTI)

    assert service.run_once() and not exchange.sent
    assert exchange.held[WTI] == sign * D("1.25")
    assert any(event == "MANUAL_TARGET_UNREACHABLE" and data["ticker"] == WTI
               for event, data in events)


@pytest.mark.parametrize("excluded", ["KXWTI-TEST", "KXWTI1H-TEST", "KXWTI15MOTHER-TEST"])
def test_wti_allowlist_does_not_adopt_other_oil_series(tmp_path, excluded):
    exchange, service, _, _ = setup(tmp_path)
    exchange.buy("3", ".42", ticker=excluded)
    exchange.buy("2", ".42", ticker=WTI)

    assert service.run_once()
    assert [(order["ticker"], order["count"]) for order in exchange.sent] == [(WTI, "2")]
    assert excluded not in service.worker.state["markets"]


def test_wti_requires_explicit_configured_series(tmp_path):
    assert Settings.read({}).series == CRYPTO_SERIES
    exchange, service, _, _ = setup(tmp_path, series=CRYPTO_SERIES)
    exchange.buy("2", ".42", ticker=WTI)

    assert service.run_once() and not exchange.sent
    assert WTI not in service.worker.state["markets"]


@pytest.mark.parametrize("sign", [1, -1])
def test_wti_partial_buy_fills_keep_their_own_target_and_quantity(tmp_path, sign):
    exchange, service, _, _ = setup(tmp_path)
    buy = exchange.make_order(WTI, sign, "2.75", ".43")
    exchange.fill(buy, "1.25", ".42")
    assert service.run_once() and len(exchange.sent) == 1
    exchange.fill(buy, "1.50", ".43")

    assert service.run_once() and len(exchange.sent) == 2
    targets = [(D(order["count"]), D(order["price"]) if sign > 0 else 1 - D(order["price"]))
               for order in exchange.sent]
    assert targets == [(D("1.25"), D(".47")), (D("1.50"), D(".48"))]
    assert service.run_once() and len(exchange.sent) == 2


@pytest.mark.parametrize("lost_ack", [False, True])
def test_wti_restart_recovers_exit_without_duplicate_submission(tmp_path, lost_ack):
    exchange, service, settings, _ = setup(tmp_path)
    exchange.buy("2.75", ".42", ticker=WTI)
    exchange.lose_ack = lost_ack
    assert service.run_once() is (not lost_ack)
    assert len(exchange.sent) == 1

    exchange.lose_ack = False
    restarted = ManualExitService(exchange, settings, clock=exchange.clock, emit=lambda *a, **k: None)
    assert restarted.run_once()
    assert len(exchange.sent) == 1 and not exchange.cancelled
    assert exchange.held[WTI] == D("2.75")


@pytest.mark.parametrize("legacy_schema", [False, True])
def test_append_wti_preserves_crypto_exits_and_uses_new_series_activation(tmp_path, legacy_schema):
    exchange, service, settings, _ = setup(tmp_path, series=CRYPTO_SERIES)
    exchange.buy("4", ".42", ticker=BTC)
    exchange.buy("2", ".43", ticker=ETH)
    assert service.run_once() and len(exchange.sent) == 2
    crypto_orders = copy.deepcopy(exchange.sent)
    crypto_ledgers = copy.deepcopy(service.worker.state["markets"])
    original_activation = service.worker.state["activation_time"]
    ownership_before = {path: path.read_bytes()
                        for path in (settings.entry_state, settings.automatic_exit_state)}

    # A pre-existing WTI position must not become a new auto-sell on activation.
    old_wti_buy = exchange.buy("3", ".42", ticker=WTI)
    if legacy_schema:
        saved = json.loads(settings.manual_state.read_text())
        saved["schema"] = 2
        saved.pop("series_activation_times", None)
        settings.manual_state.write_text(json.dumps(saved))
    exchange.clock_value += 10
    wti_activation = D(str(exchange.clock_value))
    expanded_settings = replace(settings, series=WTI_SERIES)
    expanded = ManualExitService(exchange, expanded_settings, clock=exchange.clock,
                                 emit=lambda *a, **k: None)

    assert expanded.worker.state["markets"] == crypto_ledgers
    assert expanded.worker.state["activation_time"] == original_activation
    times = expanded.worker.state["series_activation_times"]
    assert all(D(times[series]) == D(original_activation) for series in CRYPTO_SERIES)
    assert D(times["KXWTI15M"]) == wti_activation
    assert expanded.worker.state["schema"] == 3
    assert expanded.run_once() and exchange.sent == crypto_orders

    new_wti_buy = exchange.buy("2.75", ".44", ticker=WTI)
    assert expanded.run_once() and len(exchange.sent) == 3
    assert exchange.sent[-1]["ticker"] == WTI
    assert exchange.sent[-1]["price"] == "0.4900"
    assert exchange.sent[-1]["count"] == "2.75"
    assert {row["order_id"] for row in expanded.worker.ledger(WTI)["managed_fills"].values()} == {new_wti_buy}
    assert old_wti_buy != new_wti_buy and not exchange.cancelled
    assert all(path.read_bytes() == content for path, content in ownership_before.items())

    # A later restart must retain the WTI cutoff and all three resting orders.
    exchange.clock_value += 10
    restarted = ManualExitService(exchange, expanded_settings, clock=exchange.clock,
                                  emit=lambda *a, **k: None)
    assert D(restarted.worker.state["series_activation_times"]["KXWTI15M"]) == wti_activation
    assert restarted.run_once() and len(exchange.sent) == 3 and not exchange.cancelled
