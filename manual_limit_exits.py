"""Manual limit-sell execution engine. Not wired to production.

Requires an external reconciler to certify lot ownership, net-position
availability and absence of conflicting bot exit reservations before submit.
Persisted intents prevent duplicate submissions on ambiguous acknowledgements.
"""
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

from co_trading import MANUAL_GROSS_PROFIT_GOAL, gross_profit_limit


class ManualExitBlocked(RuntimeError):
    pass


class ManualLimitExitWorker:
    def __init__(self, client, path, *, profit_goal=MANUAL_GROSS_PROFIT_GOAL):
        self.client = client
        self.path = Path(path)
        self.profit_goal = Decimal(str(profit_goal))
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"orders": {}}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as stream:
            json.dump(self.state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)

    def plan(self, *, fill_id, ticker, side, quantity, entry_price, close_timestamp,
             ownership_verified=False, available_unreserved=None):
        """Prepare a single fill's exit; fail closed without ownership proof.

        available_unreserved must come from an atomic account-wide reconciliation
        that includes bot and manual resting exits, not simply positions().
        """
        if not ownership_verified or available_unreserved is None:
            raise ManualExitBlocked("Account-wide inventory reservation not verified")
        if side not in ("yes", "no") or not fill_id or not ticker:
            raise ValueError("Invalid fill identity")
        qty = Decimal(str(quantity))
        if not qty.is_finite() or qty <= 0 or qty != qty.to_integral_value():
            raise ValueError("Invalid contract quantity")
        if Decimal(str(available_unreserved)) < qty:
            raise ManualExitBlocked("Not enough unreserved owned inventory")
        target = gross_profit_limit(entry_price, qty, self.profit_goal)
        key = str(fill_id)
        existing = self.state["orders"].get(key)
        if existing:
            if any(existing[k] != v for k, v in
                   {"ticker": ticker, "side": side, "quantity": str(qty)}.items()):
                raise ManualExitBlocked("Fill identity changed")
            return existing
        intent = {"ticker": ticker, "side": side, "quantity": str(qty),
                  "target": str(target), "close_timestamp": float(close_timestamp),
                  "client_id": str(uuid.uuid4()), "status": "planned"}
        self.state["orders"][key] = intent
        self.save()
        return intent

    def submit(self, fill_id, *, now, ownership_verified=False, available_unreserved=None):
        """One GTC reduce-only sell. Unknown POST outcomes require reconciliation.

        Never resubmit an intent with unknown acknowledgement automatically.
        """
        intent = self.state["orders"][str(fill_id)]
        if intent["status"] != "planned":
            raise ManualExitBlocked("Order already submitted or acknowledgement unresolved")
        if now >= intent["close_timestamp"]:
            raise ManualExitBlocked("Market already closed")
        if not ownership_verified or available_unreserved is None or (
                Decimal(str(available_unreserved)) < Decimal(intent["quantity"])):
            raise ManualExitBlocked("Ownership/reservations must be reverified before POST")
        intent["status"] = "ack_pending"
        self.save()
        side = intent["side"]
        price = Decimal(intent["target"])
        # GTC limit sell, reduce-only; for NO use YES-denominated bid price.
        response = self.client._order(
            intent["ticker"], "ask" if side == "yes" else "bid",
            Decimal(intent["quantity"]), price if side == "yes" else 1 - price,
            reduce_only=True, ioc=False, expiration_time=int(intent["close_timestamp"]),
            client_order_id=intent["client_id"])
        if not response.get("order_id"):
            raise ManualExitBlocked("Missing order acknowledgement; reconcile before retry")
        intent["order_id"] = response["order_id"]
        intent["status"] = "submitted"
        self.save()
        return intent

    def reconcile(self, fill_id):
        """Resolve ambiguous ACK using matching client ID; never place a second order."""
        intent = self.state["orders"][str(fill_id)]
        matches = [o for o in self.client.all_orders(intent["ticker"])
                   if o.get("client_order_id") == intent["client_id"]]
        if len(matches) != 1:
            raise ManualExitBlocked("Order acknowledgement not uniquely resolved")
        intent["order_id"] = matches[0]["order_id"]
        intent["status"] = "submitted" if matches[0].get("status") not in (
            "executed", "canceled", "cancelled", "rejected") else matches[0]["status"]
        self.save()
        return intent
