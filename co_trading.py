"""Hybrid co-trading: conservative, read-only manual fill classifier.

This module deliberately does not place orders. The existing take-profit worker
owns bot-originated inventory; manual orders must never be added to its ledger.
Before enabling manual exits, a separate durable order/exit ledger and account
position reservations must be integrated and tested.
"""
from decimal import Decimal, InvalidOperation


class AmbiguousManualFill(ValueError):
    pass


def manual_buy_fills(fills, bot_entry_order_ids, bot_exit_order_ids=()):
    """Return non-bot buy fills, failing closed on missing order/fill identities.

    Caller must supply a complete, paginated fill history and complete bot order
    ownership sets. An unknown order is a manual *candidate*, not proof that it
    belongs to a user: external API writers may also create orders.
    """
    bot_ids = set(bot_entry_order_ids) | set(bot_exit_order_ids)
    result = []
    seen = set()
    for fill in fills:
        fid = fill.get("fill_id")
        oid = fill.get("order_id")
        if not fid or not oid or not fill.get("ticker"):
            raise AmbiguousManualFill("Fill identity incomplete")
        if fid in seen:
            continue
        seen.add(fid)
        if oid in bot_ids or fill.get("action") != "buy":
            continue
        if fill.get("side") not in ("yes", "no"):
            raise AmbiguousManualFill("Unknown contract side")
        try:
            qty = Decimal(str(fill.get("count_fp", fill.get("count"))))
            price = Decimal(str(fill.get(fill["side"] + "_price_dollars")))
        except (InvalidOperation, TypeError, ValueError):
            raise AmbiguousManualFill("Unknown fill quantity or price") from None
        if not qty.is_finite() or qty <= 0 or not price.is_finite() or not 0 < price < 1:
            raise AmbiguousManualFill("Invalid fill quantity or price")
        result.append({"fill_id": fid, "order_id": oid, "ticker": fill["ticker"],
                       "side": fill["side"], "quantity": str(qty), "price": str(price)})
    return result


def gross_profit_limit(entry_price, quantity, goal_dollars):
    """Compute a cent-tick take-profit price; reject unreachable targets."""
    from decimal import ROUND_CEILING
    price, qty, goal = map(lambda x: Decimal(str(x)), (entry_price, quantity, goal_dollars))
    if not all(x.is_finite() for x in (price, qty, goal)) or not 0 < price < 1 or qty <= 0 or goal <= 0:
        raise ValueError("Invalid entry or profit goal")
    target = ((price + goal / qty) * 100).to_integral_value(rounding=ROUND_CEILING) / 100
    if target >= 1:
        raise ValueError("Take-profit target not reachable below $1")
    return target
