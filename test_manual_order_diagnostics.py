"""Offline regressions for strict, safe WTI resting-order diagnostics."""
import copy
import json
from decimal import Decimal as D

import pytest

from manual_limit_exits import ManualExitBlocked, RestingOrderMismatch, failure_context
from test_manual_wti_exits import WTI, setup
from test_resting_manual_exits import exit_ids


SECRET = "UNTRUSTED_RESPONSE_SECRET_DO_NOT_LOG"


def managed_wti_exit(tmp_path, sign):
    exchange, service, settings, events = setup(tmp_path)
    exchange.buy("40", ".919", sign, ticker=WTI)
    assert service.run_once()
    assert len(exchange.sent) == 1
    oid = exit_ids(exchange)[0]
    intent = next(iter(service.worker.intents(WTI).values()))
    assert D(intent["target"]) == D(".969")
    return exchange, service, settings, events, oid, copy.deepcopy(intent)


def mismatch_context(worker, intent, row):
    with pytest.raises(RestingOrderMismatch) as rejected:
        worker._validate(WTI, intent, row)
    assert isinstance(rejected.value, ManualExitBlocked)
    context = failure_context(rejected.value)
    assert context["reason"] == "Resting exit identity or terms changed"
    assert context["error_type"] == "RestingOrderMismatch"
    assert isinstance(context["expected_yes_price"], str)
    assert isinstance(context["expected_quantity"], str)
    assert D(context["expected_quantity"]) == D("40")
    return rejected.value, context


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("changed", ["price", "quantity", "both"])
def test_subcent_price_and_fractional_quantity_mismatches_report_exact_fields(tmp_path, sign, changed):
    exchange, service, _, _, oid, intent = managed_wti_exit(tmp_path, sign)
    row = copy.deepcopy(exchange.remote[oid])
    expected_price = D(".969") if sign > 0 else D(".031")
    fields = set()
    if changed in {"price", "both"}:
        # Even a tenth of a 0.1-cent tick must not be hidden by rounding.
        row["yes_price_dollars"] = str(expected_price + D(".0001"))
        fields.add("yes_price_dollars")
    if changed in {"quantity", "both"}:
        row["initial_count_fp"] = "40.01"
        fields.add("initial_count_fp")

    _, context = mismatch_context(service.worker, intent, row)

    assert set(context["mismatched_fields"]) == fields
    assert D(context["expected_yes_price"]) == expected_price
    assert isinstance(context["observed_yes_price"], str)
    assert isinstance(context["observed_quantity"], str)
    assert D(context["observed_yes_price"]) == D(row["yes_price_dollars"])
    assert D(context["observed_quantity"]) == D(row["initial_count_fp"])
    assert len(exchange.sent) == 1 and not exchange.cancelled
    assert service.worker.intents(WTI)[intent["client_id"]] == intent


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("field", [
    "ticker", "subaccount_number", "client_order_id", "order_id", "type", "direction",
])
def test_identity_and_direction_mismatches_remain_hard_failures(tmp_path, sign, field):
    exchange, service, _, _, oid, intent = managed_wti_exit(tmp_path, sign)
    row = copy.deepcopy(exchange.remote[oid])
    replacement = {
        "ticker": "KXWTI15M-DIFFERENT-WINDOW",
        "subaccount_number": 1,
        "client_order_id": "different-client-id",
        "order_id": "different-exchange-order-id",
        "type": "market",
    }
    if field == "direction":
        row["book_side"] = "bid" if sign > 0 else "ask"
        row["outcome_side"] = "yes" if sign > 0 else "no"
    else:
        row[field] = replacement[field]

    _, context = mismatch_context(service.worker, intent, row)

    assert context["mismatched_fields"] == [field]
    assert D(context["expected_yes_price"]) == (D(".969") if sign > 0 else D(".031"))
    assert len(exchange.sent) == 1 and not exchange.cancelled


