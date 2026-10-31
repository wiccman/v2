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
    ReconciliationPending, RestingOrderMismatch,
    TargetUnreachable, ACTIVE_ORDER_STATES, TERMINAL_ORDER_STATES, direction,
    number, primary_orders, quantity, round_target, failure_context)
from request_coordinator import RequestDeferred
from order_reconciliation import verified_exit_snapshot, terminal_cancel_receipt


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

    def place_manual_stop(self, ticker, intent):
        """One explicit, price-protected, reduce-only IOC; never a resting stop sell."""
        qty = quantity(intent["quantity"])
        limit = number(intent["target"], "stop execution price")
        side, cid = intent["side"], intent["client_id"]
        if (not qty or not D(0) < limit < 1 or side not in {"YES", "NO"}
                or not isinstance(cid, str) or not cid.startswith("manualstop-")):
            raise ValueError("Invalid durable manual stop intent")
        body = {"ticker": ticker, "client_order_id": cid,
                "side": "ask" if side == "YES" else "bid", "count": format(qty, "f"),
                "price": format((limit if side == "YES" else 1-limit).quantize(D(".0001")), "f"),
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross", "post_only": False,
                "cancel_order_on_pause": True, "reduce_only": True}
        return self._write("POST", "/portfolio/events/orders", body=body)

    def cancel_manual_exit(self, ticker, intent):
        # Verify ownership afresh; cancellation is never authorized by a prefix
        # or an arbitrary order ID alone.
        oid = intent["order_id"]
        row = self.order(oid, ticker)
        if (row.get("client_order_id") != intent["client_id"] or row.get("ticker") != ticker
                or row.get("subaccount_number", 0) != 0):
            raise ManualExitBlocked("Cancellation ownership could not be verified")
        receipt = terminal_cancel_receipt(ticker, intent, row)
        if receipt is not None:
            return receipt  # No DELETE; reconciliation still verifies actual fills.
        return self._write("DELETE", "/portfolio/events/orders/" + oid,
                           params={"market_ticker": ticker, "exchange_index": -1})


