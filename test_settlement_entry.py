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
    record['signal']['prediction'] = side
    record['trade_side'] = side
    market = fake.market('TEST')
    market.update(yes_ask_dollars='0.97' if side == 'YES' else '0.04',
                  no_ask_dollars='0.97' if side == 'NO' else '0.04')
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    monkeypatch.setattr(fake, 'btc_reference_price',
                        lambda: D('100010') if side == 'YES' else D('99990'))
    return fake, record, state, clock, closed


@pytest.mark.parametrize('elapsed,expected', [(719,0),(720,1),(779,1),(780,1),(899,1),(900,0)])
@pytest.mark.parametrize('side', ['YES','NO'])
def test_boundary_side_and_six_contract_resting_limit(monkeypatch, elapsed, expected, side):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, side)
    record['signal'] = {'prediction': side}
    bot.settlement_entry(record, state, 'TEST', closed)
    assert len(fake.entries) == expected
    if expected:
        wire, quantity, price, kwargs = fake.entries[0]
        assert quantity == 6 and kwargs.get('ioc', False) is False
        assert (wire, price) == (('bid', D('.97')) if side == 'YES' else ('ask', D('.03')))
        intent = record['entry_intents'][-1]
        assert intent['hold_to_settlement'] and intent['exit_target'] == '1'
        assert D(intent['reserved_dollars']) == 6
        bot.settlement_entry(copy.deepcopy(record), state, 'TEST', closed)
        assert len(fake.entries) == 1


@pytest.mark.parametrize('ask,expected', [
    ('.96', 0), ('.9699', 0), ('.97', 1), ('.9701', 1),
    ('.975', 1), ('.98', 1), ('.99', 1), ('.999', 1), ('1', 0), ('1.001', 0),
])
@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_97_cent_limit_rests_when_selected_side_ask_is_at_least_97(monkeypatch, ask, expected, side):
    fake, record, state, clock, closed = setup(monkeypatch, side=side)
    fake.market('TEST')[side.lower() + '_ask_dollars'] = ask
    bot.settlement_entry(record, state, 'TEST', closed)
    assert len(fake.entries) == expected
    if expected:
        wire, quantity, limit, kwargs = fake.entries[0]
        assert quantity == 6 and limit == (D('.97') if side == 'YES' else D('.03'))
        assert kwargs.get('ioc', False) is False
        assert record['entry_intents'][0]['price'] == '0.97'


def test_final_window_reports_actual_quotes_when_no_97_cent_side(monkeypatch):
    import json
    fake, record, state, clock, closed = setup(monkeypatch)
    fake.market('TEST')['yes_ask_dollars'] = '0.96'
    events = []
    monkeypatch.setattr(bot, 'write_log', lambda event, *a, **k: events.append((event, k)))
    bot.settlement_entry(record, state, 'TEST', closed)
    checks = [json.loads(data['details']) for event, data in events if event == 'SETTLEMENT_97_CHECK']
    assert checks[-1]['reason'] == 'selected_side_not_at_or_above_97'
    assert checks[-1]['required_ask'] == checks[-1]['entry_limit'] == '0.97'
    assert checks[-1]['yes_ask'] == '0.96' and checks[-1]['no_ask'] == '0.04'
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


def test_reserve_six_dollars_and_preserve_existing_spend():
    record = {}
    while policy.reserve(record, 'YES', D('.39'), D('2'), D('25'), 480, 'regular'):
        pass
    earlier = sum(D(i['reserved_dollars']) for i in record['entry_intents'])
    assert 18 < earlier <= 19
    intent = policy.reserve(record, 'YES', D('.97'), D('10'), D('25'), 900, policy.SETTLEMENT_KIND)
    assert D(intent['quantity']) == 6
    assert sum(D(i['reserved_dollars']) for i in record['entry_intents']) <= 25
    assert policy.reserve(record, 'YES', D('.97'), D('10'), D('25'), 900, policy.SETTLEMENT_KIND) is None
    legacy = {'entry_intents':[{'reserved_dollars':'16'}]}
    assert policy.reserve(legacy, 'YES', D('.97'), D('10'), D('15'), 900, policy.SETTLEMENT_KIND) is None


