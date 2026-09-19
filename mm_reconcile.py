"""Validate exchange fills before incorporating external position reductions."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D


def normalized_fill(fill, ticker):
    if (fill.get('ticker') or fill.get('market_ticker')) != ticker:
        raise ValueError('Fill ticker mismatch')
    side = fill.get('book_side')
    outcome = fill.get('outcome_side')
    if side not in ('bid', 'ask') or outcome not in ('yes', 'no'):
        raise ValueError('Canonical fill direction unavailable')
    if (side == 'bid') != (outcome == 'yes'):
        raise ValueError('Inconsistent canonical fill direction')
    quantity, price, fee = (D(fill[k]) for k in ('count_fp', 'yes_price_dollars', 'fee_cost'))
    if not all(x.is_finite() for x in (quantity, price, fee)) or not (quantity > 0 and 0 <= price <= 1 and fee >= 0):
        raise ValueError('Invalid fill amount')
    stamp = datetime.fromisoformat(fill['created_time'].replace('Z', '+00:00'))
    if stamp.tzinfo is None or not fill.get('fill_id') or not fill.get('order_id'):
        raise ValueError('Incomplete fill identity or time')
    return dict(fill_id=fill['fill_id'], order_id=fill['order_id'], side=side,
                quantity=str(quantity), price=str(price), fees=str(fee),
                created_time=stamp.isoformat())


def external_reductions(record, fills, ticker, actual, finalized=False):
    """Return a complete deduplicated external ledger, or refuse ambiguous history.

    Own fills must match saved exchange order totals. External fills must reduce
    MM inventory at the instant they occur. Never adopt unrelated new exposure.
    """
    own = {o['order_id']: o for o in record['orders'] if o.get('order_id')}
    unique, counts = {}, defaultdict(lambda: D(0))
    for raw in fills:
        f = normalized_fill(raw, ticker)
        prior = unique.get(f['fill_id'])
        if prior is not None and prior != f:
            raise ValueError('Conflicting duplicate fill')
        unique[f['fill_id']] = f
    own_fills = [f for f in unique.values() if f['order_id'] in own]
    for f in own_fills:
        if f['side'] != own[f['order_id']]['side']:
            raise ValueError('Fill direction differs from order')
        counts[f['order_id']] += D(f['quantity'])
    if any(counts[oid] != D(o.get('filled', '0')) for oid, o in own.items()):
        raise ValueError('Fill history incomplete relative to order totals')
    if not own_fills:
        if actual != 0:
            raise ValueError('Unowned position')
        return []
    # Canonical timestamps retain timezone offsets; compare instants, not strings.
    timestamp = lambda f: datetime.fromisoformat(f['created_time'])
    first = min(map(timestamp, own_fills))
    relevant = sorted((f for f in unique.values() if timestamp(f) >= first), key=lambda f: (timestamp(f), f['fill_id']))
    held, external = D(0), []
    for f in relevant:
        signed = D(f['quantity']) * (1 if f['side'] == 'bid' else -1)
        if f['order_id'] not in own:
            if held == 0 or held * signed >= 0 or abs(signed) > abs(held):
                raise ValueError('External fill increases or reverses MM exposure')
            external.append(f)
        held += signed
    if not finalized and held != actual:
        raise ValueError('Fills do not explain current position')
    old_ids = {f['fill_id'] for f in record.get('external_fills', [])}
    if not old_ids.issubset({f['fill_id'] for f in external}):
        raise ValueError('Previously reconciled fills missing from history')
    return external
