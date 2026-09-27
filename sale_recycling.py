"""Conservative, idempotent entry allowance recovered from confirmed bot exits."""
from decimal import Decimal as D

from entry_policy import FEE_RESERVE, SETTLEMENT_KIND
from price_pairs import _outcome_cost, InventorySyncError


def confirmed_credits(record, exit_orders, fills, ticker):
    """Credit no more than entry reservation or net sale proceeds per share.

    Only allocated, terminal exit orders recorded by the independent monitor
    qualify. Missing exchange fills or unresolved entry fees retain allowance.
    """
    unique = {}
    for fill in fills:
        if (fill.get("ticker") or fill.get("market_ticker")) != ticker:
            raise InventorySyncError("Mismatched fill ticker")
        if fill.get("subaccount_number", 0) != 0:
            raise InventorySyncError("Non-primary fill in sale credit history")
        key = fill.get("fill_id") or fill.get("trade_id")
        if not key or (key in unique and unique[key] != fill):
            raise InventorySyncError("Missing or conflicting fill identity")
        unique[key] = fill
    by_order = {}
    for key, fill in unique.items():
        by_order.setdefault(fill.get("order_id"), []).append((key, fill))
    entries = {item["order_id"]: item for item in record.get("entry_intents", [])
               if item.get("order_id")}
    credits, used_entry_fills = {}, {}
    for exit_id, order in exit_orders.items():
        if order.get("purpose") != "take_profit" or not order.get("allocations"):
            continue
        matched = by_order.get(exit_id, [])
        count = sum((D(str(fill.get("count_fp", fill.get("count", "NaN"))))
                     for _, fill in matched), D(0))
        if count != D(order["filled"]):
            raise InventorySyncError("Confirmed sale and fill history disagree")
        if count <= 0:
            continue
        sign = 1 if order["side"] == "YES" else -1
        if any(str(fill.get("book_side", "")).lower() != ("ask" if sign > 0 else "bid")
               for _, fill in matched):
            raise InventorySyncError("Sale fill direction disagrees with exit")
        sale_value = sum((D(str(fill.get("count_fp", fill.get("count", "NaN"))))
                          * _outcome_cost(fill, sign) for _, fill in matched), D(0))
        net_sale_per_share = max(D(0), sale_value / count - FEE_RESERVE)
        remaining, credit = count, D(0)
        for allocation in order["allocations"]:
            if remaining == 0:
                break
            key = allocation["fill_id"]
            entry_fill = unique.get(key)
            if entry_fill is None:
                raise InventorySyncError("Allocated entry fill not visible")
            entry = entries.get(entry_fill.get("order_id"))
            if entry is None or entry.get("kind") == SETTLEMENT_KIND:
                raise InventorySyncError("Sale allocation does not identify a scalp entry")
            if not entry.get("reservation_reconciled"):
                raise InventorySyncError("Entry cost or fees have not been reconciled")
            if entry["side"] != order["side"]:
                raise InventorySyncError("Sale allocation side disagrees with entry")
            if str(entry_fill.get("book_side", "")).lower() != ("bid" if sign > 0 else "ask"):
                raise InventorySyncError("Entry fill direction disagrees with sale allocation")
            allocation_count = D(allocation["quantity"])
            taken = min(remaining, allocation_count)
            basis = D(entry["reserved_dollars"]) / D(entry["confirmed_entry_filled_quantity"])
            credit += taken * min(basis, net_sale_per_share)
            used_entry_fills[key] = used_entry_fills.get(key, D(0)) + taken
            if used_entry_fills[key] > D(str(entry_fill.get("count_fp", entry_fill.get("count", "NaN")))):
                raise InventorySyncError("Sale exceeds verified entry fill")
            remaining -= taken
        if remaining:
            raise InventorySyncError("Sale has no complete verified entry allocation")
        credits[exit_id] = str(credit)
    return credits