def test_opposite_inventory_does_not_get_netted(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    fake.held = D('-5')
    bot.settlement_entry(record, state, 'TEST', closed)
    assert not fake.entries


def test_final_route_does_not_require_strike_ruler_signal(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    record['signal'] = None
    bot.cycle(state)
    assert len(fake.entries) == 1 and fake.entries[0][1] == 6


def test_final_entry_never_flips_the_market_side(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, side='NO')
    record['trade_side'] = 'YES'
    bot.settlement_entry(record, state, 'TEST', closed)
    assert not fake.entries


@pytest.mark.parametrize('hold_price', ['.97', '.98', '.99', '.999'])
@pytest.mark.parametrize('quantity', ['10','3.25'])
@pytest.mark.parametrize('sign', [1,-1])
def test_exit_worker_holds_settlement_lots_and_sells_only_scalps(tmp_path, quantity, sign, hold_price):
    e = PairExchange(bid='.99')
    e.buy('.39', '5', sign)
    oid = 'hold'
    e.intents.append(dict(order_id=oid, client_id=oid, side='YES' if sign == 1 else 'NO',
                          price=hold_price, exit_target='1', hold_to_settlement=True))
    e.held += sign * D(quantity)
    e.fill(oid, sign, D(quantity), hold_price)
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


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('ask', ['.97', '.9700'])
def test_exact_settlement_limit_and_fee_budget(monkeypatch, side, ask):
    fake, record, state, clock, closed = setup(monkeypatch, side=side)
    record['signal'] = {'prediction': side}
    record['trade_side'] = side
    fake.market('TEST')[side.lower() + '_ask_dollars'] = ask
    bot.settlement_entry(record, state, 'TEST', closed)
    assert len(fake.entries) == 1
    wire, qty, price, kwargs = fake.entries[0]
    assert qty == 6 and kwargs.get('ioc', False) is False
    assert price == (D('.97') if side == 'YES' else D('.03'))
    intent = record['entry_intents'][0]
    assert D(intent['price']) == D('.97') and intent['exit_target'] == '1'
    assert intent['hold_to_settlement'] and bot.tracked_entry_price_allowed(intent)
    assert D(intent['reserved_dollars']) <= 6
    bot.settlement_entry(copy.deepcopy(record), state, 'TEST', closed)
    assert len(fake.entries) == 1

@pytest.mark.parametrize('ask', ['.98','.999'])
def test_higher_price_does_not_override_same_side_lock(monkeypatch, ask):
    fake, record, state, clock, closed = setup(monkeypatch, side='NO')
    fake.market('TEST')['no_ask_dollars'] = ask
    record['trade_side'] = 'YES'
    bot.settlement_entry(record, state, 'TEST', closed)
    assert not fake.entries


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('next_ask,expected', [('.96', 0), ('.9701', 1), ('.98', 1), ('.99', 1)])
def test_quote_above_97_before_post_still_uses_resting_97_limit(monkeypatch, side, next_ask, expected):
    fake, record, state, clock, closed = setup(monkeypatch, side=side)
    market = dict(fake.market('TEST'))
    count = [0]
    def changing(ticker):
        count[0] += 1
        return {**market, side.lower() + '_ask_dollars': '.97' if count[0] == 1 else next_ask}
    monkeypatch.setattr(fake, 'market', changing)
    bot.settlement_entry(record, state, 'TEST', closed)
    assert len(fake.entries) == expected
    if expected:
        assert fake.entries[0][2] == (D('.97') if side == 'YES' else D('.03'))
        assert fake.entries[0][3].get('ioc', False) is False


@pytest.mark.parametrize('price', ['.9701', '.98', '.99'])
def test_direct_entry_and_reservation_cannot_bypass_97_limit(monkeypatch, price):
    fake, record, state, clock, closed = setup(monkeypatch)
    fake.market('TEST')['yes_ask_dollars'] = price
    assert bot.funded_entry(record, state, 'TEST', 'YES', D(price), closed,
                            policy.SETTLEMENT_KIND, submit_before=closed.timestamp()) == ({}, 0)
    assert policy.reserve(record, 'YES', D(price), D(6), D(15),
                          closed.timestamp(), policy.SETTLEMENT_KIND) is None
    assert not fake.entries and not record['entry_intents']


@pytest.mark.parametrize('price', ['.97', '.98', '.99', '.999'])
def test_existing_settlement_intents_stay_recognized(price):
    intent = dict(kind=policy.SETTLEMENT_KIND, price=price,
                  hold_to_settlement=True, exit_target='1')
    assert bot.tracked_entry_price_allowed(intent)
