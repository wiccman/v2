"""Post +5c exits on fill detection; track their reservations until terminal.

Kalshi does not support reduce_only with GTC. Local ownership checks cannot
prevent a concurrent manual reduction or protect orders while this worker is
offline. Cancel the resting exit before manually closing the same position.
"""
from __future__ import annotations

import copy
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D

from kalshi import KalshiClient, KalshiAPIError
from manual_limit_exits import (ManualLimitExitWorker, ManualExitBlocked,
    TargetUnreachable, ACTIVE_ORDER_STATES, TERMINAL_ORDER_STATES, direction,
    number, primary_orders, quantity, round_target)
from request_coordinator import RequestDeferred


class RestingExitOnlyClient(KalshiClient):
    """Writes require a specific manual-exit submission or owned cancellation.

The worker supplies a durable intent after reconciling inventory. A generic
order, automatic entry, transfer, amend, or cancel-all call is never allowed.
"""
    def __init__(self, *args, dry_run=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.dry_run = dry_run
        self.base = "https://external-api.kalshi.com/trade-api/v2"
        self._approved_write = None

    def request(self, method, path, params=None, body=None, auth=False):
        if method.upper() != "GET":
            if self.dry_run or self._approved_write != (method, path, params, body):
                raise RuntimeError("Manual resting-exit client refused an unauthorized write")
        return super().request(method, path, params=params, body=body, auth=auth)

    def _write(self, method, path, *, params=None, body=None):
        if self.dry_run or self._approved_write is not None:
            raise RuntimeError("Manual resting-exit write is not permitted")
        self._approved_write = copy.deepcopy((method, path, params, body))
        try:
            return self.request(method, path, params=params, body=body, auth=True)
        finally:
            self._approved_write = None

    def place_manual_limit(self, ticker, intent):
        qty, target = quantity(intent["quantity"]), number(intent["target"], "target")
        side, cid = intent["side"], intent["client_id"]
        if not qty or not 0 < target < 1 or side not in {"YES", "NO"} or not cid.startswith("manual5c-"):
            raise ValueError("Invalid durable manual-exit intent")
        body = {"ticker": ticker, "client_order_id": cid,
                "side": "ask" if side == "YES" else "bid", "count": format(qty, "f"),
                "price": format((target if side == "YES" else 1-target).quantize(D(".0001")), "f"),
                "time_in_force": "good_till_canceled", "expiration_time": int(intent["expires_at"]),
                "self_trade_prevention_type": "taker_at_cross", "post_only": False,
                "cancel_order_on_pause": True, "reduce_only": False, "subaccount": 0}
        return self._write("POST", "/portfolio/events/orders", body=body)

    def cancel_manual_exit(self, ticker, intent):
        # Verify ownership afresh; cancellation is never authorized by a prefix
        # or an arbitrary order ID alone.
        oid = intent["order_id"]
        row = self.order(oid, ticker)
        if (row.get("client_order_id") != intent["client_id"] or row.get("ticker") != ticker
                or row.get("subaccount_number", 0) != 0):
            raise ManualExitBlocked("Cancellation ownership could not be verified")
        return self._write("DELETE", "/portfolio/events/orders/" + oid,
                           params={"market_ticker": ticker, "exchange_index": -1})


class RestingManualExitWorker(ManualLimitExitWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.price_increment is None:
            raise ValueError("Resting manual exits require a per-fill price increment")
        mode = self.state.get("execution_mode")
        if mode not in {None, "resting_limit"} or (mode is None and self.state["markets"]):
            raise ManualExitBlocked("Do not reuse an IOC journal for resting exits")
        self.state["execution_mode"] = "resting_limit"
        self.save()

    def intents(self, ticker):
        return self.ledger(ticker).setdefault("resting_intents", {})

    def pending(self, ticker):
        return any(i["status"] == "pending_ack" or i.get("cancel_requested")
                   for i in self.intents(ticker).values())

    def _validate(self, ticker, intent, row):
        sign = 1 if intent["side"] == "YES" else -1
        wire_price = number(row.get("yes_price_dollars"), "resting price")
        expected = D(intent["target"]) if sign > 0 else 1-D(intent["target"])
        if (row.get("ticker") != ticker or row.get("subaccount_number", 0) != 0
                or row.get("client_order_id") != intent["client_id"]
                or (intent.get("order_id") and row.get("order_id") != intent["order_id"])
                or row.get("type") != "limit" or direction(row) != -sign
                or wire_price != expected
                or quantity(row.get("initial_count_fp", row.get("initial_count"))) != D(intent["quantity"])):
            raise ManualExitBlocked("Resting exit identity or terms changed")
        filled = quantity(row.get("fill_count_fp", row.get("fill_count")))
        remaining = quantity(row.get("remaining_count_fp", row.get("remaining_count")))
        status = str(row.get("status", "")).lower()
        if (status not in ACTIVE_ORDER_STATES | TERMINAL_ORDER_STATES
                or filled < D(intent.get("filled", "0"))
                or filled + remaining > D(intent["quantity"])
                or (status in TERMINAL_ORDER_STATES and remaining)
                or (status in ACTIVE_ORDER_STATES and filled + remaining != D(intent["quantity"]))):
            raise ManualExitBlocked("Resting exit counts/status are inconsistent")
        return status, filled, remaining

    def reconcile(self, ticker):
        ledger, unresolved = self.ledger(ticker), False
        orders = primary_orders(self.client.all_orders(ticker), ticker)
        for cid, intent in self.intents(ticker).items():
            if intent["status"] in TERMINAL_ORDER_STATES:
                continue
            matches = [r for r in orders if r.get("client_order_id") == cid]
            if len(matches) > 1:
                raise ManualExitBlocked("Multiple orders match one saved client ID")
            if not matches and intent.get("order_id"):
                try:
                    matches = [self.client.order(intent["order_id"], ticker)]
                except KalshiAPIError as error:
                    if error.status_code != 404:
                        raise
            if not matches:
                unresolved = True
                continue  # Absence/404 never proves rejection or cancellation.
            row = matches[0]
            status, filled, remaining = self._validate(ticker, intent, row)
            previous = D(intent.get("filled", "0"))
            cancel_requested = intent.get("cancel_requested")
            intent.update(order_id=row["order_id"], status=status,
                          filled=str(filled), remaining=str(remaining))
            ledger.setdefault("exit_orders", {})[row["order_id"]] = {
                "side": intent["side"], "target": intent["target"], "filled": str(filled),
                "paired": False, "purpose": "manual_resting_take_profit",
                "allocations": intent["allocations"]}
            if status in TERMINAL_ORDER_STATES:
                intent.pop("cancel_requested", None)
                if filled < D(intent["quantity"]) and not cancel_requested:
                    # A user's cancel or an exchange rejection/pause must not be
                    # undone automatically. Other/new fills remain eligible.
                    ledger.setdefault("suppressed_fills", {})[intent["fill_id"]] = status
                    self.emit("MANUAL_RESTING_EXIT_STOPPED", ticker=ticker,
                              fill_id=intent["fill_id"], order_id=row["order_id"], status=status)
            self.save()
            if filled > previous:
                self.emit("MANUAL_RESTING_FILL", ticker=ticker, order_id=row["order_id"],
                          cumulative_quantity=str(filled), target=intent["target"])
            if cancel_requested and status in ACTIVE_ORDER_STATES:
                self._cancel(ticker, intent, cancel_requested)
                unresolved = True
        return not unresolved and not self.pending(ticker)

    def owns_resting_order(self, ticker, row):
        for intent in self.intents(ticker).values():
            if intent.get("order_id") == row["order_id"]:
                status, filled, remaining = self._validate(ticker, intent, row)
                if (status != intent["status"] or filled != D(intent["filled"])
                        or remaining != D(intent["remaining"])):
                    raise ManualExitBlocked("Resting reservation changed during reconciliation")
                return True
        return False

    def _cancel(self, ticker, intent, reason):
        if intent["status"] in TERMINAL_ORDER_STATES or not intent.get("order_id"):
            return False
        if self.dry_run:
            self.emit("MANUAL_DRY_RUN_CANCEL", ticker=ticker, order_id=intent["order_id"], reason=reason)
            return False
        if self.clock() < intent.get("cancel_retry_at", 0):
            return False
        intent.update(cancel_requested=reason, cancel_retry_at=self.clock() + 5)
        self.save()  # A timeout leaves a durable cancellation to reconcile.
        self.client.cancel_manual_exit(ticker, intent)
        self.emit("MANUAL_CANCEL_REQUESTED", ticker=ticker, order_id=intent["order_id"], reason=reason)
        return True  # Not a confirmed cancellation; never reuse its size yet.

    def suspend(self, ticker):
        # Best effort: errors must not prevent attempting other known exits.
        for intent in self.intents(ticker).values():
            if intent["status"] == "pending_ack":
                # An accepted new order may not be visible to the read model
                # yet. Preserve its reservation and recover it; do not turn
                # normal acknowledgement lag into cancel/repost churn.
                continue
            try:
                self._cancel(ticker, intent, "ownership_unverified")
            except Exception as error:
                self.emit("MANUAL_CANCEL_UNCONFIRMED", ticker=ticker,
                          order_id=intent.get("order_id"), error_type=type(error).__name__)

    def maybe_submit(self, ticker, record, automatic_ledger):
        ledger = self.ledger(ticker)
        if ledger.get("window_closed") or self.pending(ticker) or automatic_ledger.get("pending"):
            return False
        plans = {p["fill_id"]: p for p in self._plans.get(ticker, [])}
        active = [i for i in self.intents(ticker).values() if i["status"] in ACTIVE_ORDER_STATES]
        reserved = defaultdict(lambda: D(0))
        for intent in active:
            reserved[intent["fill_id"]] += D(intent["remaining"])
        # Retire oversized reservations before placing any new order, including
        # a manual close to zero or reversal. Never cancel a user's own order.
        cancel = []
        for intent in active:
            plan = plans.get(intent["fill_id"])
            if (not plan or reserved[intent["fill_id"]] > plan["quantity"]
                    or intent["side"] != ("YES" if plan["sign"] > 0 else "NO")):
                cancel.append(intent)
        if cancel:
            changed = False
            for intent in cancel:
                changed = self._cancel(ticker, intent, "inventory_reduced") or changed
            return changed
        if self.clock() < ledger.get("retry_after", 0):
            return False
        market = self.client.market(ticker)
        close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        if close.tzinfo is None:
            raise ManualExitBlocked("Market close requires a timezone")
        expires = int(close.timestamp())
        if market.get("status") not in {"active", "open"} or self.clock() >= expires:
            return False
        for fid, plan in sorted(plans.items()):
            if fid in ledger.get("suppressed_fills", {}):
                continue
            qty = quantity(plan["quantity"] - reserved[fid])
            if not qty:
                continue
            try:
                target = round_target(plan["raw_target"], market.get("price_ranges"), plan["sign"])
            except TargetUnreachable:
                self.emit("MANUAL_TARGET_UNREACHABLE", ticker=ticker, fill_id=fid,
                          required_price=str(plan["raw_target"]))
                continue
            self.verify_snapshot(ticker)
            held = self._snapshots[ticker]["held"]
            if held * plan["sign"] <= 0 or qty + sum(reserved.values(), D(0)) > abs(held):
                raise ManualExitBlocked("Resting exits exceed verified owned inventory")
            if self.dry_run:
                self.emit("MANUAL_DRY_RUN_RESTING_ORDER", ticker=ticker, fill_id=fid,
                          quantity=str(qty), target=str(target), expires_at=expires)
                continue
            intent = {"client_id": "manual5c-" + str(uuid.uuid4()), "status": "pending_ack",
                      "created_at": self.clock(), "expires_at": expires,
                      "side": "YES" if plan["sign"] > 0 else "NO", "quantity": str(qty),
                      "target": str(target), "fill_id": fid,
                      "allocations": [{"fill_id": fid, "quantity": str(qty)}]}
            self.intents(ticker)[intent["client_id"]] = intent
            self.save()
            try:
                self.verify_snapshot(ticker)
                if self.clock() >= expires:
                    raise ManualExitBlocked("Market closed before submission")
            except Exception:
                self.intents(ticker).pop(intent["client_id"])
                self.save()  # Proven unsent; safe to retry after reconciliation.
                raise
            try:
                result = self.client.place_manual_limit(ticker, intent)
            except (KalshiAPIError, RequestDeferred) as error:
                if isinstance(error, RequestDeferred) or error.status_code in {400, 401, 403, 404, 422, 429}:
                    self.intents(ticker).pop(intent["client_id"])
                    self.transport.defer(ledger, error)
                raise  # Unknown response/timeouts retain pending_ack.
            if (not result.get("order_id") or result.get("client_order_id") != intent["client_id"]):
                raise ManualExitBlocked("Resting order acknowledgement identity is missing/mismatched")
            intent["order_id"] = result["order_id"]
            self.save()
            self.emit("MANUAL_RESTING_SUBMITTED", ticker=ticker, fill_id=fid,
                      order_id=intent["order_id"], quantity=str(qty), target=str(target),
                      fill_confirmed=False)
            return True  # Reconcile again before submitting the next fill.
        return False

    def window_closed(self, ticker, close):
        if not self.reconcile(ticker):
            self.suspend(ticker)
            return False
        active = [i for i in self.intents(ticker).values() if i["status"] in ACTIVE_ORDER_STATES]
        if active:
            for intent in active:
                self._cancel(ticker, intent, "market_closed")
            return False
        super().window_closed(ticker, close)
        return True