class RestingManualExitWorker(ManualLimitExitWorker):
    def __init__(self, *args, stop_loss_cents=D("11"), **kwargs):
        self.stop_loss_increment = number(stop_loss_cents, "stop loss cents") / 100
        if not D(0) < self.stop_loss_increment < 1:
            raise ValueError("Manual stop loss must be between zero and one dollar")
        super().__init__(*args, **kwargs)
        previous = self.state.get("stop_loss_increment")
        if previous is not None and number(previous, "saved stop loss") != self.stop_loss_increment:
            raise ManualExitBlocked("Stop loss policy changed; review the existing journal")
        self.state["stop_loss_increment"] = str(self.stop_loss_increment)
        self.state["execution_mode"] = "resting_limit"
        self._coverage_reports = {}
        self._protection_reports = {}
        self._protection_report_at = {}
        self._protection_cycles = defaultdict(int)
        self._stop_checked_cycle = {}
        self.save()

    def _validate_execution_mode(self):
        if self.price_increment is None:
            raise ValueError("Resting manual exits require a per-fill price increment")
        mode = self.state.get("execution_mode")
        if mode not in {None, "resting_limit"} or (mode is None and self.state["markets"]):
            raise ManualExitBlocked("Do not reuse an IOC journal for resting exits")

    def intents(self, ticker):
        return self.ledger(ticker).setdefault("resting_intents", {})

    def pending(self, ticker):
        return bool(self.ledger(ticker).get("pending")) or any(
            i["status"] == "pending_ack" or i.get("cancel_requested")
            for i in self.intents(ticker).values())

    def _validate(self, ticker, intent, row):
        sign = 1 if intent["side"] == "YES" else -1
        expected = D(intent["target"]) if sign > 0 else 1-D(intent["target"])
        expected_qty = quantity(intent["quantity"])
        # Never print a raw response or arbitrary string value. Invalid numeric
        # representations are reported as null, with the failed field named.
        try:
            wire_price = number(row.get("yes_price_dollars"), "resting price")
        except ManualExitBlocked:
            wire_price = None
        try:
            initial = quantity(row.get("initial_count_fp", row.get("initial_count")))
        except ManualExitBlocked:
            initial = None
        try:
            wire_direction = direction(row)
        except ManualExitBlocked:
            wire_direction = None
        checks = {
            "ticker": row.get("ticker") == ticker,
            "subaccount_number": row.get("subaccount_number", 0) == 0,
            "client_order_id": row.get("client_order_id") == intent["client_id"],
            "order_id": not intent.get("order_id") or row.get("order_id") == intent["order_id"],
            "type": row.get("type") == "limit",
            "direction": wire_direction == -sign,
            "yes_price_dollars": wire_price == expected,
            "initial_count_fp": initial == expected_qty,
        }
        mismatches = [name for name, matched in checks.items() if not matched]
        if mismatches:
            raise RestingOrderMismatch(mismatches, expected, wire_price, expected_qty, initial)
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
        # Recover a stop IOC before considering any new exits. An ambiguous
        # acknowledgement must never generate a second stop order.
        if not self.transport._reconcile(ticker, ledger):
            return False
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
            verified = verified_exit_snapshot(self, ticker, intent, row)
            if verified is None:
                unresolved = True
                continue
            row, (status, filled, remaining) = verified
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
                    raise ReconciliationPending("Resting reservation changed during reconciliation")
                return True
        return False

    def external_order_reservation(self, orders):
        reserved = D(0)
        for row in orders:
            remaining = quantity(row.get("remaining_count_fp", row.get("remaining_count")))
            filled = quantity(row.get("fill_count_fp", row.get("fill_count")))
            initial = quantity(row.get("initial_count_fp", row.get("initial_count")))
            # A user's amended/decreased order can retain its original initial
            # count. Only the verified still-resting amount reserves inventory.
            if filled + remaining > initial:
                raise ManualExitBlocked("External resting order counts are inconsistent")
            reserved += remaining
        return reserved

    def observe(self, ticker, record, automatic_ledger, fills, entry_orders, exit_orders, held):
        super().observe(ticker, record, automatic_ledger, fills, entry_orders, exit_orders, held)
        snapshot = self._snapshots[ticker]
        reserved = defaultdict(lambda: D(0))
        for intent in self.intents(ticker).values():
            if intent["status"] in ACTIVE_ORDER_STATES:
                reserved[intent["fill_id"]] += D(intent["remaining"])
        uncovered = sum((max(D(0), plan["quantity"] - reserved[plan["fill_id"]])
                         for plan in self._plans[ticker]), D(0))
        report = {"external_remaining": str(snapshot["external_reserved"]),
                  "held_quantity": str(abs(snapshot["held"])),
                  "bot_reserved_quantity": str(sum(reserved.values(), D(0))),
                  "uncovered_quantity": str(uncovered)}
        if (snapshot["external_reserved"] or ticker in self._coverage_reports) and self._coverage_reports.get(ticker) != report:
            self._coverage_reports[ticker] = report
            self.emit("MANUAL_EXTERNAL_COVERAGE", ticker=ticker, **report)

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
        result = self.client.cancel_manual_exit(ticker, intent)
        event = ("MANUAL_CANCEL_NOT_NEEDED" if isinstance(result, dict) and
                 result.get("already_terminal") else "MANUAL_CANCEL_REQUESTED")
        self.emit(event, ticker=ticker, order_id=intent["order_id"], reason=reason)
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
                          order_id=intent.get("order_id"), **failure_context(error))

    def begin_protection_check(self, ticker):
        """A cycle only counts as monitored after a *fresh* valid market quote."""
        self._protection_cycles[ticker] += 1

    def report_protection(self, ticker):
        """Audit actual exchange TP reservations and the local SL monitor for open fills.

        Every managed fill with available, unreserved inventory has a distinct
        TP and SL state. A stop is software-monitored, never claimed to be a
        resting exchange order. Reconcile validates each TP's remote identity,
        quantity, direction and price before it can count as active.
        """
        ledger = self.ledger(ticker)
        plans = {p["fill_id"]: p for p in self._plans.get(ticker, [])}
        active = [i for i in self.intents(ticker).values()
                  if i["status"] in ACTIVE_ORDER_STATES]
        reservations = defaultdict(lambda: D(0))
        pending = set()
        canceling = set()
        for intent in self.intents(ticker).values():
            fid = intent["fill_id"]
            if intent.get("cancel_requested"):
                canceling.add(fid)
            if intent["status"] == "pending_ack":
                pending.add(fid)
            if intent["status"] in ACTIVE_ORDER_STATES:
                reservations[fid] += quantity(intent["remaining"])
        stop_verified = (self._stop_checked_cycle.get(ticker) ==
                         self._protection_cycles[ticker])
        # Also track open manual inventory covered by someone else's sell.
        # That order is not safely cancelable by this bot, so don't claim
        # those contracts have a bot-managed stop.
        remaining = {}
        for summary in ledger.get("profit_summary", {}).values():
            for fill in summary.get("fills", []):
                fid = fill["fill_id"]
                qty = quantity(fill["remaining_quantity"])
                if qty:
                    remaining[fid] = qty
        old = {fid for t, fid in self._protection_reports if t == ticker}
        current = set()

        def publish(report):
            key = (ticker, report["fill_id"])
            now = self.clock()
            if (self._protection_reports.get(key) != report or
                    now - self._protection_report_at.get(key, float("-inf")) >= 30):
                self._protection_reports[key] = report
                self._protection_report_at[key] = now
                self.emit("MANUAL_PROTECTION_CHECK", ticker=ticker, **report)
                if (report["tp_status"] in {"missing", "partial", "suppressed",
                                            "over_reserved", "external_sell"} or
                        report["sl_status"] in {"unverified", "unavailable",
                                               "blocked_by_external_sell",
                                               "partially_blocked"}):
                    self.emit("MANUAL_PROTECTION_ALERT", ticker=ticker, **report)

        for fid, plan in sorted(plans.items()):
            current.add(fid)
            entry = number(ledger["managed_fills"][fid]["cost"], "entry cost")
            stop_trigger = entry - self.stop_loss_increment
            expected = plan["quantity"]
            actual = reservations[fid]
            if fid in canceling:
                tp = "canceling"
            elif fid in pending:
                tp = "submitting"
            elif fid in ledger.get("suppressed_fills", {}):
                tp = "suppressed"
            elif actual == expected:
                tp = "active"
            elif actual == 0:
                tp = "missing"
            elif actual < expected:
                tp = "partial"
            else:
                tp = "over_reserved"
            external_qty = max(D(0), remaining.get(fid, expected) - expected)
            if fid in ledger.get("stop_armed", {}):
                sl = "triggered"
            elif stop_trigger <= 0:
                sl = "unavailable"  # No positive sell price 11c below entry.
            elif not stop_verified:
                sl = "unverified"
            elif external_qty:
                sl = "partially_blocked"
            else:
                sl = "monitoring"
            report = {"fill_id": fid, "side": "YES" if plan["sign"] > 0 else "NO",
                      "entry": str(entry), "quantity": str(expected),
                      "externally_reserved_quantity": str(external_qty),
                      "tp_target": str(plan["raw_target"]),
                      "tp_status": tp, "tp_resting_quantity": str(actual),
                      "sl_trigger": str(stop_trigger) if stop_trigger > 0 else None,
                      "sl_status": sl, "sl_order_type": "local_bid_monitor"}
            publish(report)
        for fid, qty in sorted(remaining.items()):
            if fid in current:
                continue
            current.add(fid)
            saved = ledger["managed_fills"][fid]
            entry = number(saved["cost"], "entry cost")
            trigger = entry - self.stop_loss_increment
            publish({"fill_id": fid, "side": "YES" if saved["sign"] > 0 else "NO",
                     "entry": str(entry), "quantity": "0",
                     "externally_reserved_quantity": str(qty),
                     "tp_target": str(entry + self.price_increment),
                     "tp_status": "external_sell", "tp_resting_quantity": "0",
                     "sl_trigger": str(trigger) if trigger > 0 else None,
                     "sl_status": "blocked_by_external_sell",
                     "sl_order_type": "local_bid_monitor"})
        for fid in old - current:
            self._protection_reports.pop((ticker, fid), None)
            self._protection_report_at.pop((ticker, fid), None)
            self.emit("MANUAL_PROTECTION_CLOSED", ticker=ticker, fill_id=fid)

    def _stop_candidate(self, ticker, plans):
        """Check every open fill against its own downside trigger on each pass."""
        ledger = self.ledger(ticker)
        armed = ledger.setdefault("stop_armed", {})
        if not plans:
            return None, None
        market = self.client.market(ticker)
        close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        if close.tzinfo is None:
            raise ManualExitBlocked("Market close requires a timezone")
        if market.get("status") not in {"active", "open"} or self.clock() >= close.timestamp():
            return None, market
        candidate = None
        for fid, plan in sorted(plans.items()):
            # Canceling one's TP suppresses automatic TP reposting, but does
            # not silently disable the independently configured stop monitor.
            side = "yes" if plan["sign"] > 0 else "no"
            bid = number(market.get(side + "_bid_dollars"), "stop bid")
            if not 0 <= bid <= 1:
                raise ManualExitBlocked("Stop bid out of bounds")
            entry = number(ledger["managed_fills"][fid]["cost"], "manual entry cost")
            trigger = entry - self.stop_loss_increment
            if fid not in armed and trigger > 0 and bid <= trigger:
                armed[fid] = {"entry": str(entry), "trigger": str(trigger),
                              "armed_at": self.clock()}
                self.save()  # Never forget a crossed stop during cancellation.
                self.emit("MANUAL_STOP_ARMED", ticker=ticker, fill_id=fid,
                          entry=str(entry), trigger=str(trigger), observed_bid=str(bid))
            if fid in armed and candidate is None:
                candidate = (fid, plan, bid)
        self._stop_checked_cycle[ticker] = self._protection_cycles[ticker]
        return candidate, market

    def _submit_stop(self, ticker, fid, plan, bid, market, reserved):
        ledger = self.ledger(ticker)
        if self.clock() < max(ledger.get("retry_after", 0), ledger.get("next_attempt_at", 0)):
            return False
        if not 0 < bid < 1:
            return False  # No executable bid; retry next poll without a blind market order.
        qty = quantity(plan["quantity"] - reserved[fid])
        if not qty:
            return False
        close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp()
        if self.clock() >= close:
            return False
        self.verify_snapshot(ticker)
        snapshot = self._snapshots[ticker]
        if (plan["sign"] * snapshot["held"] <= 0 or
                qty + sum(reserved.values(), D(0)) + snapshot["external_reserved"] > abs(snapshot["held"])):
            raise ManualExitBlocked("Stop exits exceed verified available inventory")
        if self.dry_run:
            self.emit("MANUAL_DRY_RUN_STOP", ticker=ticker, fill_id=fid,
                      quantity=str(qty), observed_bid=str(bid))
            return False
        intent = {"client_id": "manualstop-" + str(uuid.uuid4()), "side": "YES" if plan["sign"] > 0 else "NO",
                  "quantity": str(qty), "target": str(bid), "created_at": self.clock(),
                  "purpose": "manual_stop_loss", "paired": False,
                  "allocations": [{"fill_id": fid, "quantity": str(qty)}]}
        ledger["pending"] = intent
        self.save()  # Must survive a lost POST acknowledgement or restart.
        if self.clock() >= close:
            ledger.pop("pending")
            self.save()
            return False
        try:
            result = self.client.place_manual_stop(ticker, intent)
        except RequestDeferred as error:
            ledger.pop("pending")  # Proves no HTTP dispatch occurred.
            self.transport.defer(ledger, error)
            raise
        except KalshiAPIError as error:
            if error.status_code in {400, 401, 403, 404, 422, 429}:
                ledger.pop("pending")  # Confirmed rejection only.
                self.transport.defer(ledger, error)
            raise
        if not result.get("order_id") or result.get("client_order_id") != intent["client_id"]:
            raise ManualExitBlocked("Stop order acknowledgement identity is missing/mismatched")
        intent["order_id"] = result["order_id"]
        intent["placement_receipt"] = result
        self.save()
        self.emit("MANUAL_STOP_SUBMITTED", ticker=ticker, fill_id=fid,
                  order_id=intent["order_id"], quantity=str(qty), limit=str(bid),
                  execution="reduce_only_ioc", fill_confirmed=False)
        return True

    def maybe_submit(self, ticker, record, automatic_ledger):
        ledger = self.ledger(ticker)
        if ledger.get("window_closed") or self.pending(ticker) or automatic_ledger.get("pending"):
            return False
        plans = {p["fill_id"]: p for p in self._plans.get(ticker, [])}
        active = [i for i in self.intents(ticker).values() if i["status"] in ACTIVE_ORDER_STATES]
        reserved = defaultdict(lambda: D(0))
        for intent in active:
            reserved[intent["fill_id"]] += D(intent["remaining"])
        # Plans exclude external orders' FIFO coverage. Retire overlapping or
        # oversized bot reservations before placing a replacement, including a
        # manual close to zero or reversal. Never cancel a user's own order.
        # The quote is checked for every eligible open fill, even when its
        # entire quantity is already reserved by a take-profit sell.
        stop, stop_market = self._stop_candidate(ticker, plans)
        cancel = []
        for intent in active:
            plan = plans.get(intent["fill_id"])
            target_mismatch = False
            if plan and stop_market and stop_market.get("status") in {"active", "open"}:
                try:
                    expected = round_target(plan["raw_target"],
                                            stop_market.get("price_ranges"), plan["sign"])
                except TargetUnreachable:
                    # Existing exchange order is not blindly replaced by an
                    # arbitrary valid price when the target is out of range.
                    expected = number(intent["target"], "existing target")
                target_mismatch = number(intent["target"], "current target") != expected
            if (not plan or reserved[intent["fill_id"]] > plan["quantity"]
                    or intent["side"] != ("YES" if plan["sign"] > 0 else "NO")
                    or target_mismatch):
                cancel.append(intent)
        if cancel:
            changed = False
            for intent in cancel:
                try:
                    changed = self._cancel(ticker, intent, "inventory_reduced") or changed
                except Exception as error:
                    # _cancel persisted its reservation and retry deadline.
                    # Report this attempt now: suspend will respect that
                    # deadline rather than issuing/logging a second attempt.
                    self.emit("MANUAL_CANCEL_UNCONFIRMED", ticker=ticker,
                              order_id=intent.get("order_id"), **failure_context(error))
                    raise
            return changed
        # Monitor even inventory reserved by +5c sell orders. Release it
        # before submitting a stop; never double-sell a reserved contract.
        if stop:
            fid, plan, bid = stop
            matching = [i for i in active if i["fill_id"] == fid]
            if matching:
                changed = False
                for intent in matching:
                    changed = self._cancel(ticker, intent, "stop_loss") or changed
                return changed
            return self._submit_stop(ticker, fid, plan, bid, stop_market, reserved)
        if self.clock() < ledger.get("retry_after", 0):
            return False
        # No quote/market lookup when every eligible fill is already reserved.
        # Keep all fresh inventory checks for an actual submission unchanged.
        candidates = [(fid, plan) for fid, plan in sorted(plans.items())
                      if fid not in ledger.get("suppressed_fills", {})
                      and fid not in ledger.get("stop_armed", {})
                       and quantity(plan["quantity"] - reserved[fid])]
        if not candidates:
            return False
        market = self.client.market(ticker)
        close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        if close.tzinfo is None:
            raise ManualExitBlocked("Market close requires a timezone")
        expires = int(close.timestamp())
        if market.get("status") not in {"active", "open"} or self.clock() >= expires:
            return False
        for fid, plan in candidates:
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
            external = self._snapshots[ticker]["external_reserved"]
            if held * plan["sign"] <= 0 or qty + sum(reserved.values(), D(0)) + external > abs(held):
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
