import copy
from decimal import Decimal as D
import pytest
import bot
import entry_policy as policy
from test_five_minute_exits import cycle_setup
from test_price_pairs import PairExchange
from take_profit import TakeProfitMonitor


def setup(monkeypatch, elapsed=780, side='YES'):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    fake.held = D('0')
    market = fake.market('TEST')
    market.update(yes_ask_dollars='0.97' if side == 'YES' else '0.04',
                  no_ask_dollars='0.97' if side == 'NO' else '0.04')
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    return fake, record, state, clock, closed


@pytest.mark.parametrize('elapsed,expected', [(779,0),(780,1),(899,1),(900,0)])
@pytest.mark.parametrize('side', ['YES','NO'])
def test_boundary_side_and_ten_contract_ioc(monkeypatch, elapsed, expected, side):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, side)
    record['signal'] = {'prediction': side}
    bot.settlement_entry(record, state, 'TEST', closed)
    assert len(fake.entries) == expected
    if expected:
        wire, quantity, price, kwargs = fake.entries[0]
        assert quantity == 10 and kwargs['ioc'] is True
        assert (wire, price) == (('bid', D('.97')) if side == 'YES' else ('ask', D('.03')))
        intent = record['entry_intents'][-1]
        assert intent['hold_to_settlement'] and intent['exit_target'] == '1'
        assert D(intent['reserved_dollars']) == 10
        bot.settlement_entry(copy.deepcopy(record), state, 'TEST', closed)
        assert len(fake.entries) == 1


@pytest.mark.parametrize('ask', ['.96','.98','.99'])
def test_requires_97_cent_quote(monkeypatch, ask):
    fake, record, state, clock, closed = setup(monkeypatch)
    fake.market('TEST')['yes_ask_dollars'] = ask
    bot.settlement_entry(record, state, 'TEST', closed)
    assert not fake.entries


def test_final_window_reports_actual_quotes_when_no_97_cent_side(monkeypatch):
    import json
    fake, record, state, clock, closed = setup(monkeypatch)
    fake.market('TEST')['yes_ask_dollars'] = '0.98'
    events = []
    monkeypatch.setattr(bot, 'write_log', lambda event, *a, **k: events.append((event, k)))
    bot.settlement_entry(record, state, 'TEST', closed)
    checks = [json.loads(data['details']) for event, data in events if event == 'SETTLEMENT_97_CHECK']
    assert checks[-1]['reason'] == 'waiting_for_exact_97_ask'
    assert checks[-1]['yes_ask'] == '0.98' and checks[-1]['no_ask'] == '0.04'
    assert checks[-1]['seconds_remaining'] == 120
    assert not fake.entries


def test_final_window_reports_funding_wait_without_claiming_an_entry(monkeypatch):
    import json
    fake, record, state, clock, closed = setup(monkeypatch)
    monkeypatch.setattr(fake, 'market_cash', lambda ticker: {'exchange_index': 2, 'cash_dollars': '1.9818'})
    events = []
    monkeypatch.setattr(bot, 'write_log', lambda event, *a, **k: events.append((event, k)))
    bot.settlement_entry(record, state, 'TEST', closed)
    names = [event for event, _ in events]
    assert 'ENTRY_WAIT_MARKET_CASH' in names and 'SETTLEMENT_97_ENTRY' not in names
    checks = [json.loads(data['details']) for event, data in events if event == 'SETTLEMENT_97_CHECK']
    assert checks[-1]['reason'] == 'not_submitted_or_unacknowledged'
    assert not fake.entries and not record['entry_intents']


def test_final_window_requires_order_ack_before_logging_entry(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    monkeypatch.setattr(fake, 'place_entry', lambda *a, **k: {})
    events = []
    monkeypatch.setattr(bot, 'write_log', lambda event, *a, **k: events.append(event))
    bot.settlement_entry(record, state, 'TEST', closed)
    assert 'SETTLEMENT_97_ENTRY' not in events
    assert len(record['entry_intents']) == 1  # Keep the reservation until resolved.


def test_late_quote_cannot_submit_after_close(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 899)
    market = fake.market('TEST')
    def slow(ticker):
        clock[0] = closed.timestamp()
        return market
    monkeypatch.setattr(fake, 'market', slow)
    bot.settlement_entry(record, state, 'TEST', closed)
    assert not fake.entries


def test_ambiguous_ack_never_rebuys(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    def timeout(*a, **k):
        raise TimeoutError('ack lost')
    monkeypatch.setattr(fake, 'place_entry', timeout)
    with pytest.raises(TimeoutError):
        bot.settlement_entry(record, state, 'TEST', closed)
    bot.settlement_entry(copy.deepcopy(record), state, 'TEST', closed)
    assert len(record['entry_intents']) == 1


def test_reserve_ten_dollars_and_preserve_existing_spend():
    record = {}
    while policy.reserve(record, 'YES', D('.39'), D('2'), D('25'), 480, 'regular'):
        pass
    earlier = sum(D(i['reserved_dollars']) for i in record['entry_intents'])
    assert earlier <= 15
    intent = policy.reserve(record, 'YES', D('.97'), D('10'), D('25'), 900, policy.SETTLEMENT_KIND)
    assert D(intent['quantity']) == 10
    assert sum(D(i['reserved_dollars']) for i in record['entry_intents']) <= 25
    assert policy.reserve(record, 'YES', D('.97'), D('10'), D('25'), 900, policy.SETTLEMENT_KIND) is None
    legacy = {'entry_intents':[{'reserved_dollars':'16'}]}
    assert policy.reserve(legacy, 'YES', D('.97'), D('10'), D('25'), 900, policy.SETTLEMENT_KIND) is None


def test_opposite_inventory_does_not_get_netted(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    fake.held = D('-5')
    bot.settlement_entry(record, state, 'TEST', closed)
    assert not fake.entries


def test_final_route_does_not_require_strike_ruler_signal(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    record['signal'] = None
    bot.cycle(state)
    assert len(fake.entries) == 1 and fake.entries[0][1] == 10


def test_final_entry_never_flips_the_market_side(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, side='NO')
    record['trade_side'] = 'YES'
    bot.settlement_entry(record, state, 'TEST', closed)
    assert not fake.entries


@pytest.mark.parametrize('quantity', ['10','3.25'])
@pytest.mark.parametrize('sign', [1,-1])
def test_exit_worker_holds_settlement_lots_and_sells_only_scalps(tmp_path, quantity, sign):
    e = PairExchange(bid='.99')
    e.buy('.39', '5', sign)
    oid = 'hold'
    e.intents.append(dict(order_id=oid, client_id=oid, side='YES' if sign == 1 else 'NO',
                          price='.97', exit_target='1', hold_to_settlement=True))
    e.held += sign * D(quantity)
    e.fill(oid, sign, D(quantity), '.97')
    state = {'markets':{'T':{'close_timestamp':1900,'entry_intents':e.intents}}}
    m = TakeProfitMonitor(e, lambda: state, tmp_path/'hold.json',
        pairs={D('.39'):D('.46'),D('.97'):D('1')}, clock=lambda:1000, emit=lambda *a,**k:None)
    for _ in range(3):
        m.run_once()
    assert m.healthy
    assert e.held == sign * D(quantity)
    assert len(e.submissions) == 1 and e.submissions[0]['count'] == '5'
    restarted = TakeProfitMonitor(e, lambda: state, tmp_path/'hold.json',
        pairs={D('.39'):D('.46'),D('.97'):D('1')}, clock=lambda:1001, emit=lambda *a,**k:None)
    restarted.run_once()
    assert restarted.healthy and len(e.submissions) == 1
