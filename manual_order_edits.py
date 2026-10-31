"""Opt-in read-only adoption of operator amendments to a known exit order.

Kalshi allows amendments to price/count and client_order_id. We anchor ownership
in the exchange order_id already saved before any edit, never a price or prefix.
This module never sends an amend/cancel/order request. A changed order is read
back, bounded by its original fill allocation and checked against actual fills
before its new terms are saved. Unknown identities remain fail-closed.
"""
from __future__ import annotations

import copy
from decimal import Decimal as D

from kalshi import KalshiAPIError
from manual_limit_exits import (
    ACTIVE_ORDER_STATES, TERMINAL_ORDER_STATES, ManualExitBlocked,
    ReconciliationPending, direction, number, quantity,
)
from order_reconciliation import _verify_terminal_fills


class ManualEditPending(ReconciliationPending):
    """An identifiable amendment needs a fresh read; preserve reservations."""


def amendment_terms(ticker, intent, row):
    """Validate immutable identity and bounded mutable terms without saving."""
    oid = intent.get("order_id")
    sign = 1 if intent["side"] == "YES" else -1
    if (not isinstance(oid, str) or not oid or row.get("order_id") != oid
            or row.get("ticker") != ticker or row.get("subaccount_number", 0) != 0
            or row.get("type") != "limit" or direction(row) != -sign):
        raise ManualExitBlocked("Manual amendment order identity is inconsistent")
    cid = row.get("client_order_id")
    if not isinstance(cid, str) or not cid or len(cid) > 128:
        raise ManualExitBlocked("Manual amendment client identity is invalid")
    wire = number(row.get("yes_price_dollars"), "manual amendment price")
    if not 0 < wire < 1 or wire.quantize(D(".0001")) != wire:
        raise ManualExitBlocked("Invalid manual amendment price")
    if row.get("no_price_dollars") is not None:
        if number(row["no_price_dollars"], "opposite amendment price") + wire != 1:
            raise ManualExitBlocked("Conflicting manual amendment prices")
    initial = quantity(row.get("initial_count_fp", row.get("initial_count")))
    filled = quantity(row.get("fill_count_fp", row.get("fill_count")))
    remaining = quantity(row.get("remaining_count_fp", row.get("remaining_count")))
    status = str(row.get("status", "")).lower()
    allocation = sum((quantity(a["quantity"]) for a in intent["allocations"]), D(0))
    cap = quantity(intent.get("original_quantity", intent["quantity"]))
    if (status not in ACTIVE_ORDER_STATES | TERMINAL_ORDER_STATES or not initial
            or not cap or cap > allocation or initial > cap
            or filled < quantity(intent.get("filled", "0"))
            or filled + remaining > initial
            or (status in TERMINAL_ORDER_STATES and remaining)):
        raise ManualExitBlocked("Manual amendment exceeds allocated inventory or has invalid counts")
    # Decrease can retain the original initial_count. The effective max is the
    # already-filled quantity plus the desired outstanding quantity, not a delta.
    if status in ACTIVE_ORDER_STATES:
        effective = filled + remaining
        if not remaining:
            raise ManualEditPending("Active amended order has no remaining quantity yet")
    elif status == "executed":
        effective = filled
        if not effective:
            raise ManualExitBlocked("Executed amended order has no verified execution")
    else:
        effective = min(initial, quantity(intent["quantity"]))
        if filled > effective:
            effective = filled
    return {"target": str(wire if sign > 0 else 1-wire),
            "client_id": cid, "quantity": str(effective),
            "wire_initial_quantity": str(initial)}, (status, filled, remaining)


def verified_manual_exit_snapshot(worker, ticker, intent, row):
    """Preserve valid edits; no cancellation or default-target restoration."""
    try:
        return row, worker._validate(ticker, intent, row)
    except ManualExitBlocked:
        # Before requesting a refresh, refuse changed immutable identity,
        # over-allocation, missing price data and contradictory counts.
        amendment_terms(ticker, intent, row)
    try:
        fresh = worker.client.order(intent["order_id"], ticker)
    except KalshiAPIError as error:
        if error.status_code != 404:
            raise
        worker.emit("MANUAL_EDIT_RECHECK_PENDING", ticker=ticker,
                    order_id=intent["order_id"])
        return None  # A 404 never releases a reservation or permits a new sell.
    try:
        return fresh, worker._validate(ticker, intent, fresh)
    except ManualExitBlocked:
        terms, state = amendment_terms(ticker, intent, fresh)
    try:
        _verify_terminal_fills(worker, ticker, intent, fresh, state[1])
    except ReconciliationPending as error:
        raise ManualEditPending(str(error)) from error
    updated = copy.deepcopy(intent)
    updated.setdefault("original_target", intent["target"])
    updated.setdefault("original_quantity", intent["quantity"])
    updated.update(terms, manual_override=True)
    # Full validation is still required; the order's identity/direction/counts
    # are not bypassed by enabling this policy.
    worker._validate(ticker, updated, fresh)
    previous = copy.deepcopy(intent)
    ledger = worker.ledger(ticker)
    overrides_before = copy.deepcopy(ledger.get("manual_tp_overrides", {}))
    overrides = ledger.setdefault("manual_tp_overrides", {})
    overrides[intent["fill_id"]] = {"reason": "operator_amended_existing_order",
                                    "order_id": intent["order_id"]}
    intent.clear()
    intent.update(updated)
    try:
        worker.save()
    except Exception:
        intent.clear()
        intent.update(previous)
        ledger["manual_tp_overrides"] = overrides_before
        raise
    worker.emit("MANUAL_ORDER_EDIT_ADOPTED", ticker=ticker,
                order_id=intent["order_id"], fill_id=intent["fill_id"],
                previous_target=previous["target"], target=intent["target"],
                previous_quantity=previous["quantity"], quantity=intent["quantity"],
                remaining=str(state[2]), fills_verified=True,
                default_tp_repost_suppressed=True)
    return fresh, state
