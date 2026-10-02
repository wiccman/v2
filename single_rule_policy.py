"""The only active strategy; dollar profit is gross, before exchange fees."""
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR

MIN_DISTANCE = D('50')
ENTRY_PRICE = D('0.96')
WINDOW_SECONDS = 120
MAX_CONTRACTS = D('11')
PROFIT_DOLLARS = D('0.40')


def entry_side(spot, strike, seconds_remaining, yes_ask, no_ask):
    """Return the qualifying outcome, never a pre-window or opposite buy."""
    spot, strike = D(str(spot)), D(str(strike))
    remaining = D(str(seconds_remaining))
    if not all(x.is_finite() for x in (spot, strike, remaining)) or min(spot, strike) <= 0:
        raise ValueError('Invalid reference price, strike, or clock')
    if not 0 < remaining <= WINDOW_SECONDS:
        return None
    delta = spot - strike
    side = 'YES' if delta >= MIN_DISTANCE else 'NO' if delta <= -MIN_DISTANCE else None
    if side is None:
        return None
    ask = D(str(yes_ask if side == 'YES' else no_ask))
    return side if ask.is_finite() and ask == ENTRY_PRICE else None


def grid_exit_price(required, side, ranges):
    """Smallest valid outcome sell price meeting required gross proceeds.

    Kalshi orders are priced on the YES grid. A NO sale is a YES bid, so
    round that bid down, rather than assuming the grid is symmetric.
    Read live market.price_ranges; never infer tick size from its label.
    """
    required = D(str(required))
    if side not in ('YES', 'NO') or not required.is_finite():
        raise ValueError('Invalid exit side or target')
    if not isinstance(ranges, list) or not ranges:
        raise ValueError('Market price_ranges unavailable')
    choices = []
    for band in ranges:
        start, end, step = (D(str(band[k])) for k in ('start', 'end', 'step'))
        if (not all(x.is_finite() for x in (start, end, step))
                or not 0 <= start < end <= 1 or step < D('0.0001')
                or any(x != x.quantize(D('0.0001')) for x in (start, end, step))):
            raise ValueError('Invalid market price grid')
        if side == 'YES':
            wanted = max(required, start, D('0.0001'))
            n = ((wanted - start) / step).to_integral_value(rounding=ROUND_CEILING)
        else:
            wanted = min(D(1) - required, end, D('0.9999'))
            n = ((wanted - start) / step).to_integral_value(rounding=ROUND_FLOOR)
        wire = start + n * step
        if not start <= wire <= end or not 0 < wire < 1:
            continue
        outcome = wire if side == 'YES' else D(1) - wire
        if outcome >= required:
            choices.append(outcome)
    return min(choices) if choices else None


def _outcome_price(fill, side):
    prices = {}
    for outcome in ('yes', 'no'):
        key = outcome + '_price_dollars'
        if fill.get(key) is not None:
            prices[outcome] = D(str(fill[key]))
    if not prices:
        raise ValueError('Verified fill dollar price unavailable')
    if any(not p.is_finite() or not 0 <= p <= 1 for p in prices.values()):
        raise ValueError('Invalid fill price')
    if len(prices) == 2 and prices['yes'] + prices['no'] != 1:
        raise ValueError('Conflicting outcome prices')
    selected = side.lower()
    return prices[selected] if selected in prices else D(1) - next(iter(prices.values()))


def profit_target(fills, entry_sides, exit_sides, remaining, side, ranges):
    """Target $0.40 across the market's single bot trade, including partial exits.

    Use only attributed bot entry costs and confirmed bot sale proceeds.
    Partial sales do not restart the profit goal. Untracked/manual proceeds
    are not counted, and manual holdings are excluded by the exit monitor.
    """
    remaining = D(str(remaining))
    if not remaining.is_finite() or remaining <= 0:
        raise ValueError('Invalid remaining bot inventory')
    if set(entry_sides) & set(exit_sides):
        raise ValueError('Entry and exit identities overlap')
    unique, cost, proceeds = {}, D(0), D(0)
    for fill in fills:
        key = fill.get('fill_id') or fill.get('trade_id')
        if not key:
            raise ValueError('Fill identity unavailable')
        if key in unique:
            if unique[key] != fill:
                raise ValueError('Conflicting duplicate fill')
            continue
        unique[key] = fill
        oid = fill.get('order_id')
        outcomes = entry_sides if oid in entry_sides else exit_sides
        if oid not in outcomes:
            continue
        quantity = D(str(fill.get('count_fp', fill.get('count', 'NaN'))))
        if not quantity.is_finite() or quantity <= 0 or outcomes[oid] not in ('YES', 'NO'):
            raise ValueError('Invalid attributed fill')
        amount = quantity * _outcome_price(fill, outcomes[oid])
        if oid in entry_sides:
            cost += amount
        else:
            proceeds += amount
    if cost <= 0:
        raise ValueError('No verified cost for bot inventory')
    required = (cost + PROFIT_DOLLARS - proceeds) / remaining
    return grid_exit_price(required, side, ranges), {
        'gross_profit_goal': str(PROFIT_DOLLARS),
        'verified_entry_cost': str(cost),
        'confirmed_sale_proceeds': str(proceeds),
        'remaining_bot_quantity': str(remaining),
        'required_outcome_price': str(required),
        'fees_included': False,
    }
