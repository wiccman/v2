"""Real ownership monitor plus offline exchange; never sends live orders."""
import copy
import json
from decimal import Decimal as D

import pytest
import bot
from price_pairs import InventorySyncError
from take_profit import TakeProfitMonitor
from test_five_minute_exits import cycle_setup
from test_price_pairs import PairExchange
from test_settlement_switch import setup_switch


class CoexistExchange(PairExchange):
    def __init__(self, sign):
        super().__init__()
        self.sign = sign
        self.entries = []
        self.ask = D('.53')

    def market(self, ticker):
        side = 'yes' if self.sign > 0 else 'no'
        other = 'no' if self.sign > 0 else 'yes'
        return {'ticker': ticker, 'floor_strike': '100000',
                side + '_ask_dollars': str(self.ask), other + '_ask_dollars': '.47',
                'yes_bid_dollars': str(self.bid), 'no_bid_dollars': str(self.bid)}

    def btc_reference_price(self):
        return D(100000) + 60 * self.sign

    def market_cash(self, ticker):
        return {'exchange_index': 2, 'cash_dollars': '100'}

    def place_entry(self, ticker, side, quantity, price, expiration_time, **kwargs):
        oid = 'new-' + str(len(self.entries))
        self.entries.append((side, quantity, price))
        sign = 1 if side == 'YES' else -1
        self.held += quantity * sign
        self.fill(oid, sign, quantity, str(price if sign > 0 else 1-price))
        receipt = dict(order_id=oid, client_order_id=kwargs['client_order_id'],
                       fill_count=str(quantity), remaining_count='0', status='executed',
                       maker_fees_dollars='0', taker_fees_dollars='0')
        self.remote[oid] = receipt
        return receipt


def setup(tmp_path, monkeypatch, sign=1):
    _, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    record.update(historical_strike_orders=[], close_timestamp=closed.timestamp())
    state['markets'] = {'T': record}
    e = CoexistExchange(sign)
    monkeypatch.setattr(bot, 'client', e)
    monkeypatch.setattr(bot, 'DIRECTIONAL_ENTRY_POLICY', True)
    monkeypatch.setattr(bot, 'ENTRY_START_DELAY', 60)
    monkeypatch.setattr(bot, 'END', 720)
    m = TakeProfitMonitor(e, lambda: copy.deepcopy(state), tmp_path/'ownership.json',
                         pairs=bot.ALL_ENTRY_EXIT_PAIRS, clock=lambda: clock[0],
                         quote_gate=True, per_order_percentage=D('.05'), emit=lambda *a, **k: None)
    monkeypatch.setattr(bot, 'EXIT_MONITOR', m)
    return e, record, state, clock, closed, m


@pytest.mark.parametrize('sign', [1, -1])
def test_manual_first_allows_five_then_three_bot_contracts_and_sells_only_bot(tmp_path, monkeypatch, sign):
    e, record, state, clock, closed, m = setup(tmp_path, monkeypatch, sign)
    e.manual(sign, '159.57')
    m.run_once()
    side = 'YES' if sign > 0 else 'NO'
    assert m.bot_inventory('T', record, e.held) == 0
    assert bot.funded_entry(record, state, 'T', side, D('.53'), closed, 'regular')[1] == 5
    # A saved entry must invalidate the older zero-bot-inventory receipt.
    assert bot.funded_entry(record, state, 'T', side, D('.53'), closed, 'regular') == ({}, D(0))
    bot.reconcile_entries(state)
    m.run_once()
    assert m.bot_inventory('T', record, e.held) == 5 * sign
    clock[0] += 7
    assert bot.funded_entry(record, state, 'T', side, D('.53'), closed, 'regular')[1] == 3
    bot.reconcile_entries(state)
    m.run_once()
    assert bot.funded_entry(record, state, 'T', side, D('.53'), closed, 'regular') == ({}, D(0))
    assert abs(e.held) == D('167.57')
    assert sum(D(i['reserved_dollars']) for i in record['entry_intents']) <= 30
    e.bid = D('.70')
    m.run_once()
    m.run_once()
    assert abs(e.held) == D('159.57')
    assert sum(D(order['count']) for order in e.submissions) == 8
    assert m.bot_inventory('T', record, e.held) == 0


