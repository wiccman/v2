"""Rejected worker initialization must leave durable ownership journals intact."""
import json
from decimal import Decimal as D

import pytest

from manual_limit_exits import ManualExitBlocked, digest
from resting_manual_exits import RestingManualExitWorker
from test_manual_limit_exits import Exchange, T


CRYPTO_SERIES = ("KXBTC15M", "KXETH15M")
WTI_SERIES = (*CRYPTO_SERIES, "KXWTI15M")


def write_legacy_journal(path, exchange, *, policy, mode=None, markets=None):
    state = {
        "schema": 2,
        "activation_time": "900.0",
        "key_scope": digest(exchange.key_id),
        "series": list(CRYPTO_SERIES),
        "target_policy": policy,
        "markets": {} if markets is None else markets,
    }
    if mode is not None:
        state["execution_mode"] = mode
    path.write_text(json.dumps(state, indent=2) + "\n")
    return path.read_bytes()


@pytest.mark.parametrize("series", [CRYPTO_SERIES, WTI_SERIES])
@pytest.mark.parametrize("mode", ["monitored_reduce_only", None])
def test_incompatible_monitored_journal_is_not_upgraded_before_rejection(tmp_path, series, mode):
    exchange = Exchange()
    path = tmp_path / "manual.json"
    before = write_legacy_journal(
        path, exchange,
        policy={"mode": "price_increment", "increment": "0.05"},
        mode=mode,
        markets={T: {"pending": {"client_id": "preserve-unresolved-exit"},
                     "managed_fills": {}, "exit_orders": {}}},
    )

    with pytest.raises(ManualExitBlocked, match="IOC journal"):
        RestingManualExitWorker(exchange, path, series=series,
                               price_increment=D(".05"), clock=exchange.clock,
                               emit=lambda *args, **kwargs: None)

    assert path.read_bytes() == before
    assert not path.with_suffix(".tmp").exists()
    assert not exchange.sent and not exchange.cancelled and not exchange.reads


@pytest.mark.parametrize("series", [CRYPTO_SERIES, WTI_SERIES])
def test_missing_increment_does_not_upgrade_existing_legacy_journal(tmp_path, series):
    exchange = Exchange()
    path = tmp_path / "manual.json"
    before = write_legacy_journal(path, exchange, policy={"mode": "order_gross_profit"})

    with pytest.raises(ValueError, match="per-fill price increment"):
        RestingManualExitWorker(exchange, path, series=series, clock=exchange.clock,
                               emit=lambda *args, **kwargs: None)

    assert path.read_bytes() == before
    assert not path.with_suffix(".tmp").exists()
    assert not exchange.sent and not exchange.cancelled and not exchange.reads


def test_missing_increment_does_not_create_a_fresh_journal(tmp_path):
    exchange = Exchange()
    path = tmp_path / "new" / "manual.json"

    with pytest.raises(ValueError, match="per-fill price increment"):
        RestingManualExitWorker(exchange, path, series=WTI_SERIES, clock=exchange.clock,
                               emit=lambda *args, **kwargs: None)

    assert not path.exists()
    assert not path.parent.exists()
    assert not exchange.sent and not exchange.cancelled and not exchange.reads
