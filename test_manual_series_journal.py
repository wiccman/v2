"""Offline rejection and recovery checks for extending manual-exit coverage."""
import copy
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from manual_exit_runner import ManualExitService
from manual_limit_exits import ManualExitBlocked
from test_manual_wti_exits import setup, BTC, CRYPTO_SERIES, WTI_SERIES


@pytest.mark.parametrize("series", [
    ("KXBTC15M",),
    ("KXETH15M", "KXBTC15M"),
    ("KXBTC15M", "KXWTI15M"),
    ("KXWTI15M", "KXBTC15M", "KXETH15M"),
])
def test_scope_removal_replacement_or_reordering_cannot_forget_existing_exits(tmp_path, series):
    exchange, service, settings, _ = setup(tmp_path, series=CRYPTO_SERIES)
    exchange.buy("2.75", ".42", ticker=BTC)
    assert service.run_once() and len(exchange.sent) == 1
    before = settings.manual_state.read_bytes()

    with pytest.raises(ManualExitBlocked, match="series scope"):
        ManualExitService(exchange, replace(settings, series=series), clock=exchange.clock)

    assert settings.manual_state.read_bytes() == before
    assert len(exchange.sent) == 1 and not exchange.cancelled


@pytest.mark.parametrize("corruption", [
    "missing_map", "missing_series", "unknown_series", "not_a_map",
    "nonfinite_time", "early_time", "wrong_account", "wrong_policy",
])
def test_invalid_activation_metadata_blocks_extension_without_rewriting_state(tmp_path, corruption):
    exchange, service, settings, _ = setup(tmp_path, series=CRYPTO_SERIES)
    exchange.buy("2.75", ".42", ticker=BTC)
    assert service.run_once()
    saved = copy.deepcopy(service.worker.state)
    if corruption == "missing_map":
        saved.pop("series_activation_times")
    elif corruption == "missing_series":
        saved["series_activation_times"].pop("KXETH15M")
    elif corruption == "unknown_series":
        saved["series_activation_times"]["KXWTI15M"] = "1000"
    elif corruption == "not_a_map":
        saved["series_activation_times"] = ["1000", "1000"]
    elif corruption == "nonfinite_time":
        saved["series_activation_times"]["KXETH15M"] = "NaN"
    elif corruption == "early_time":
        saved["series_activation_times"]["KXETH15M"] = "999"
    elif corruption == "wrong_account":
        saved["key_scope"] = "another-account"
    elif corruption == "wrong_policy":
        saved["target_policy"]["increment"] = "0.06"
    settings.manual_state.write_text(json.dumps(saved))
    before = settings.manual_state.read_bytes()
    exchange.clock_value += 10

    with pytest.raises(ManualExitBlocked):
        ManualExitService(exchange, replace(settings, series=WTI_SERIES), clock=exchange.clock)

    assert settings.manual_state.read_bytes() == before
    assert len(exchange.sent) == 1 and not exchange.cancelled


def test_legacy_upgrade_preserves_pending_crypto_fills_original_cutoff(tmp_path):
    exchange, service, settings, _ = setup(tmp_path, series=CRYPTO_SERIES)
    exchange.buy("2.75", ".42", ticker=BTC)  # Filled before the restart, not yet observed.
    saved = copy.deepcopy(service.worker.state)
    saved["schema"] = 2
    saved.pop("series_activation_times")
    settings.manual_state.write_text(json.dumps(saved))
    original_activation = D(saved["activation_time"])
    exchange.clock_value += 10

    restarted = ManualExitService(exchange, settings, clock=exchange.clock,
                                  emit=lambda *a, **k: None)
    assert restarted.run_once() and len(exchange.sent) == 1
    assert exchange.sent[0]["count"] == "2.75"
    assert exchange.sent[0]["price"] == "0.4700"
    assert all(D(t) == original_activation
               for t in restarted.worker.state["series_activation_times"].values())


def test_backward_clock_cannot_retroactively_enable_new_series(tmp_path):
    exchange, _, settings, _ = setup(tmp_path, series=CRYPTO_SERIES)
    before = settings.manual_state.read_bytes()
    exchange.clock_value -= 1

    with pytest.raises(ManualExitBlocked, match="Clock precedes"):
        ManualExitService(exchange, replace(settings, series=WTI_SERIES), clock=exchange.clock)

    assert settings.manual_state.read_bytes() == before
    assert not exchange.sent and not exchange.cancelled
