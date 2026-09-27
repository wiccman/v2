"""Durable, conservative spending reservations shared by every entry route."""
import uuid
from decimal import Decimal as D

FEE_RESERVE = D("0.03")  # per contract, including fractional-fill rounding cushion
ZERO = D("0")
ENTRY_QUANTITY = D("5")
EARLIER_ORDER_BUDGET = D("2.80")  # Per-order ceiling, also constrained by remaining market allowance.
SETTLEMENT_BUDGET = D("6")
SETTLEMENT_PRICE = D("0.97")
SETTLEMENT_KIND = "settlement_97"
SETTLEMENT_WINDOW = 180


def settlement_price_allowed(price):
    """Recognize existing settlement lots, including pre-upgrade 98/99c buys."""
    price = D(str(price))
    return price.is_finite() and SETTLEMENT_PRICE <= price < D("1")


def settlement_entry_price_allowed(price):
    """Trigger at 97c or higher; the submitted buy limit remains exactly 97c."""
    price = D(str(price))
    return price.is_finite() and SETTLEMENT_PRICE <= price < D("1")


def market_budget():
    # Fixed requested allowance; stale Railway budget settings must not keep
    # this release at an older cap.
    return D("21")


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


def remaining_allowance(record, cap, kind):
    cap = min(D(cap), market_budget())
    limit = cap if kind == SETTLEMENT_KIND else min(cap, market_budget() - SETTLEMENT_BUDGET)
    spent = sum((D(item["reserved_dollars"]) for item in record.get("entry_intents", [])), ZERO)
    recovered = sum((D(value) for value in record.get("recycled_exit_orders", {}).values()), ZERO)
    if recovered < ZERO or recovered > spent:
        raise ValueError("Invalid confirmed sale credit")
    spent -= recovered
    return max(ZERO, limit - spent)


def entry_quantity(price, kind, record=None, cap=None, max_quantity=None):
    if kind == SETTLEMENT_KIND:
        quantity = (SETTLEMENT_BUDGET / (D(price) + FEE_RESERVE)).to_integral_value(rounding="ROUND_DOWN")
    else:
        available = EARLIER_ORDER_BUDGET
        if record is not None:
            available = min(available, remaining_allowance(record, market_budget() if cap is None else cap, kind))
        quantity = min(ENTRY_QUANTITY, (available / (D(price) + FEE_RESERVE)).to_integral_value(rounding="ROUND_DOWN"))
    return quantity if max_quantity is None else min(quantity, max(ZERO, D(max_quantity)))


def reserve(record, side, price, order_budget, cap, cancel_at, kind, max_quantity=None):
    initialize(record)
    if record.get("entry_budget_legacy"):
        return None
    # Legacy per-route dollar settings cannot override the shared allocation.
    price, cap = D(price), D(cap)
    if not all(x.is_finite() and x > ZERO for x in (price, cap)) or price >= 1:
        return None
    cap = min(cap, market_budget())
    spent = sum((D(item["reserved_dollars"]) for item in record["entry_intents"]), ZERO)
    recovered = sum((D(value) for value in record.get("recycled_exit_orders", {}).values()), ZERO)
    if recovered < ZERO or recovered > spent:
        raise ValueError("Invalid confirmed sale credit")
    spent -= recovered
    if kind == SETTLEMENT_KIND:
        if price != SETTLEMENT_PRICE or any(i.get("kind") == SETTLEMENT_KIND and attempt_committed(i) for i in record["entry_intents"]):
            return None
    else:
        # Earlier trades cannot consume the settlement budget reserved for settlement.
        cap = min(cap, market_budget() - SETTLEMENT_BUDGET)
    quantity = entry_quantity(price, kind, record, cap, max_quantity=max_quantity)
    if quantity < 1 or quantity * (price + FEE_RESERVE) > cap - spent:
        return None
    intent = dict(client_id=str(uuid.uuid4()), side=side, price=str(price),
                  quantity=str(quantity), reserved_dollars=str(quantity * (price + FEE_RESERVE)),
                  cancel_at=cancel_at, kind=kind, entry_closed=False,
                  reservation_initial_dollars=str(quantity * (price + FEE_RESERVE)))
    record["entry_intents"].append(intent)
    # Keep allowance until terminal fills prove an unfilled remainder or the
    # request is proven unsubmitted. Sales never restore filled-entry allowance.
    return intent


def attempt_committed(intent):
    """Only a proven zero-fill/no-order outcome permits a fresh attempt."""
    if intent.get("release_reason") in {"insufficient_balance", "request_deferred"}:
        return False
    return not (intent.get("reservation_reconciled") is True
                and D(intent.get("confirmed_entry_filled_quantity", "NaN")) == 0)


def reconcile_reservation(intent, order):
    """Release only a terminal order's verified unfilled quantity, never sales."""
    if order.get("status") not in {"executed", "canceled", "expired"}:
        return ZERO
    if not intent.get("order_id") or order.get("order_id") != intent["order_id"]:
        raise ValueError("Entry reservation order identity mismatch")
    filled = D(str(order.get("fill_count_fp", order.get("fill_count", "NaN"))))
    quantity, price = D(intent.get("quantity", "NaN")), D(intent.get("price", "NaN"))
    old = D(intent["reserved_dollars"])
    if not all(x.is_finite() for x in (filled, quantity, price, old)) or not (0 <= filled <= quantity and quantity > 0 and 0 < price < 1):
        raise ValueError("Terminal entry fill quantity is unavailable or invalid")
    fees = []
    for name in ("maker_fees_dollars", "taker_fees_dollars"):
        if order.get(name) is not None:
            fee = D(str(order[name]))
            if not fee.is_finite() or fee < 0:
                raise ValueError("Invalid terminal entry fee")
            fees.append(fee)
    if 0 < filled < quantity and len(fees) != 2:
        raise ValueError("Partial entry fees unavailable; retaining full reservation")
    # Small fractional fills can have rounded fees above the per-contract
    # cushion. Never release money already charged as an entry fee.
    used = max(filled * (price + FEE_RESERVE), filled * price + sum(fees, ZERO))
    original = D(intent.get("reservation_initial_dollars", str(old)))
    intent.update(reservation_initial_dollars=str(original), reserved_dollars=str(used),
                  reservation_released_dollars=str(max(ZERO, original - used)),
                  confirmed_entry_filled_quantity=str(filled), reservation_reconciled=True,
                  entry_closed=True, terminal_status=order["status"])
    return old - used


def release_unsubmitted(intent, reason):
    """Release only an intent proven not to have created an exchange order."""
    if reason not in {'insufficient_balance', 'request_deferred'}:
        raise ValueError('Unverified release reason')
    if intent.get('order_id'):
        raise ValueError('Cannot release an accepted order reservation')
    if 'released_dollars' not in intent:
        intent['released_dollars'] = intent['reserved_dollars']
    intent['reserved_dollars'] = '0'
    intent['entry_closed'] = True
    intent['release_reason'] = reason
