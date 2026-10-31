"""Offline buy-stop prototype. No network, credentials, or live order transport.

Prices use Decimal dollars. The quote's bid/ask are for the selected outcome,
so a NO trigger follows the NO price. Caller supplies a fresh ordered stream.
This prototype is not connected to the existing bot or its exit journals.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal as D
import json


def money(value):
    value = D(str(value))
    if not value.is_finite() or not D('0') < value < D('1'):
        raise ValueError('Price must be finite and strictly between $0 and $1')
    return value


@dataclass(frozen=True)
class Settings:
    ticker: str = 'SIMULATED-15M'
    side: str = 'YES'
    trigger: D = D('0.60')
    cap: D = D('0.62')
    quantity: int = 10
    expires_ms: int = 900_000
    max_age_ms: int = 500
    max_spread: D = D('0.04')

    def __post_init__(self):
        if not self.ticker or self.side not in ('YES', 'NO'):
            raise ValueError('Explicit ticker and YES/NO required')
        for field in ('trigger', 'cap', 'max_spread'):
            object.__setattr__(self, field, money(getattr(self, field)))
        if self.cap < self.trigger:
            raise ValueError('Cap cannot be below trigger')
        if type(self.quantity) is not int or self.quantity < 1:
            raise ValueError('Positive whole-contract quantity required')
        if self.expires_ms <= 0 or self.max_age_ms <= 0:
            raise ValueError('Positive expiry and freshness threshold required')


@dataclass(frozen=True)
class Quote:
    ticker: str
    side: str
    bid: D
    ask: D
    ts_ms: int
    sequence: int
    snapshot: bool = False
    market_open: bool = True


@dataclass(frozen=True)
class Intent:
    ticker: str
    side: str
    quantity: int
    cap: D
    expires_ms: int
    trigger_ms: int


class BuyStop:
    """One attempt per armed instance; ambiguous/no/partial fills never retry.

    WAITING -> ARMED after a valid quote below trigger -> ATTEMPTED on crossing.
    Crossing beyond cap consumes the trigger as SKIPPED. Disconnect resets the
    baseline. A post-reconnect snapshot above trigger cannot manufacture a cross.
    A production adapter MUST persist state before sending any real request.
    """
    def __init__(self, settings):
        self.settings = settings
        self.state = 'WAITING'
        self.last_sequence = -1
        self.reason = 'Awaiting a fresh quote below the trigger'

    def disconnect(self):
        if self.state in ('WAITING', 'ARMED'):
            self.state = 'WAITING'
            self.last_sequence = -1
            self.reason = 'Disconnected; require a fresh baseline'

    def observe(self, quote, now_ms):
        s = self.settings
        if self.state not in ('WAITING', 'ARMED'):
            return None
        if now_ms >= s.expires_ms:
            self.state, self.reason = 'EXPIRED', 'Market window ended'
            return None
        if quote.ticker != s.ticker or quote.side != s.side:
            return None
        if quote.sequence <= self.last_sequence:
            return None
        self.last_sequence = quote.sequence
        if not quote.market_open:
            self.state, self.reason = 'EXPIRED', 'Market is closed'
            return None
        try:
            bid, ask = money(quote.bid), money(quote.ask)
        except (ValueError, ArithmeticError):
            self.state, self.reason = 'WAITING', 'Invalid price; require a new baseline'
            return None
        if (not 0 <= now_ms - quote.ts_ms <= s.max_age_ms or bid > ask
                or ask - bid > s.max_spread):
            self.state, self.reason = 'WAITING', 'Stale/invalid/wide quote; require a new baseline'
            return None
        if quote.snapshot:
            self.state = 'WAITING'
        if ask < s.trigger:
            self.state, self.reason = 'ARMED', 'Fresh quote observed below trigger'
            return None
        if self.state != 'ARMED':
            self.reason = 'Already above trigger; waiting for a new cross from below'
            return None
        if ask > s.cap:
            self.state, self.reason = 'SKIPPED', 'Price crossed beyond entry cap'
            return None
        self.state, self.reason = 'ATTEMPTED', 'One capped IOC intent created'
        return Intent(s.ticker, s.side, s.quantity, s.cap, s.expires_ms, now_ms)

    def checkpoint(self):
        return json.dumps({'settings': asdict(self.settings), 'state': self.state,
                           'last_sequence': self.last_sequence, 'reason': self.reason}, default=str)

    @classmethod
    def restore(cls, checkpoint):
        data = json.loads(checkpoint)
        obj = cls(Settings(**data['settings']))
        if data['state'] not in ('WAITING', 'ARMED', 'ATTEMPTED', 'SKIPPED', 'EXPIRED'):
            raise ValueError('Invalid saved state')
        obj.state, obj.reason = data['state'], data['reason']
        # A new stream needs a fresh baseline, even if previously ARMED.
        if obj.state in ('WAITING', 'ARMED'):
            obj.disconnect()
        return obj


def simulate_ioc(intent, offers, execution_ms):
    """Match synthetic offers at execution time, not the trigger-time quote.

    offers is [(selected-outcome ask price, integer available contracts), ...].
    IOC can partially fill; no remainder is left resting. Fees are excluded.
    """
    if execution_ms < intent.trigger_ms:
        raise ValueError('Execution cannot precede trigger')
    if execution_ms >= intent.expires_ms:
        return {'filled': 0, 'average': None, 'cost': '0', 'unfilled': intent.quantity,
                'resting': 0, 'status': 'expired_before_submission'}
    levels = []
    for price, count in offers:
        if type(count) is not int or count < 0:
            raise ValueError('Synthetic liquidity must be nonnegative whole contracts')
        levels.append((money(price), count))
    remaining, cost = intent.quantity, D('0')
    for price, available in sorted(levels):
        if price > intent.cap or remaining == 0:
            break
        count = min(remaining, available)
        remaining -= count
        cost += price * count
    filled = intent.quantity - remaining
    return {'filled': filled, 'average': str(cost / filled) if filled else None,
            'cost': str(cost), 'unfilled': remaining, 'resting': 0,
            'status': 'filled' if remaining == 0 else 'remainder_canceled'}


def quote(ask, sequence, *, now_ms=None, side='YES', ticker='SIMULATED-15M',
          bid=None, snapshot=False, market_open=True):
    ask = D(str(ask))
    return Quote(ticker, side, ask - D('.01') if bid is None else D(str(bid)), ask,
                 sequence * 100 if now_ms is None else now_ms, sequence, snapshot, market_open)


def run_example(name, trigger_ask, offers):
    engine = BuyStop(Settings())
    engine.observe(quote('.55', 1, snapshot=True), 100)
    intent = engine.observe(quote(trigger_ask, 2), 200)
    receipt = simulate_ioc(intent, offers, 210) if intent else None
    return {'scenario': name, 'state': engine.state, 'reason': engine.reason,
            'execution': receipt, 'data_kind': 'synthetic'}


def examples():
    return [
        run_example('55c -> 60c; 10 offers at 60c', '.60', [('.60', 10)]),
        run_example('55c -> 61c; 10 offers at 61c', '.61', [('.61', 10)]),
        run_example('55c -> 70c directly', '.70', [('.70', 10)]),
        run_example('Triggers at 60c; offers jump to 70c before execution', '.60', [('.70', 10)]),
        run_example('Partial: 3 at 60c + 2 at 62c; remainder at 65c', '.60', [('.60', 3), ('.62', 2), ('.65', 5)]),
        run_example('Triggers at 60c; no offers available', '.60', []),
    ]


if __name__ == '__main__':
    print(json.dumps({'mode': 'OFFLINE SIMULATION ONLY', 'results': examples()}, indent=2))
