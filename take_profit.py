from entry_policy import SETTLEMENT_KIND, SETTLEMENT_WINDOW, settlement_price_allowed, settlement_entry_price_allowed, attempt_committed
"""Independent, durable exit monitor and authorized settlement-side transition.

Kalshi V2 rejects resting reduce-only orders. The worker sends price-protected
reduce-only IOCs for observed holdings and reconciles each submission before
retrying. This is a bot-managed target, not an exchange-hosted resting bracket.
Normal take-profit orders do not depend on quotes. A requested settlement-side
transition uses the observed bid to close opposite inventory, even at a loss.
"""
import json
import hashlib
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from kalshi import KalshiAPIError, terminal_ioc_receipt
from request_coordinator import RequestDeferred, retry_delay
from price_pairs import paired_inventory, fill_cost_inventory, order_profit_inventory, order_percentage_inventory, owned_position, InventorySyncError
from price_pairs import order_increment_inventory

TERMINAL = {"executed", "canceled", "expired"}


def entry_fingerprint(record):
    """Bind an ownership receipt to the entry ledger it actually verified."""
    return hashlib.sha256(json.dumps(record.get("entry_intents", []),
                                    sort_keys=True).encode()).hexdigest()


class TakeProfitMonitor:
    def __init__(self, client, read_entries, path, target=Decimal("0.45"),
                 poll=1.0, clock=time.time, emit=None, pairs=None, fill_cost_targets=False,
                 per_order_profit=None, no_fill_pause=0.0, quote_gate=False,
                 per_order_percentage=None, per_order_increment=None, force_exit_price=None):
        self.client, self.read_entries = client, read_entries
        self.path, self.target = Path(path), Decimal(target)
        self.pairs = pairs
        self.fill_cost_targets = fill_cost_targets
        self.per_order_profit = Decimal(per_order_profit) if per_order_profit is not None else None
        self.per_order_percentage = Decimal(per_order_percentage) if per_order_percentage is not None else None
        self.per_order_increment = Decimal(per_order_increment) if per_order_increment is not None else None
        self.force_exit_price = Decimal(force_exit_price) if force_exit_price is not None else None
        if self.force_exit_price is not None and (not self.force_exit_price.is_finite()
                                                 or not 0 < self.force_exit_price < 1):
            raise ValueError("Forced exit price must be between zero and one")
        self.poll, self.clock = max(1.0, float(poll)), clock
        self.no_fill_pause = max(0.0, float(no_fill_pause))
        self.quote_gate = quote_gate
        self.emit = emit or (lambda event, **data: print(json.dumps({
            "event": event, "time_utc": datetime.now(timezone.utc).isoformat(), **data
        }), flush=True))
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"markets": {}}
        self._healthy = False
        self._last_success = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._errors = {}

    @property
    def healthy(self):
        return self._healthy and self.clock() - self._last_success <= max(10, 3 * self.poll)

    def settlement_ready(self, ticker, side):
        """Read an atomic receipt, never mutable state halfway through a pass."""
        if not self.healthy or not self.path.exists():
            return False
        ledger = json.loads(self.path.read_text()).get("markets", {}).get(ticker, {})
        ready = ledger.get("settlement_ready", {})
        return (ready.get("side") == side and not ledger.get("pending")
                and 0 <= self.clock() - ready.get("checked_at", 0) <= max(10, 3 * self.poll)
                and self.clock() < ready.get("close_timestamp", 0))

    def bot_inventory(self, ticker, record, account_held):
        """Use the exit worker's atomic, recent fill reconciliation for sizing.

        No additional fill-history requests are added to the entry loop. A
        changed position, entry ledger, or unresolved exit requires a new pass.
        """
        if not self.healthy or self.pairs is None or not self.path.exists():
            raise InventorySyncError("Ownership monitor not ready")
        ledger = json.loads(self.path.read_text()).get("markets", {}).get(ticker, {})
        receipt = ledger.get("ownership", {})
        if (ledger.get("pending") or not receipt
                or not 0 <= self.clock() - receipt["checked_at"] <= max(10, 3 * self.poll)
                or receipt["entry_fingerprint"] != entry_fingerprint(record)
                or Decimal(receipt["account_held"]) != account_held):
            raise InventorySyncError("Waiting for current bot inventory reconciliation")
        return Decimal(receipt["bot_held"])

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            json.dump(self.state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    def wake(self):
        self._wake.set()

    def start(self):
        self._thread = threading.Thread(target=self.run, name="take-profit", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=6)

    def error(self, ticker, error):
        message = repr(error)
        previous = self._errors.get(ticker)
        if not previous or previous[0] != message or self.clock() - previous[1] >= 30:
            self.emit("TP_ERROR", ticker=ticker, error=message, new_entries="paused")
            self._errors[ticker] = (message, self.clock())

    def defer(self, ledger, error):
        delay = max(self.poll, retry_delay(getattr(error, "retry_after", None), 5.0, self.clock()))
        ledger["retry_after"] = self.clock() + delay
        self.save()

    def run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.run_once()
            except Exception as error:
                self._healthy = False
                self.error("", error)
            self._wake.wait(self.poll)

    def _clear_legacy(self, ticker, record, ledger):
        ids = {record.get("take_profit_order_id"), record.get("historical_take_profit_order_id")}
        ids.update(o.get("order_id") for o in record.get("historical_take_profit_orders", []))
        for order_id in sorted(ids - {None, ""} - set(ledger.get("legacy_cleared", []))):
            order = self.client.order(order_id, ticker)
            if order.get("status") not in TERMINAL:
                self.client.cancel(order_id, ticker)
                order = self.client.order(order_id, ticker)
            if order.get("status") not in TERMINAL:
                raise RuntimeError(f"Legacy exit cancellation unconfirmed: {order_id}")
            ledger.setdefault("legacy_cleared", []).append(order_id)
            self.save()
            self.emit("TP_LEGACY_RECONCILED", ticker=ticker, order_id=order_id)

    def _pending_status(self, ticker, intent, reason):
        attempts = min(int(intent.get("status_attempts", 0)) + 1, 4)
        delay = min(15.0, 2.0 ** attempts)
        intent.update(status_attempts=attempts, next_status_at=self.clock() + delay)
        self.save()
        self.emit("TP_STATUS_PENDING", ticker=ticker, order_id=intent.get("order_id"),
                  client_id=intent["client_id"], reason=reason, retry_seconds=delay,
                  new_entries="paused")
        return False

    def _reconcile(self, ticker, ledger):
        intent = ledger.get("pending")
        if not intent:
            return True
        if self.clock() < intent.get("next_status_at", 0):
            return False
        order = terminal_ioc_receipt(intent)
        if order is not None:
            pass  # Durable matching-engine result; no read-model lookup needed.
        elif intent.get("order_id"):
            try:
                order = self.client.order(intent["order_id"], ticker)
            except KalshiAPIError as error:
                if error.status_code != 404:
                    raise
                # The single-order AND aggregate lookup have not resolved the
                # ID. Preserve it across restarts; absence is not cancellation.
                return self._pending_status(ticker, intent, "order_not_visible")
        else:
            order = next((o for o in self.client.all_orders(ticker)
                          if o.get("client_order_id") == intent["client_id"]), None)
            if not order:
                return self._pending_status(ticker, intent, "acknowledgement_unresolved")
            intent["order_id"] = order["order_id"]
            self.save()
        filled = Decimal(str(order.get("fill_count_fp", order.get("fill_count", "0"))))
        if filled > Decimal(intent.get("reported_fill", "0")):
            event = "SETTLEMENT_CLOSE_FILL" if intent.get("purpose") == "settlement_switch" else "TP_FILL"
            self.emit(event, ticker=ticker, order_id=order["order_id"],
                      cumulative_quantity=str(filled), side=intent["side"], target=intent["target"])
            intent["reported_fill"] = str(filled)
            self.save()
        if order.get("status") not in TERMINAL:
            return self._pending_status(ticker, intent, "awaiting_terminal_status")
        if filled:
            ledger.pop("next_attempt_at", None)
            ledger.setdefault("exit_orders", {})[order["order_id"]] = {
                "side": intent["side"], "target": intent["target"],
                "paired": intent.get("paired", False), "filled": str(filled),
                "purpose": intent.get("purpose", "take_profit")}
            if "allocations" in intent:
                ledger["exit_orders"][order["order_id"]]["allocations"] = intent["allocations"]
        elif intent.get("purpose") != "settlement_switch" and self.no_fill_pause:
            # A known unfilled IOC cannot help until the book changes. Avoid
            # repeating the same read/reconcile/write cycle every second.
            ledger["next_attempt_at"] = self.clock() + self.no_fill_pause
        ledger.pop("pending")
        self.save()
        return True

    def _paired_buckets(self, ticker, record, ledger, held):
        fills = self.client.all_fills(ticker)
        # An entry can land while the exchange reads are in flight. Its intent
        # was persisted before POST; refresh that snapshot before attribution.
        record = self.read_entries().get("markets", {}).get(ticker, record)
        by_client = ledger.setdefault("entry_order_ids", {})
        missing = [i for i in record.get("entry_intents", [])
                   if not i.get("order_id") and not i.get("entry_closed")
                   and i.get("client_id") not in by_client]
        if missing:
            remote = {o.get("client_order_id"): o for o in self.client.all_orders(ticker)}
            for item in missing:
                found = remote.get(item["client_id"])
                if found:
                    by_client[item["client_id"]] = found["order_id"]
            self.save()
        entries = {}
        for item in record.get("entry_intents", []):
            order_id = item.get("order_id") or by_client.get(item.get("client_id"))
            price = Decimal(item.get("price", "-1"))
            target = self.pairs.get(price)
            if item.get("hold_to_settlement") is True:
                if not settlement_price_allowed(price) or Decimal(item.get("exit_target", "-1")) != 1:
                    raise ValueError("Invalid settlement inventory target")
                target = Decimal("1")
            # Preserve the original target for inventory from the retired tier.
            # New entries use only the current pairs.
            if target is None:
                target = {Decimal("0.25"): Decimal("0.31"),
                          Decimal("0.32"): Decimal("0.39")}.get(price)
            if order_id and target is None and any(f.get("order_id") == order_id for f in fills):
                raise ValueError("Known entry fill has no configured exit target")
            if order_id and target is not None:
                saved_target = Decimal(item.get("exit_target", str(target)))
                previous_target = {Decimal("0.70"): Decimal("0.80"),
                                   Decimal("0.73"): Decimal("0.81"),
                                   Decimal("0.85"): Decimal("0.92")}.get(price)
                if saved_target != target and not (previous_target == saved_target and
                    item.get("entry_execution_version", 3) < 4):
                    raise ValueError("Saved entry target conflicts with configured pair")
                entries[order_id] = {"side": item["side"], "target": str(saved_target),
                                     "price": str(price),
                                     "fixed_exit_target": item.get("fixed_exit_target") is True}
        exits = ledger.get("exit_orders", {})
        # An order status can update before its fills endpoint. Do not reuse the
        # inventory until all previously confirmed exits appear in fill history.
        unique = {f.get("fill_id") or f.get("trade_id"): f for f in fills}
        for item in record.get("entry_intents", []):
            order_id = item.get("order_id") or by_client.get(item.get("client_id"))
            receipt = terminal_ioc_receipt(item)
            confirmed = (receipt["fill_count_fp"] if receipt is not None
                         else item.get("confirmed_entry_filled_quantity"))
            if order_id and confirmed is not None:
                observed = sum((Decimal(str(f.get("count_fp", f.get("count", "0"))))
                                for f in unique.values() if f.get("order_id") == order_id), Decimal(0))
                if observed < Decimal(confirmed):
                    raise InventorySyncError("Confirmed entry is not yet visible in fill history")
        for order_id, order in exits.items():
            observed = sum((Decimal(str(f.get("count_fp", f.get("count", "0"))))
                            for f in unique.values() if f.get("order_id") == order_id), Decimal(0))
            if observed != Decimal(order["filled"]):
                raise InventorySyncError("Confirmed exit is not yet consistent with fill history")
        outside = []
        if self.per_order_increment is not None:
            result = order_increment_inventory(fills, entries, exits, held, ticker, outside,
                                               self.per_order_increment)
        elif self.per_order_percentage is not None:
            result = order_percentage_inventory(fills, entries, exits, held, ticker, outside,
                                                self.per_order_percentage)
        elif self.per_order_profit is not None:
            result = order_profit_inventory(fills, entries, exits, held, ticker,
                                            self.per_order_profit)
        elif self.fill_cost_targets:
            result = fill_cost_inventory(fills, entries, exits, held, ticker, outside)
        else:
            result = paired_inventory(fills, entries, exits, held, ticker, outside), {}
        if outside and any(i.get("client_id") not in by_client for i in missing):
            # A bot POST with a lost ACK could explain these lots. Resolve its
            # identity before classifying inventory as belonging to others.
            raise InventorySyncError("Entry acknowledgement unresolved; inventory ownership pending")
        if ledger.get("outside_inventory", []) != outside:
            ledger["outside_inventory"] = outside
            self.save()
            self.emit("TP_OUTSIDE_INVENTORY", ticker=ticker, lots=outside,
                      action="excluded_from_take_profit", position_verified=True)
        bot_held, allocations = owned_position(fills, entries, exits, held, ticker)
        ledger["ownership"] = {
            "checked_at": self.clock(), "entry_fingerprint": entry_fingerprint(record),
            "account_held": str(held), "bot_held": str(bot_held),
            "outside_held": str(held - bot_held), "allocations": allocations,
        }
        self.save()
        return result

    def _market(self, ticker, record, ledger):
        close = record.get("close_timestamp")
        if close is None and record.get("entry_cancel_at") is not None:
            close = float(record["entry_cancel_at"]) + 540
        if close is None:
            close = ledger.get("close_timestamp")
        if close is None:
            market = self.client.market(ticker)
            close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp()
            ledger["close_timestamp"] = close
            self.save()
        if self.clock() >= float(close):
            # An unknown acknowledgement remains recorded for audit. Never send
            # new exits to an expired market or label unsettled inventory sold.
            if ledger.get("armed") and not ledger.get("close_reported"):
                ledger["close_reported"] = True
                self.save()
                self.emit("TP_WINDOW_CLOSED", ticker=ticker, fill_confirmed=False)
            return True
        if ledger.pop("settlement_ready", None):
            self.save()
        if self.clock() < ledger.get("retry_after", 0):
            return False
        switch = record.get("settlement_switch", {})
        urgent_switch = (switch.get("allow_loss") is True and
                         switch.get("phase") in {"requested", "ready"})
        if not urgent_switch and self.clock() < ledger.get("next_attempt_at", 0) and not ledger.get("pending"):
            return True
        self._clear_legacy(ticker, record, ledger)
        if not self._reconcile(ticker, ledger):
            return False
        if not urgent_switch and self.clock() < ledger.get("next_attempt_at", 0):
            return True
        positions = self.client.positions(ticker)
        matches = [p for p in positions if p.get("ticker") == ticker]
        if len(matches) > 1:
            raise RuntimeError("Multiple position rows for ticker; refusing ambiguous exit sizing")
        held = Decimal(str(matches[0]["position_fp"])) if matches else Decimal("0")
        if not held.is_finite():
            raise ValueError("Invalid position quantity")
        buckets, plans = (self._paired_buckets(ticker, record, ledger, held)
                          if self.pairs is not None else ({self.target: held}, {}))
        market = None
        bot_held = Decimal(ledger["ownership"]["bot_held"]) if self.pairs is not None else held
        if self.force_exit_price is not None and bot_held:
            market = self.client.market(ticker)
            side = "yes" if bot_held > 0 else "no"
            bid = Decimal(str(market[side + "_bid_dollars"]))
            if not bid.is_finite() or not 0 <= bid <= 1:
                raise ValueError("Invalid forced-exit bid")
            if bid >= self.force_exit_price:
                # Override every target, including hold-to-settlement, using
                # verified bot allocations rather than the shared position.
                plan = ({"allocations": ledger["ownership"]["allocations"], "cost_groups": []}
                        if self.pairs is not None else None)
                return self._submit(ticker, ledger, close, bot_held, self.force_exit_price,
                                    paired=False, plan=plan)
        switch = record.get("settlement_switch", {})
        switching = (switch.get("side") in {"YES", "NO"} and switch.get("allow_loss") is True
                     and not any(item.get("kind") == SETTLEMENT_KIND and attempt_committed(item) for item in record.get("entry_intents", []))
                     and float(close) - SETTLEMENT_WINDOW <= self.clock() < float(close))
        if switching:
            # Entry worker cancels/reconciles buys. Unknown ACKs remain a block
            # so an old buy cannot refill the position after this close.
            if any(not item.get("entry_closed") and item.get("kind") != SETTLEMENT_KIND
                   for item in record.get("entry_intents", [])):
                return False
            opposite = (switch["side"] == "YES" and held < 0) or (switch["side"] == "NO" and held > 0)
            if opposite:
                if self.pairs is not None and Decimal(ledger["ownership"]["outside_held"]):
                    # Opposing buys would net the manual position. Leave both
                    # the manual lots and bot lots intact until it is removed.
                    if not ledger.get("manual_switch_blocked"):
                        ledger["manual_switch_blocked"] = True
                        self.save()
                        self.emit("SETTLEMENT_MANUAL_POSITION_WAIT", ticker=ticker,
                                  outside_held=ledger["ownership"]["outside_held"],
                                  action="manual_inventory_preserved")
                    return True
                market = self.client.market(ticker)
                if not settlement_entry_price_allowed(market[switch["side"].lower() + "_ask_dollars"]):
                    return False
                held_side = "yes" if held > 0 else "no"
                bid = Decimal(str(market[held_side + "_bid_dollars"]))
                if not bid.is_finite() or not Decimal("0") < bid < Decimal("1"):
                    return False
                # Close only verified bot lots, at the observed bid. Persist
                # exact allocations so a restart cannot reassign manual lots.
                plan = ({"allocations": ledger["ownership"]["allocations"], "cost_groups": []}
                        if self.pairs is not None else None)
                return self._submit(ticker, ledger, close, held, bid,
                                    paired=False, purpose="settlement_switch", plan=plan)
        if switching:
            ledger["settlement_ready"] = {"side": switch["side"], "checked_at": self.clock(),
                                           "close_timestamp": float(close)}
            self.save()
        if held == 0:
            if ledger.pop("armed", None):
                self.save()
                self.emit("TP_POSITION_FLAT", ticker=ticker)
            return True
        # Rotate across occupied targets: an unfilled low-price IOC cannot
        # starve the other tier. Quantities always come from verified fills.
        # A target of $1 is reserved for held settlement inventory, never an IOC exit.
        targets = [target for target in buckets if target < Decimal("1")]
        if not targets:
            if ledger.pop("armed", None):
                self.save()
            return True
        previous = Decimal(ledger.get("last_target", "-1"))
        target = next((t for t in targets if t > previous), targets[0])
        quantity = buckets[target]
        if self.quote_gate:
            # A sell cannot fill at its target while the best bid is lower.
            # This read replaces futile order writes and reconciliation reads.
            market = market if market is not None else self.client.market(ticker)
            side = "yes" if quantity > 0 else "no"
            bid = Decimal(str(market[side + "_bid_dollars"]))
            if not bid.is_finite() or bid < 0 or bid > 1:
                raise ValueError("Invalid take-profit bid")
            if bid < target:
                ledger["last_target"] = str(target)
                return True
        return self._submit(ticker, ledger, close, quantity, target,
                            paired=self.pairs is not None, plan=plans.get(target))

    def _submit(self, ticker, ledger, close, quantity, target, *, paired, purpose="take_profit", plan=None):
        if self.clock() >= float(close):
            return False
        side = "YES" if quantity > 0 else "NO"
        prefix = "SETTLEMENT_CLOSE" if purpose == "settlement_switch" else "TP"
        armed = {"side": side, "quantity": str(abs(quantity)), "target": str(target)}
        if plan is not None:
            allocated = sum((Decimal(item["quantity"]) for item in plan["allocations"]), Decimal(0))
            if allocated != abs(quantity):
                raise ValueError("Exit allocation does not cover its requested quantity")
            armed["cost_groups"] = plan["cost_groups"]
        if ledger.get("armed") != armed:
            ledger["armed"] = armed
            self.save()
            self.emit(prefix + "_ARMED", ticker=ticker, **armed, execution="reduce_only_ioc")
        # The exchange, rather than a potentially stale quote, tests the limit.
        # One net-position exit covers all entry routes without double allocation.
        intent = {**armed, "client_id": str(uuid.uuid4()), "created_at": self.clock(),
                  "paired": paired, "purpose": purpose}
        if plan is not None:
            intent["allocations"] = plan["allocations"]
        ledger["pending"] = intent
        ledger["last_target"] = str(target)
        self.save()  # No submission unless its recovery ID is durable.
        if self.clock() >= float(close):
            ledger.pop("pending")  # Deadline passed during save; no POST was sent.
            self.save()
            return False
        try:
            result = self.client.place_take_profit(ticker, quantity, target, close,
                                                  client_order_id=intent["client_id"])
        except RequestDeferred as error:
            ledger.pop("pending")  # Local rejection: no HTTP request was sent.
            self.defer(ledger, error)
            raise
        except KalshiAPIError as error:
            # These responses definitively rejected the order. Timeouts, 409,
            # and server errors retain the intent for read-only recovery.
            if error.status_code in {400, 401, 403, 404, 422, 429}:
                ledger.pop("pending")
                self.defer(ledger, error)
            raise
        if not result.get("order_id"):
            raise RuntimeError("Exit response missing order ID; saved for reconciliation")
        intent["order_id"] = result["order_id"]
        intent["placement_receipt"] = result
        self.save()
        self.emit(prefix + "_SUBMITTED", ticker=ticker, order_id=result["order_id"], **armed,
                  execution="reduce_only_ioc", fill_confirmed=False)
        return True

    def run_once(self):
        entries = self.read_entries()  # Atomic entry-state snapshot; never mutate it.
        ok = True
        for ticker, record in entries.get("markets", {}).items():
            if self._stop.is_set():
                ok = False
                break
            if ticker in entries.get("mm", {}).get("markets", {}):
                continue
            ledger = self.state["markets"].setdefault(ticker, {})
            try:
                try:
                    market_ok = self._market(ticker, record, ledger)
                except InventorySyncError:
                    # Retry once before failing closed; no exit was submitted
                    # from the inconsistent snapshot.
                    fresh = self.read_entries().get("markets", {}).get(ticker, record)
                    market_ok = self._market(ticker, fresh, ledger)
                ok = market_ok and ok
            except Exception as error:
                ok = False
                # Includes rate limits on GET/order reconciliation, not just POST.
                if isinstance(error, (KalshiAPIError, RequestDeferred)):
                    self.defer(ledger, error)
                self.error(ticker, error)
        self._healthy = ok
        if ok:
            self._last_success = self.clock()