@pytest.mark.parametrize("field,value,reported", [
    ("yes_price_dollars", SECRET, "yes_price_dollars"),
    ("yes_price_dollars", {"private": SECRET}, "yes_price_dollars"),
    ("yes_price_dollars", [SECRET], "yes_price_dollars"),
    ("yes_price_dollars", "NaN", "yes_price_dollars"),
    ("initial_count_fp", SECRET, "initial_count_fp"),
    ("initial_count_fp", {"private": [SECRET]}, "initial_count_fp"),
    ("initial_count_fp", "Infinity", "initial_count_fp"),
    ("ticker", SECRET, "ticker"),
    ("subaccount_number", SECRET, "subaccount_number"),
    ("client_order_id", SECRET, "client_order_id"),
    ("order_id", SECRET, "order_id"),
    ("type", SECRET, "type"),
    ("book_side", SECRET, "direction"),
    ("outcome_side", {"private": SECRET}, "direction"),
])
def test_invalid_response_values_are_redacted_in_exception_and_log_context(tmp_path, field, value, reported):
    exchange, service, _, _, oid, intent = managed_wti_exit(tmp_path, 1)
    row = copy.deepcopy(exchange.remote[oid])
    row[field] = value
    row["unrelated_response_body"] = {"api_key": SECRET}

    error, context = mismatch_context(service.worker, intent, row)

    assert context["mismatched_fields"] == [reported]
    encoded = json.dumps({"exception": str(error), "context": context}, allow_nan=False)
    assert SECRET not in encoded
    assert "unrelated_response_body" not in encoded and "api_key" not in encoded
    if field == "yes_price_dollars":
        assert context["observed_yes_price"] is None
    if field == "initial_count_fp":
        assert context["observed_quantity"] is None
    assert len(exchange.sent) == 1 and not exchange.cancelled


@pytest.mark.parametrize("sign", [1, -1])
def test_correct_subcent_exit_and_its_partial_fills_are_accepted_without_churn(tmp_path, sign):
    exchange, service, _, _, oid, intent = managed_wti_exit(tmp_path, sign)
    row = exchange.remote[oid]
    row["yes_price_dollars"] = "0.9690" if sign > 0 else "0.0310"
    row["initial_count_fp"] = "40.00"

    assert service.worker._validate(WTI, intent, row) == ("resting", D(0), D(40))
    assert service.run_once()
    exchange.fill(oid, "10.25", ".969")
    assert service.run_once()
    saved = service.worker.intents(WTI)[intent["client_id"]]
    assert D(saved["filled"]) == D("10.25")
    assert D(saved["remaining"]) == D("29.75")
    assert len(exchange.sent) == 1 and not exchange.cancelled

    exchange.fill(oid, "29.75", ".969")
    assert service.run_once()
    assert exchange.held[WTI] == 0
    assert len(exchange.sent) == 1 and not exchange.cancelled


@pytest.mark.parametrize("sign", [1, -1])
def test_app_auto_sell_overlap_cancels_only_bot_order_without_duplicates(tmp_path, sign):
    exchange, service, _, events, bot_oid, _ = managed_wti_exit(tmp_path, sign)
    app_oid = exchange.make_order(WTI, sign, "40", ".969",
                                  client_id="kalshi-app-autosell", action="sell")
    app_before = copy.deepcopy(exchange.remote[app_oid])

    assert not service.run_once()
    assert exchange.cancelled == [bot_oid]
    assert exchange.remote[app_oid] == app_before
    assert len(exchange.sent) == 1
    assert any(name == "MANUAL_EXIT_PAUSED" and
               data.get("reason") == "An existing resting order can reduce the shared position"
               for name, data in events)

    # Repeated reconciliation must neither replace the bot order nor touch the app order.
    assert not service.run_once()
    assert exchange.cancelled == [bot_oid]
    assert exchange.remote[app_oid] == app_before
    assert len(exchange.sent) == 1

    exchange.fill(app_oid, "40", ".969")
    assert service.run_once()
    assert exchange.held[WTI] == 0
    assert len(exchange.sent) == 1
    assert exchange.cancelled == [bot_oid] and app_oid not in exchange.cancelled
