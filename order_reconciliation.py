"""Recover price-only exit discrepancies without relaxing order ownership checks.

A changed price is not evidence that an order is still live, nor is a cancel
404 evidence that it finished. Refresh the exact order, verify identity/counts,
and require matching fills before accepting a terminal price discrepancy.
Active mismatches are canceled and confirmed before their size is reusable.
The original per-fill take-profit target is never rewritten to the remote price.
"""
from __future__ import annotations

from decimal import Decimal as D

from kalshi import KalshiAPIError
from manual_limit_exits import (
    ACTIVE_ORDER_STATES, TERMINAL_ORDER_STATES, ManualExitBlocked,
    ReconciliationPending, RestingOrderMismatch, direction, number, quantity,
)
from price_pairs import _outcome_cost


def _observed_terms(worker, ticker, intent, row):
    """Strictly verify every term except price, for recovery evidence only."""
    wire = number(row.get("yes_price_dollars"), "observed exit price")
    if not 0 < wire < 1 or wire.quantize(D(".0001")) != wire:
        raise ManualExitBlocked("Invalid observed exit price")
    if row.get("no_price_dollars") is not None:
        if number(row["no_price_dollars"], "observed opposite price") + wire != 1:
            raise ManualExitBlocked("Conflicting observed exit prices")
    observed = dict(intent)
    observed["target"] = str(wire if intent["side"] == "YES" else 1 - wire)
    # This copy is never saved as the strategy intent and never authorizes a sell.
    state = worker._validate(ticker, observed, row)
    if state[0] == "executed" and state[1] != quantity(intent["quantity"]):
        raise ManualExitBlocked("Executed order count is incomplete")
    return state


def _verify_terminal_fills(worker, ticker, intent, row, filled):
    """Cross-check the order's terminal count against deduplicated actual fills."""
    total, seen = D(0), {}
    sign = 1 if intent["side"] == "YES" else -1
    for fill in worker.client.all_fills(ticker):
        if fill.get("order_id") != row["order_id"]:
            continue
        fid = fill.get("fill_id") or fill.get("trade_id")
        if not isinstance(fid, str) or not fid:
            raise ManualExitBlocked("Fill ID missing")
        if fid in seen:
            if seen[fid] != fill:
                raise ManualExitBlocked("Conflicting duplicate fill")
            continue
        seen[fid] = fill
        if ((fill.get("ticker") or fill.get("market_ticker")) != ticker
                or fill.get("subaccount_number", 0) != 0
                or direction(fill) != -sign):
            raise ManualExitBlocked("Terminal exit fill identity is inconsistent")
        count = quantity(fill.get("count_fp", fill.get("count")))
        if not count:
            raise ManualExitBlocked("Terminal exit fill quantity is zero")
        _outcome_cost(fill, sign)  # Reject missing/conflicting/nonfinite prices.
        total += count
    if total != filled:
        raise ReconciliationPending("Exit order/fill history has not converged")


def verified_exit_snapshot(worker, ticker, intent, row):
    """Return a validated (row, state), or None while recovery is unconfirmed."""
    try:
        return row, worker._validate(ticker, intent, row)
    except RestingOrderMismatch as error:
        if set(error.mismatched_fields) != {"yes_price_dollars"}:
            raise  # A price repair never overrides another identity/size failure.
        if error.observed_yes_price is None:
            raise
    _observed_terms(worker, ticker, intent, row)
    oid = row["order_id"]
    try:
        fresh = worker.client.order(oid, ticker)
    except KalshiAPIError as error:
        if error.status_code != 404:
            raise
        # The single-order helper also searches history. Neither a missing
        # row nor a 404 permits a new order or releases the saved reservation.
        worker.emit("MANUAL_PRICE_RECHECK_PENDING", ticker=ticker, order_id=oid)
        return None
    if fresh.get("order_id") != oid:
        raise ManualExitBlocked("Price recovery order identity changed")
    try:
        return fresh, worker._validate(ticker, intent, fresh)
    except RestingOrderMismatch as error:
        if set(error.mismatched_fields) != {"yes_price_dollars"}:
            raise
    state = _observed_terms(worker, ticker, intent, fresh)
    if state[0] in ACTIVE_ORDER_STATES:
        # Preserve the original target. Only release this order's reservation
        # after the venue confirms a terminal status and the fills agree.
        if not intent.get("order_id"):
            intent["order_id"] = oid
            worker.save()
        worker.emit("MANUAL_PRICE_REPAIR_CANCEL", ticker=ticker, order_id=oid,
                    observed_yes_price=str(number(fresh["yes_price_dollars"], "price")),
                    observed_status=state[0])
        try:
            attempted = worker._cancel(ticker, intent, "price_mismatch_repair")
        except KalshiAPIError as error:
            if error.status_code != 404:
                raise
            attempted = True  # Read back; this is NOT a cancellation confirmation.
        if not attempted:
            return None
        try:
            fresh = worker.client.order(oid, ticker)
        except KalshiAPIError as error:
            if error.status_code != 404:
                raise
            return None
        if fresh.get("order_id") != oid:
            raise ManualExitBlocked("Price recovery order identity changed")
        state = _observed_terms(worker, ticker, intent, fresh)
        if state[0] in ACTIVE_ORDER_STATES:
            return None
    if state[0] not in TERMINAL_ORDER_STATES:
        return None
    _verify_terminal_fills(worker, ticker, intent, fresh, state[1])
    expected = D(intent["target"]) if intent["side"] == "YES" else 1 - D(intent["target"])
    recovery = {"expected_yes_price": str(expected),
                "observed_yes_price": str(number(fresh["yes_price_dollars"], "price")),
                "status": state[0], "filled": str(state[1])}
    intent["price_recovery"] = recovery
    worker.save()
    worker.emit("MANUAL_PRICE_MISMATCH_RECOVERED", ticker=ticker, order_id=oid,
                **recovery, fills_verified=True, original_target_preserved=True)
    return fresh, state


def terminal_cancel_receipt(ticker, intent, row):
    """Avoid DELETE on a proven terminal order; caller must still reconcile it."""
    status = str(row.get("status", "")).lower()
    if status not in TERMINAL_ORDER_STATES:
        return None
    sign = 1 if intent["side"] == "YES" else -1
    initial = quantity(row.get("initial_count_fp", row.get("initial_count")))
    filled = quantity(row.get("fill_count_fp", row.get("fill_count")))
    remaining = quantity(row.get("remaining_count_fp", row.get("remaining_count")))
    if (row.get("order_id") != intent["order_id"]
            or row.get("client_order_id") != intent["client_id"]
            or row.get("ticker") != ticker or row.get("subaccount_number", 0) != 0
            or row.get("type") != "limit" or direction(row) != -sign
            or initial != quantity(intent["quantity"]) or remaining != 0
            or not quantity(intent.get("filled", "0")) <= filled <= initial
            or (status == "executed" and filled != initial)):
        raise ManualExitBlocked("Terminal cancellation evidence is inconsistent")
    return {"order_id": row["order_id"], "client_order_id": row["client_order_id"],
            "already_terminal": True}
