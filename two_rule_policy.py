"""Two requested entry rules and gross-dollar exits. No exchange I/O.

Targets are before fees, not guaranteed profits. An unattainable target is
reported, never silently replaced by a 98c/99c exit or an entry-price filter.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping, Sequence

D = Decimal
ZERO, ONE = D(0), D(1)
FEE_RESERVE = D('0.03')  # Existing per-contract entry cushion, not a fee quote.
BUILD = 'two-rules-2026-10-02-r2'


def number(value: Any, name: str) -> Decimal:
    try:
        result = D(str(value))
    except Exception as exc:
        raise ValueError(f'Invalid {name}') from exc
    if not result.is_finite():
        raise ValueError(f'Non-finite {name}')
    return result


@dataclass(frozen=True)
class Rule:
    name: str
    distance: Decimal
    contracts: int
    profit: Decimal
    last_seconds: int | None = None
    exact_ask: Decimal | None = None


FINAL = Rule('final_2m_50', D('50'), 9, D('0.40'), 120, D('0.96'))
DIRECTIONAL = Rule('directional_100', D('100'), 9, D('1.00'))
RULES = (FINAL, DIRECTIONAL)
BY_NAME = {rule.name: rule for rule in RULES}


@dataclass(frozen=True)
class Signal:
    rule: str
    side: str
    price: Decimal
    contracts: int
    profit: Decimal


def direction(spot: Any, strike: Any, threshold: Decimal) -> str | None:
    spot, strike = number(spot, 'spot'), number(strike, 'strike')
    if min(spot, strike) <= 0:
        raise ValueError('Reference prices must be positive')
    gap = spot - strike
    return 'YES' if gap >= threshold else 'NO' if gap <= -threshold else None


def candidate(rule: Rule, market: Mapping[str, Any], spot: Any,
              seconds_remaining: Any) -> Signal | None:
    left = number(seconds_remaining, 'seconds_remaining')
    if not ZERO < left <= D('900'):
        return None
    if rule.last_seconds is not None and left > rule.last_seconds:
        return None
    side = direction(spot, market['floor_strike'], rule.distance)
    if side is None:
        return None
    ask = number(market[side.lower() + '_ask_dollars'], 'ask')
    if not ZERO < ask < ONE:
        return None
    if rule.exact_ask is not None and ask != rule.exact_ask:
        return None
    return Signal(rule.name, side, ask, rule.contracts, rule.profit)


def valid_grid(ranges: Any) -> list[tuple[Decimal, Decimal, Decimal]]:
    if not isinstance(ranges, list) or not ranges:
        raise ValueError('Market price_ranges unavailable; cannot infer a tick')
    bands = []
    for item in ranges:
        start, end, step = (number(item[k], 'price grid') for k in ('start', 'end', 'step'))
        if not ZERO <= start < end <= ONE or not D('.0001') <= step <= ONE:
            raise ValueError('Invalid price grid range')
        if any(x != x.quantize(D('.0001')) for x in (start, end, step)):
            raise ValueError('Unsupported price precision')
        bands.append((start, end, step))
    bands.sort()
    if any(bands[i][0] < bands[i - 1][1] for i in range(1, len(bands))):
        raise ValueError('Overlapping price grid ranges')
    return bands


def grid_exit(required: Any, side: str, ranges: Any) -> Decimal | None:
    """Round on the YES wire grid, including an asymmetric NO complement.

    Bands are half-open [start,end); 0 and 1 are never submitted as prices.
    Missing/inconsistent metadata fails closed, rather than guessing ticks.
    """
    required = number(required, 'exit price')
    if side not in ('YES', 'NO'):
        raise ValueError('Invalid outcome side')
    choices = []
    for start, end, step in valid_grid(ranges):
        n_max = ((end - start) / step).to_integral_value(rounding=ROUND_CEILING) - 1
        max_wire = start + n_max * step
        if side == 'YES':
            desired = max(required, start, D('.0001'))
            n = ((desired - start) / step).to_integral_value(rounding=ROUND_CEILING)
        else:
            desired = min(ONE - required, max_wire, D('.9999'))
            n = ((desired - start) / step).to_integral_value(rounding=ROUND_FLOOR)
        wire = start + n * step
        if not start <= wire < end or not ZERO < wire < ONE:
            continue
        outcome = wire if side == 'YES' else ONE - wire
        if outcome >= required:
            choices.append(outcome)
    return min(choices) if choices else None


def fill_price(fill: Mapping[str, Any], side: str) -> Decimal:
    prices = {key: number(fill[key + '_price_dollars'], 'fill price')
              for key in ('yes', 'no') if fill.get(key + '_price_dollars') is not None}
    if not prices or any(not ZERO <= p <= ONE for p in prices.values()):
        raise ValueError('Invalid/missing fill dollar price')
    if len(prices) == 2 and prices['yes'] + prices['no'] != ONE:
        raise ValueError('Conflicting fill prices')
    key = side.lower()
    return prices[key] if key in prices else ONE - next(iter(prices.values()))


def unique_fills(fills: Sequence[Mapping[str, Any]], ticker: str) -> list[Mapping[str, Any]]:
    found = {}
    for fill in fills:
        if (fill.get('ticker') or fill.get('market_ticker')) != ticker:
            raise ValueError('Fill ticker mismatch')
        if fill.get('subaccount_number', 0) != 0:
            raise ValueError('Non-primary fill')
        identity = fill.get('fill_id') or fill.get('trade_id')
        if not identity:
            raise ValueError('Fill identity missing')
        if identity in found and found[identity] != fill:
            raise ValueError('Conflicting duplicate fill')
        found[identity] = fill
    return list(found.values())


@dataclass(frozen=True)
class Receipt:
    entered: Decimal
    sold: Decimal
    cost: Decimal
    proceeds: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.entered - self.sold


def receipt(trade: Mapping[str, Any], fills: Sequence[Mapping[str, Any]]) -> Receipt:
    side = trade['side']
    if side not in ('YES', 'NO'):
        raise ValueError('Invalid saved side')
    orders = {o['order_id']: o for o in trade['orders'] if o.get('order_id')}
    if len(orders) != sum(bool(o.get('order_id')) for o in trade['orders']):
        raise ValueError('Duplicate saved order identity')
    entered = sold = cost = proceeds = ZERO
    for fill in fills:
        order = orders.get(fill.get('order_id'))
        if order is None:
            continue
        q = number(fill.get('count_fp', fill.get('count')), 'fill quantity')
        if q <= 0:
            raise ValueError('Nonpositive fill quantity')
        role = order['role']
        if role not in ('entry', 'exit'):
            raise ValueError('Unknown order role')
        expected = ('bid' if side == 'YES' else 'ask') if role == 'entry' else ('ask' if side == 'YES' else 'bid')
        if str(fill.get('book_side', '')).lower() != expected:
            raise ValueError('Fill direction contradicts saved order')
        price = fill_price(fill, side)
        limit = number(order['price'], 'saved limit')
        if role == 'entry':
            if price > limit:
                raise ValueError('Entry fill exceeds buy limit')
            entered += q
            cost += q * price
        else:
            if price < limit:
                raise ValueError('Exit fill below sell limit')
            sold += q
            proceeds += q * price
    if sold > entered:
        raise ValueError('Exit fills exceed tracked bot inventory')
    return Receipt(entered, sold, cost, proceeds)


def target(trade: Mapping[str, Any], observed: Receipt, ranges: Any) -> Decimal | None:
    if observed.remaining <= 0:
        return None
    goal = number(trade['profit'], 'saved profit goal')
    if goal <= 0:
        raise ValueError('Profit goal must be positive')
    return grid_exit((observed.cost + goal - observed.proceeds) / observed.remaining,
                     trade['side'], ranges)


def config() -> dict[str, Any]:
    return {'build': BUILD, 'profit_basis': 'gross_before_fees',
            'rules': [{'name': r.name, 'strike_distance_dollars': str(r.distance),
                       'contracts': r.contracts, 'gross_profit_dollars': str(r.profit),
                       'last_seconds': r.last_seconds,
                       'exact_ask': str(r.exact_ask) if r.exact_ask is not None else None}
                      for r in RULES],
            'fixed_98c_or_99c_exit': False}
