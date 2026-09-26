"""Durable, conservative spending reservations shared by every entry route."""
import uuid
from decimal import Decimal as D

FEE_RESERVE = D("0.03")  # per contract, including fractional-fill rounding cushion
ZERO = D("0")
ENTRY_QUANTITY = D("5")
SETTLEMENT_BUDGET = D("10")
SETTLEMENT_PRICE = D("0.97")
SETTLEMENT_KIND = "settlement_97"


def market_budget():
    # Fixed requested allowance; stale Railway budget settings must not keep
    # this release at an older cap.
    return D("25")


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


def entry_quantity(price, kind):
    if kind == SETTLEMENT_KIND:
        return (SETTLEMENT_BUDGET / (D(price) + FEE_RESERVE)).to_integral_value(rounding="ROUND_DOWN")
    return ENTRY_QUANTITY


def reserve(record, side, price, order_budget, cap, cancel_at, kind):
    initialize(record)
    if record.get("entry_budget_legacy"):
        return None
    # order_budget is retained for caller compatibility; dollar-based sizing
    # and tier splitting no longer control order quantity.
    price, cap = D(price), D(cap)
    if not all(x.is_finite() and x > ZERO for x in (price, cap)) or price >= 1:
        return None
    spent = sum((D(item["reserved_dollars"]) for item in record["entry_intents"]), ZERO)
    if kind == SETTLEMENT_KIND:
        if price != SETTLEMENT_PRICE or any(i.get("kind") == SETTLEMENT_KIND for i in record["entry_intents"]):
            return None
    else:
        # Earlier trades cannot consume the ten dollars reserved for settlement.
        cap = min(cap, market_budget() - SETTLEMENT_BUDGET)
    quantity = entry_quantity(price, kind)
    # Never shrink the requested five contracts to fit leftover allowance.
    if quantity * (price + FEE_RESERVE) > cap - spent:
        return None
    intent = dict(client_id=str(uuid.uuid4()), side=side, price=str(price),
                  quantity=str(quantity), reserved_dollars=str(quantity * (price + FEE_RESERVE)),
                  cancel_at=cancel_at, kind=kind, entry_closed=False)
    record["entry_intents"].append(intent)
    # Accepted/ambiguous orders retain allowance after cancel, exit or restart.
    # Only a proven no-order outcome can explicitly release its reservation.
    return intent


def release_unsubmitted(intent, reason):
    """Release only an intent proven not to have created an exchange order."""
    if reason != 'insufficient_balance':
        raise ValueError('Unverified release reason')
    if intent.get('order_id'):
        raise ValueError('Cannot release an accepted order reservation')
    if 'released_dollars' not in intent:
        intent['released_dollars'] = intent['reserved_dollars']
    intent['reserved_dollars'] = '0'
    intent['entry_closed'] = True
    intent['release_reason'] = reason