@pytest.mark.parametrize('sign', [1, -1])
def test_opposite_manual_position_is_preserved(tmp_path, monkeypatch, sign):
    e, record, state, clock, closed, m = setup(tmp_path, monkeypatch, sign)
    e.manual(-sign, '159.57')
    m.run_once()
    side = 'YES' if sign > 0 else 'NO'
    assert bot.funded_entry(record, state, 'T', side, D('.53'), closed, 'regular') == ({}, D(0))
    assert not e.entries and not e.submissions


@pytest.mark.parametrize('problem', ['stale', 'position', 'ledger', 'pending_exit', 'restart'])
def test_unverified_ownership_never_opens_new_exposure(tmp_path, monkeypatch, problem):
    e, record, state, clock, closed, m = setup(tmp_path, monkeypatch)
    e.manual(1, '10')
    m.run_once()
    if problem == 'stale':
        clock[0] += 11
    elif problem == 'position':
        e.manual(1, '1')
    elif problem == 'ledger':
        record['entry_intents'].append({'client_id': 'unresolved'})
    elif problem == 'pending_exit':
        m.state['markets']['T']['pending'] = {'client_id': 'exit'}
        m.save()
    elif problem == 'restart':
        m = TakeProfitMonitor(e, lambda: state, m.path, pairs=bot.ALL_ENTRY_EXIT_PAIRS,
                              clock=lambda: clock[0])
    with pytest.raises(InventorySyncError):
        m.bot_inventory('T', record, e.held)


def test_missing_confirmed_entry_fills_cannot_reset_bot_cap(tmp_path, monkeypatch):
    e, record, state, clock, closed, m = setup(tmp_path, monkeypatch)
    e.manual(1, '10')
    m.run_once()
    bot.funded_entry(record, state, 'T', 'YES', D('.53'), closed, 'regular')
    # Lagging position and fill reads agree with each other, but contradict the POST receipt.
    e.history.pop()
    e.held -= 5
    m.run_once()
    assert not m.healthy
    assert bot.funded_entry(record, state, 'T', 'YES', D('.53'), closed, 'regular') == ({}, D(0))
    assert len(e.entries) == 1


@pytest.mark.parametrize('sign', [1, -1])
def test_final_settlement_entry_excludes_same_side_manual_size(tmp_path, monkeypatch, sign):
    e, record, state, clock, closed, m = setup(tmp_path, monkeypatch, sign)
    e.manual(sign, '159.57')
    clock[0] = closed.timestamp() - 170
    e.ask = D('.96')
    record['signal'] = None
    m.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert len(e.entries) == 1 and e.entries[0][1] == 6
    assert abs(e.held) == D('165.57') and not e.submissions


@pytest.mark.parametrize('desired', ['YES', 'NO'])
def test_settlement_switch_never_liquidates_mixed_manual_and_bot_inventory(tmp_path, monkeypatch, desired):
    e, record, state, clock, closed, m, events = setup_switch(tmp_path, monkeypatch, desired)
    e.manual(-1 if desired == 'YES' else 1, '7')
    bot.settlement_entry(record, state, 'T', closed)
    m.run_once()
    assert not e.submissions and not e.buys and abs(e.held) == 9
    assert m.healthy and not m.settlement_ready('T', desired)


def test_manual_position_change_during_quote_blocks_submission(tmp_path, monkeypatch):
    e, record, state, clock, closed, m = setup(tmp_path, monkeypatch)
    e.manual(1, '10')
    m.run_once()
    market, calls = e.market, []
    def changing_quote(ticker):
        calls.append(ticker)
        if len(calls) == 2:
            e.manual(-1, '10')
        return market(ticker)
    monkeypatch.setattr(e, 'market', changing_quote)
    assert bot.funded_entry(record, state, 'T', 'YES', D('.53'), closed, 'regular') == ({}, D(0))
    assert not e.entries
