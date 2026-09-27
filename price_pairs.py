"""Entry limits and their durable take-profit targets (outcome cents)."""
from decimal import Decimal as D, ROUND_CEILING
from entry_policy import FEE_RESERVE


def parse_pairs(value="39:46"):
    pairs = {}
    for item in value.split(","):
        fields = item.strip().split(":")
        if len(fields) != 2:
            raise ValueError("ENTRY_EXIT_PAIRS_CENTS must look like 39:46")
        entry, target = (D(x.strip()) for x in fields)
        if not all(x.is_finite() and x == x.to_integral_value() and 1 <= x <= 99
                   for x in (entry, target)) or target <= entry:
            raise ValueError("Each pair needs whole cents 1..99 and exit above entry")
        entry, target = entry / 100, target / 100
        if entry in pairs:
            raise ValueError("Entry prices must be unique")
        pairs[entry] = target
    return dict(sorted(pairs.items()))


class InventorySyncError(ValueError):
    """Independent exchange snapshots have not converged yet."""


def _remaining_lots(fills, entry_orders, exit_orders, held, ticker, untracked=None):
    """Replay verified fills, retaining the identity of every unsold entry lot.

    New exits consume their saved fill allocations; historical paired exits
    consume their original fixed target. Manual reductions use FIFO. Neither
    a changed average nor a restart can reassign an already submitted exit.
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
        # `book_side` is the side of this account's order and maps directly to
        # net YES inventory: bid adds YES inventory, ask adds NO inventory.
        # The outcome/action fields describe the contract trade but are not a
        # reliable substitute for that mapping, especially for NO contracts.
        book_side = str(fill.get("book_side", "")).lower()
        sign = {"bid": 1, "ask": -1}.get(book_side)
        if sign is None:
            fields = {name: fill.get(name) for name in ("book_side", "outcome_side", "side", "action", "order_id") if name in fill}
            raise ValueError(f"Fill direction unavailable: {fields}")
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

    lots, allocated, consumed = [], {}, {}
    for order_id, order in exit_orders.items():
        if "allocations" not in order:
            continue
        allocation = {}
        for item in order["allocations"]:
            key, quantity = item["fill_id"], D(item["quantity"])
            if key in allocation or not quantity.is_finite() or quantity <= 0:
                raise ValueError("Invalid saved exit allocation")
            allocation[key] = quantity
        if not allocation:
            raise ValueError("Empty saved exit allocation")
        allocated[order_id] = allocation
        consumed[order_id] = {}
    for _, key, order_id, sign, quantity in sorted(events):
        entry = entry_orders.get(order_id)
        exit_order = exit_orders.get(order_id)
        if entry and sign != (1 if entry["side"] == "YES" else -1):
            raise ValueError("Entry fill direction does not match its saved intent")
        if exit_order and sign != (-1 if exit_order["side"] == "YES" else 1):
            raise ValueError("Exit fill direction does not match its saved intent")
        targeted = exit_order and exit_order.get("paired")
        for lot in lots:
            if lot["sign"] == sign or lot["quantity"] == 0:
                continue
            available = lot["quantity"]
            if order_id in allocated:
                remaining = (allocated[order_id].get(lot["fill_id"], D(0))
                             - consumed[order_id].get(lot["fill_id"], D(0)))
                available = min(available, remaining)
            elif targeted and lot["target"] != D(exit_order["target"]):
                continue
            reduced = min(quantity, available)
            lot["quantity"] -= reduced
            quantity -= reduced
            if order_id in allocated:
                used = consumed[order_id]
                used[lot["fill_id"]] = used.get(lot["fill_id"], D(0)) + reduced
            if not quantity:
                break
        if quantity:
            if exit_order:
                raise ValueError("Exit fill exceeds its attributable inventory")
            target = D(entry["target"]) if entry else None
            lots.append(dict(fill_id=key, sign=sign, quantity=quantity, target=target,
                             entry=entry, fill=seen[key]))
    lots = [lot for lot in lots if lot["quantity"]]
    reconstructed = sum((lot["quantity"] * lot["sign"] for lot in lots), D(0))
    if reconstructed != D(held):
        raise InventorySyncError("Fills and position disagree; waiting for consistent exchange data")
    outside = [lot for lot in lots if lot["target"] is None]
    if outside and untracked is None:
        raise ValueError("Untracked inventory has no verified paired exit target")
    if untracked is not None:
        untracked.extend({"fill_id": lot["fill_id"], "order_id": lot["fill"].get("order_id"),
                          "side": "YES" if lot["sign"] > 0 else "NO",
                          "quantity": str(lot["quantity"])} for lot in outside)
    return [lot for lot in lots if lot["target"] is not None]


def paired_inventory(fills, entry_orders, exit_orders, held, ticker, untracked=None):
    """Original fixed-pair accounting, also used to verify legacy receipts."""
    lots = _remaining_lots(fills, entry_orders, exit_orders, held, ticker, untracked)
    buckets = {}
    for lot in lots:
        buckets[lot["target"]] = buckets.get(lot["target"], D(0)) + lot["quantity"] * lot["sign"]
    return dict(sorted(buckets.items()))


def _outcome_cost(fill, sign):
    """Read trade prices, excluding fees, from the exchange fill payload."""
    prices = {}
    modern = any(fill.get(side + "_price_dollars") is not None for side in ("yes", "no"))
    for side in ("yes", "no"):
        if fill.get(side + "_price_dollars") is not None:
            price = D(str(fill[side + "_price_dollars"]))
        elif not modern and fill.get(side + "_price") is not None:
            price = D(str(fill[side + "_price"])) / 100
        else:
            continue
        if not price.is_finite() or not 0 < price < 1:
            raise ValueError("Invalid entry fill price")
        prices[side] = price
    if not prices:
        raise ValueError("Entry fill price unavailable; cannot calculate take profit")
    if len(prices) == 2 and prices["yes"] + prices["no"] != 1:
        raise ValueError("Conflicting YES/NO entry fill prices")
    side, other = ("yes", "no") if sign > 0 else ("no", "yes")
    return prices[side] if side in prices else 1 - prices[other]


def fill_cost_inventory(fills, entry_orders, exit_orders, held, ticker, untracked=None):
    """Return quantities and durable exit plans from remaining actual fill costs.

    Share a weighted average only among lots with the same profit increment.
    Sales consume the allocated lots FIFO, and sold cost is excluded on the next
    pass. Whole-cent ceilings preserve the gross increment without requiring a
    quote/market lookup; whole cents are valid on Kalshi's current price grids.
    """
    lots = _remaining_lots(fills, entry_orders, exit_orders, held, ticker, untracked)
    groups, buckets, plans = {}, {}, {}
    for lot in lots:
        if lot["target"] == 1:
            buckets[D(1)] = buckets.get(D(1), D(0)) + lot["sign"] * lot["quantity"]
            continue
        price = D(lot["entry"]["price"])
        margin = lot["target"] - price
        if not price.is_finite() or not 0 < price < lot["target"] < 1:
            raise ValueError("Invalid saved entry profit increment")
        cost = _outcome_cost(lot["fill"], lot["sign"])
        if cost > price:
            raise ValueError("Entry fill price exceeds its saved limit")
        lot["cost"] = cost
        groups.setdefault((lot["sign"], margin), []).append(lot)
    for (sign, margin), members in sorted(groups.items()):
        quantity = sum((lot["quantity"] for lot in members), D(0))
        cost = sum((lot["quantity"] * lot["cost"] for lot in members), D(0)) / quantity
        target = (cost + margin).quantize(D("0.01"), rounding=ROUND_CEILING)
        if not 0 < target < 1:
            raise ValueError("Calculated take-profit target is outside tradable prices")
        buckets[target] = buckets.get(target, D(0)) + sign * quantity
        plan = plans.setdefault(target, {"allocations": [], "cost_groups": []})
        plan["allocations"].extend({"fill_id": lot["fill_id"], "quantity": str(lot["quantity"])}
                                   for lot in members)
        plan["cost_groups"].append({"average_fill_cost": str(cost), "quantity": str(quantity),
                                    "profit_increment": str(margin)})
    return dict(sorted(buckets.items())), plans


def order_profit_inventory(fills, entry_orders, exit_orders, held, ticker, profit):
    """Price each buy order's remaining inventory for a dollar gross profit.

    Targets use verified entry fills, never the limit price. An unattainable
    target keeps the original paired exit so the position is still managed.
    """
    profit = D(profit)
    if not profit.is_finite() or profit <= 0:
        raise ValueError("Per-order profit must be positive")
    lots = _remaining_lots(fills, entry_orders, exit_orders, held, ticker, [])
    groups, buckets, plans = {}, {}, {}
    for lot in lots:
        if lot["target"] == 1:
            buckets[D(1)] = buckets.get(D(1), D(0)) + lot["sign"] * lot["quantity"]
        else:
            groups.setdefault((lot["sign"], lot["fill"]["order_id"]), []).append(lot)
    for (sign, order_id), members in sorted(groups.items()):
        quantity = sum((lot["quantity"] for lot in members), D(0))
        cost = sum((lot["quantity"] * _outcome_cost(lot["fill"], sign)
                    for lot in members), D(0)) / quantity
        target = (cost + profit / quantity).quantize(D("0.01"), rounding=ROUND_CEILING)
        if target > D("0.99"):
            # This buy cannot earn the requested amount before settlement.
            # Preserve its saved scalp exit rather than leave it unmanaged.
            target = max(lot["target"] for lot in members)
        buckets[target] = buckets.get(target, D(0)) + sign * quantity
        plan = plans.setdefault(target, {"allocations": [], "cost_groups": []})
        plan["allocations"].extend({"fill_id": lot["fill_id"], "quantity": str(lot["quantity"])}
                                   for lot in members)
        plan["cost_groups"].append({"order_id": order_id, "average_fill_cost": str(cost),
                                    "quantity": str(quantity), "gross_profit_goal": str(profit)})
    return dict(sorted(buckets.items())), plans


def order_percentage_inventory(fills, entry_orders, exit_orders, held, ticker, untracked, percentage):
    """Target a percentage return over fill cost with conservative fee room.

    The existing 3c per-contract fee reserve is applied to each leg. It is a
    cushion rather than a live fee quote. Settlement inventory stays held.
    """
    percentage = D(percentage)
    if not percentage.is_finite() or percentage <= 0:
        raise ValueError("Per-order percentage must be positive")
    lots = _remaining_lots(fills, entry_orders, exit_orders, held, ticker, untracked)
    groups, buckets, plans = {}, {}, {}
    for lot in lots:
        if lot["target"] == 1:
            buckets[D(1)] = buckets.get(D(1), D(0)) + lot["sign"] * lot["quantity"]
        else:
            groups.setdefault((lot["sign"], lot["fill"]["order_id"]), []).append(lot)
    for (sign, order_id), members in sorted(groups.items()):
        quantity = sum((lot["quantity"] for lot in members), D(0))
        cost = sum((lot["quantity"] * _outcome_cost(lot["fill"], sign)
                    for lot in members), D(0)) / quantity
        target = ((cost + FEE_RESERVE) * (D(1) + percentage) + FEE_RESERVE).quantize(
            D("0.01"), rounding=ROUND_CEILING)
        if target > D("0.99"):
            target = max(lot["target"] for lot in members)
        buckets[target] = buckets.get(target, D(0)) + sign * quantity
        plan = plans.setdefault(target, {"allocations": [], "cost_groups": []})
        plan["allocations"].extend({"fill_id": lot["fill_id"], "quantity": str(lot["quantity"])}
                                   for lot in members)
        plan["cost_groups"].append({"order_id": order_id, "average_fill_cost": str(cost),
                                    "quantity": str(quantity), "target_return": str(percentage),
                                    "fee_reserve_per_leg": str(FEE_RESERVE)})
    return dict(sorted(buckets.items())), plans
