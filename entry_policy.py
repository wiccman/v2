"""Durable, conservative spending reservations shared by every entry route."""
import uuid
from decimal import Decimal as D, ROUND_DOWN

FEE_RESERVE = D("0.03")  # per contract, including fractional-fill rounding cushion
ZERO = D("0")


def market_budget():
    # Fixed requested allowance; stale Railway budget settings must not keep
    # this release at the previous $6 cap.
    return D("10")


def initialize(record):
    if "entry_intents" in record:
        return
    # Previous versions did not retain a complete spending ledger. Never assume
    # that a restarted, already traded market has its full allowance available.
    record["entry_budget_legacy"] = bool(
        record.get("buys") or record.get("orders") or record.get("dual_limit_attempted")
        or record.get("spot_entry_attempted") or record.get("final_entry_attempted")
        or record.get("historical_triggered_strikes") or record.get("historical_strike_orders")
        or record.get("dual_limit_orders")
    )
    record["entry_intents"] = []


def reserve(record, side, price, order_budget, cap, cancel_at, kind):
    initialize(record)
    if record.get("entry_budget_legacy"):
        return None
    price, order_budget, cap = D(price), D(order_budget), D(cap)
    if not all(x.is_finite() and x > ZERO for x in (price, order_budget, cap)) or price >= 1:
        return None
    spent = sum((D(item["reserved_dollars"]) for item in record["entry_intents"]), ZERO)
    quantity = min(order_budget / price, max(ZERO, cap - spent) / (price + FEE_RESERVE))
    quantity = quantity.quantize(D("0.01"), rounding=ROUND_DOWN)
    if quantity <= ZERO:
        return None
    intent = dict(client_id=str(uuid.uuid4()), side=side, price=str(price),
                  quantity=str(quantity), reserved_dollars=str(quantity * (price + FEE_RESERVE)),
                  cancel_at=cancel_at, kind=kind, entry_closed=False)
    record["entry_intents"].append(intent)
    # Reservations remain spent even after cancel/reject/exit: never recycle the
    # same allowance or overspend after an ambiguous POST or process restart.
    return intent
