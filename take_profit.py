"""Independent, durable fixed-price exit monitor. No signal or quote dependency.

Kalshi V2 rejects resting reduce-only orders. The worker sends price-protected
reduce-only IOCs for observed holdings and reconciles each submission before
retrying. This is a bot-managed target, not an exchange-hosted resting bracket.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path

from kalshi import KalshiAPIError
from price_pairs import paired_inventory

TERMINAL = {"executed", "canceled", "expired"}


class TakeProfitMonitor:
    def __init__(self, client, read_entries, path, target=Decimal("0.45"),
                 poll=1.0, clock=time.time, emit=None, pairs=None):
        self.client, self.read_entries = client, read_entries
        self.path, self.target = Path(path), Decimal(target)
        self.pairs = pairs
        self.poll, self.clock = max(1.0, float(poll)), clock
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
        delay = 5.0
        retry_after = getattr(error, "retry_after", None)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - self.clock())
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
            order = self.client.order(order_id)
            if order.get("status") not in TERMINAL:
                self.client.cancel(order_id, ticker)
                order = self.client.order(order_id)
            if order.get("status") not in TERMINAL:
                raise RuntimeError(f"Legacy exit cancellation unconfirmed: {order_id}")
            ledger.setdefault("legacy_cleared", []).append(order_id)
            self.save()
            self.emit("TP_LEGACY_RECONCILED", ticker=ticker, order_id=order_id)

    def _reconcile(self, ticker, ledger):
        intent = ledger.get("pending")
        if not intent:
            return
        if intent.get("order_id"):
            order = self.client.order(intent["order_id"])
        else:
            order = next((o for o in self.client.all_orders(ticker)
                          if o.get("client_order_id") == intent["client_id"]), None)
            if not order:
                raise RuntimeError(f"Exit acknowledgement unresolved: {intent['client_id']}")
            intent["order_id"] = order["order_id"]
            self.save()
        filled = Decimal(str(order.get("fill_count_fp", order.get("fill_count", "0"))))
        if filled > Decimal(intent.get("reported_fill", "0")):
            self.emit("TP_FILL", ticker=ticker, order_id=order["order_id"],
                      cumulative_quantity=str(filled), side=intent["side"], target=intent["target"])
            intent["reported_fill"] = str(filled)
            self.save()
        if order.get("status") not in TERMINAL:
            raise RuntimeError(f"Exit still awaiting terminal status: {order['order_id']}")
        if filled:
            ledger.setdefault("exit_orders", {})[order["order_id"]] = {
                "side": intent["side"], "target": intent["target"],
                "paired": intent.get("paired", False), "filled": str(filled)}
        ledger.pop("pending")
        self.save()

    def _paired_buckets(self, ticker, record, ledger, held):
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
            target = self.pairs.get(Decimal(item.get("price", "-1")))
            if order_id and target is not None:
                saved_target = Decimal(item.get("exit_target", str(target)))
                if saved_target != target:
                    raise ValueError("Saved entry target conflicts with configured pair")
                entries[order_id] = {"side": item["side"], "target": str(saved_target)}
        fills = self.client.all_fills(ticker)
        exits = ledger.get("exit_orders", {})
        # An order status can update before its fills endpoint. Do not reuse the
        # inventory until all previously confirmed exits appear in fill history.
        unique = {f.get("fill_id") or f.get("trade_id"): f for f in fills}
        for order_id, order in exits.items():
            observed = sum((Decimal(str(f.get("count_fp", f.get("count", "0"))))
                            for f in unique.values() if f.get("order_id") == order_id), Decimal(0))
            if observed != Decimal(order["filled"]):
                raise ValueError("Confirmed exit is not yet consistent with fill history")
        return paired_inventory(fills, entries, exits, held, ticker)

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
        if self.clock() < ledger.get("retry_after", 0):
            return False
        self._clear_legacy(ticker, record, ledger)
        self._reconcile(ticker, ledger)
        positions = self.client.positions(ticker)
        matches = [p for p in positions if p.get("ticker") == ticker]
        if len(matches) > 1:
            raise RuntimeError("Multiple position rows for ticker; refusing ambiguous exit sizing")
        held = Decimal(str(matches[0]["position_fp"])) if matches else Decimal("0")
        if not held.is_finite():
            raise ValueError("Invalid position quantity")
        buckets = self._paired_buckets(ticker, record, ledger, held) if self.pairs is not None else {self.target: held}
        if held == 0:
            if ledger.pop("armed", None):
                self.save()
                self.emit("TP_POSITION_FLAT", ticker=ticker)
            return True
        side = "YES" if held > 0 else "NO"
        # Rotate across occupied targets: an unfilled low-price IOC cannot
        # starve the other tier. Quantities come from fills, never average cost.
        targets = list(buckets)
        previous = Decimal(ledger.get("last_target", "-1"))
        target = next((t for t in targets if t > previous), targets[0])
        quantity = buckets[target]
        armed = {"side": side, "quantity": str(abs(quantity)), "target": str(target)}
        if ledger.get("armed") != armed:
            ledger["armed"] = armed
            self.save()
            self.emit("TP_ARMED", ticker=ticker, **armed, execution="reduce_only_ioc")
        # The exchange, rather than a potentially stale quote, tests the limit.
        # One net-position exit covers all entry routes without double allocation.
        intent = {**armed, "client_id": str(uuid.uuid4()), "created_at": self.clock(),
                  "paired": self.pairs is not None}
        ledger["pending"] = intent
        ledger["last_target"] = str(target)
        self.save()  # No submission unless its recovery ID is durable.
        try:
            result = self.client.place_take_profit(ticker, quantity, target, close,
                                                  client_order_id=intent["client_id"])
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
        self.save()
        self.emit("TP_SUBMITTED", ticker=ticker, order_id=result["order_id"], **armed,
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
                ok = self._market(ticker, record, ledger) and ok
            except Exception as error:
                ok = False
                # Includes rate limits on GET/order reconciliation, not just POST.
                if isinstance(error, KalshiAPIError):
                    self.defer(ledger, error)
                self.error(ticker, error)
        self._healthy = ok
        if ok:
            self._last_success = self.clock()
