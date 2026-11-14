"""Regression checks for the reversible Crypto Leader-only routing switch."""

from unittest.mock import Mock

import minute8_top2 as router


def test_zone_buying_dormant_still_reconciles_owned_exits(monkeypatch):
    monkeypatch.setenv("ZONE_BUYING_ENABLED", "false")
    legacy = Mock(return_value="reconciled")
    monkeypatch.setattr(router.cash, "run_cash_checked_pass", legacy)
    no_regular = Mock(side_effect=AssertionError("Zone 1 entries must be dormant"))
    no_late = Mock(side_effect=AssertionError("Zone 2 entries must be dormant"))
    monkeypatch.setattr(router, "regular_pass", no_regular)
    monkeypatch.setattr(router.late, "run_late_pass", no_late)

    result = router.run_pass(
        "client", "journal", ("KXETH15M", "KXXRP15M"), 10, 20,
        True, 720.0, provider=Mock(), clock=lambda: 720.0
    )
    assert result == "reconciled"
    legacy.assert_called_once_with(
        "client", "journal", (), 10, 20, True, 720.0, 1
    )
    no_regular.assert_not_called()
    no_late.assert_not_called()


def test_zone_buying_can_be_restored_without_code_changes(monkeypatch):
    monkeypatch.setenv("ZONE_BUYING_ENABLED", "true")
    regular = Mock()
    late = Mock(return_value="zone2")
    monkeypatch.setattr(router, "regular_pass", regular)
    monkeypatch.setattr(router.late, "run_late_pass", late)
    monkeypatch.setattr(router.cash, "run_cash_checked_pass",
                        Mock(side_effect=AssertionError("Not dormant")))

    result = router.run_pass(
        "client", "journal", ("KXETH15M",), 10, 20,
        True, 720.0, provider="provider", clock=lambda: 720.0
    )
    assert result == "zone2"
    regular.assert_called_once()
    late.assert_called_once()


def test_dormant_switch_is_fail_closed_for_unrecognized_value(monkeypatch):
    monkeypatch.setenv("ZONE_BUYING_ENABLED", "disabled")
    assert not router.zone_buying_enabled()


def test_manual_tp_worker_runs_with_leader_and_excludes_bot_owned_markets(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("ZONE_BUYING_ENABLED", "false")
    monkeypatch.setenv("MANUAL_EXITS_ENABLED", "true")
    monkeypatch.setenv("LEADER_TRADING_ENABLED", "false")
    worker = Mock()
    start = Mock(return_value=worker)
    monkeypatch.setattr(router, "manual_take_profit", start)
    monkeypatch.setattr(router, "run_pass", Mock())
    journal = SimpleNamespace(data={"windows": {
        "zone2::KXETH15M-test": {"ticker": "KXETH15M-test", "buy": {"client_id": "owned"}},
    }})
    with router.installed_policy(provider=Mock()):
        router.core.run_pass("client", journal, (), 5, 5, False, 720)
        start.assert_not_called()
        router.core.run_pass("client", journal, (), 5, 5, True, 720)
        router.core.run_pass("client", journal, (), 5, 5, True, 722)
    start.assert_called_once()
    assert worker.run_once.call_count == 2
    worker.run_once.assert_called_with(excluded_tickers={"KXETH15M-test"})
