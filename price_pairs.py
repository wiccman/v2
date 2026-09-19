"""Entry limits and their durable take-profit targets (outcome cents)."""
from decimal import Decimal as D


def parse_pairs(value="32:39,39:46"):
    pairs = {}
    for item in value.split(","):
        fields = item.strip().split(":")
        if len(fields) != 2:
            raise ValueError("ENTRY_EXIT_PAIRS_CENTS must look like 32:39,39:46")
        entry, target = (D(x.strip()) for x in fields)
        if not all(x.is_finite() and x == x.to_integral_value() and 1 <= x <= 99
                   for x in (entry, target)) or target <= entry:
            raise ValueError("Each pair needs whole cents 1..99 and exit above entry")
        entry, target = entry / 100, target / 100
        if entry in pairs:
            raise ValueError("Entry prices must be unique")
        pairs[entry] = target
    return dict(sorted(pairs.items()))


def paired_inventory(fills, entry_orders, exit_orders, held, ticker):
    """Replay fills into target lots; verify them against exchange net holdings.

    Bot exits consume their assigned target. Other reductions (manual sales and
    opposite-side buys) consume oldest inventory first. Unknown new inventory
    remains unmanaged. No average-cost target or inferred fill is used.
    """
    from datetime import datetime

    seen, events, directions = {}, [], {}
    for fill in fills:
        if (fill.get("ticker") or fill.get("market_ticker")) != ticker:
            raise ValueError("Fill ticker missing or mismatched")
        if fill.get("subaccount_number", 0) != 0:
            raise ValueError("Non-primary subaccount in fill history")
        key = fill.get("fill_id") or fill.get("trade_id")
        if not key:
            raise ValueError("Fill ID unavailable")
        if key in seen:
            if seen[key] != fill:
                raise ValueError("Conflicting duplicate fill")
            continue
        seen[key] = fill
        quantity = D(str(fill.get("count_fp", fill.get("count", "NaN"))))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("Invalid fill quantity")
        # Kalshi's REST payload has used both lowercase and uppercase enums.
        # Normalize before deriving the net YES-position direction.
        book_side = str(fill.get("book_side", "")).lower()
        side = str(fill.get("outcome_side") or fill.get("side") or "").lower()
        action = str(fill.get("action", "")).lower()
        outcome_sign = {"yes": 1, "no": -1}.get(side)
        action_sign = {"buy": 1, "sell": -1}.get(action)
        legacy_sign = outcome_sign * action_sign if outcome_sign and action_sign else None
        sign = {"bid": 1, "ask": -1}.get(book_side, legacy_sign)
        if sign is None or (legacy_sign is not None and legacy_sign != sign):
            fields = {name: fill.get(name) for name in ("book_side", "outcome_side", "side", "action", "order_id") if name in fill}
            raise ValueError(f"Fill direction unavailable or contradictory: {fields}")
        if fill.get("created_time"):
            stamp = datetime.fromisoformat(fill["created_time"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("Fill timestamp needs a timezone")
            timestamp = D(str(stamp.timestamp()))
        elif fill.get("ts") is not None:
            timestamp = D(str(fill["ts"]))
        else:
            raise ValueError("Fill timestamp unavailable")
        if not timestamp.is_finite():
            raise ValueError("Invalid fill timestamp")
        directions.setdefault(timestamp, set()).add(sign)
        if len(directions[timestamp]) > 1:
            raise ValueError("Fill ordering is ambiguous at a shared timestamp")
        events.append((timestamp, key, fill.get("order_id"), sign, quantity))

    lots = []
    for _, key, order_id, sign, quantity in sorted(events):
        entry = entry_orders.get(order_id)
        exit_order = exit_orders.get(order_id)
        if entry and sign != (1 if entry["side"] == "YES" else -1):
            raise ValueError("Entry fill direction does not match its saved intent")
        if exit_order and sign != (-1 if exit_order["side"] == "YES" else 1):
            raise ValueError("Exit fill direction does not match its saved intent")
        # A known paired exit can reduce only that target's remaining lots.
        # Retired global-target orders are treated as ordinary FIFO reductions.
        targeted = exit_order and exit_order.get("paired")
        for lot in lots:
            if lot["sign"] == sign or lot["quantity"] == 0:
                continue
            if targeted and lot["target"] != D(exit_order["target"]):
                continue
            reduced = min(quantity, lot["quantity"])
            lot["quantity"] -= reduced
            quantity -= reduced
            if not quantity:
                break
        if quantity:
            if exit_order:
                raise ValueError("Exit fill exceeds its attributable inventory")
            target = D(entry["target"]) if entry else None
            lots.append(dict(fill_id=key, sign=sign, quantity=quantity, target=target))
    lots = [lot for lot in lots if lot["quantity"]]
    reconstructed = sum((lot["quantity"] * lot["sign"] for lot in lots), D(0))
    if reconstructed != D(held):
        raise ValueError("Fills and position disagree; waiting for consistent exchange data")
    if any(lot["target"] is None for lot in lots):
        raise ValueError("Untracked inventory has no verified paired exit target")
    buckets = {}
    for lot in lots:
        buckets[lot["target"]] = buckets.get(lot["target"], D(0)) + lot["quantity"] * lot["sign"]
    return dict(sorted(buckets.items()))

