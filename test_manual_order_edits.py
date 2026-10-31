"""Operator amendments must not be undone, duplicated, or mistaken for fills."""
import copy
from dataclasses import replace
from decimal import Decimal as D

import pytest

from kalshi import KalshiAPIError
from manual_exit_runner import ManualExitService, Settings
from manual_limit_exits import ManualExitBlocked
from manual_order_edits import verified_manual_exit_snapshot, amendment_terms
from test_manual_limit_exits import T
from test_manual_stop_loss import setup
from test_order_price_recovery import reprice


def opened(tmp_path, sign=1):
    e, old, config, events = setup(tmp_path)
    config = replace(config, stop_loss_cents=D('20'), respect_manual_edits=True)
    s = ManualExitService(e, config, clock=e.clock,
                         emit=lambda ev, **kw: events.append((ev, kw)))
    e.buy(10, '.60', sign)
    e.bid = e.no_bid = D('.59')
    assert s.run_once(), events
    intent = next(iter(s.worker.intents(T).values()))
    return e, s, config, events, intent, intent['order_id']


def shrink(e, oid, remaining, keep_initial=False):
    row = e.remote[oid]
    row['remaining_count_fp'] = str(remaining)
    if not keep_initial:
        row['initial_count_fp'] = str(D(str(remaining)) + D(row['fill_count_fp']))


@pytest.mark.parametrize('sign', [1, -1])
@pytest.mark.parametrize('price', ['.61', '.70', '.6420'])
def test_manual_prices_survive_poll_and_restart(tmp_path, sign, price):
    e, s, config, events, intent, oid = opened(tmp_path, sign)
    reprice(e, oid, price)
    assert s.run_once(), events
    assert D(intent['target']) == D(price) and intent['manual_override']
    assert not e.cancelled and len(e.sent) == 1
    assert s.run_once() and not e.cancelled and len(e.sent) == 1
    restarted = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **k: None)
    assert restarted.run_once() and len(e.sent) == 1 and not e.cancelled
    assert any(ev == 'MANUAL_ORDER_EDIT_ADOPTED' for ev, _ in events)
    report = [r for ev,r in events if ev == 'MANUAL_PROTECTION_CHECK'][-1]
    assert D(report['tp_target']) == D(price)
    assert report['tp_manual_override'] and report['sl_trigger'] == '0.40'


@pytest.mark.parametrize('sign', [1, -1])
@pytest.mark.parametrize('keep_initial', [False, True])
def test_size_reduction_stays_reduced_and_stop_covers_remainder(tmp_path, sign, keep_initial):
    e, s, _, events, intent, oid = opened(tmp_path, sign)
    shrink(e, oid, 4, keep_initial)
    assert s.run_once(), events
    assert intent['quantity'] == '4' and intent['remaining'] == '4'
    assert len(e.sent) == 1 and not e.cancelled
    e.fill(oid, 4, '.65')
    assert s.run_once(), events
    assert e.held[T] == sign*6 and len(e.sent) == 1
    e.bid = e.no_bid = D('.40')
    assert s.run_once(), events
    assert e.held[T] == 0
    assert len(e.sent) == 2 and e.sent[-1]['count'] == '6'
    assert e.sent[-1]['reduce_only'] is True


@pytest.mark.parametrize('sign', [1, -1])
def test_partial_fill_then_price_count_and_client_id_amendment(tmp_path, sign):
    e, s, _, events, intent, oid = opened(tmp_path, sign)
    e.fill(oid, 3, '.65')
    assert s.run_once()
    reprice(e, oid, '.64')
    shrink(e, oid, 4)
    e.remote[oid]['client_order_id'] = 'app-modified-id'
    assert s.run_once(), events
    assert intent['quantity'] == '7' and intent['filled'] == '3'
    assert intent['remaining'] == '4' and intent['client_id'] == 'app-modified-id'
    assert not e.cancelled and len(e.sent) == 1
    e.bid = e.no_bid = D('.40')
    assert s.run_once(), events
    assert e.cancelled == [oid] and e.sent[-1]['count'] == '7'
    assert e.held[T] == 0


def test_can_increase_back_only_within_original_allocation(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    shrink(e, oid, 4)
    assert s.run_once(), events
    shrink(e, oid, 10)
    assert s.run_once(), events
    assert intent['quantity'] == '10' and len(e.sent) == 1 and not e.cancelled


def test_canceling_manual_edited_tp_does_not_repost(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    reprice(e, oid, '.70')
    assert s.run_once()
    e.request('DELETE', '/portfolio/events/orders/' + oid)
    assert s.run_once(), events
    assert len(e.sent) == 1 and e.cancelled == [oid]
    e.bid = D('.40')
    assert s.run_once(), events
    assert e.held[T] == 0 and len(e.sent) == 2


def test_new_buy_keeps_default_tp_after_an_existing_order_override(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    reprice(e, oid, '.70')
    assert s.run_once()
    e.buy(2, '.60'); e.clock_value += 2
    assert s.run_once(), events
    assert len(e.sent) == 2 and e.sent[-1]['price'] == '0.6500'
    assert e.remote[oid]['yes_price_dollars'] == '0.70'
    assert not e.cancelled


def test_unknown_exact_order_keeps_reservation_and_never_duplicates(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    reprice(e, oid, '.70')
    def missing(*a, **k): raise KalshiAPIError(404, 'not found')
    e.order = missing
    assert not s.run_once()
    assert not s.run_once()
    assert len(e.sent) == 1 and not e.cancelled
    assert intent['target'] == '0.65'


def test_delayed_amended_fill_does_not_cancel_or_repost(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    reprice(e, oid, '.64')
    fid = e.fill(oid, 3, '.64'); e.hidden_fills.add(fid)
    assert not s.run_once()
    assert not e.cancelled and len(e.sent) == 1
    e.hidden_fills.clear()
    assert s.run_once(), events
    assert intent['filled'] == '3' and intent['remaining'] == '7'
    assert not e.cancelled and len(e.sent) == 1


@pytest.mark.parametrize('sign', [1, -1])
def test_terminal_edited_order_verified_without_cancel(tmp_path, sign):
    e, s, _, events, intent, oid = opened(tmp_path, sign)
    reprice(e, oid, '.62'); e.fill(oid, 10, '.62')
    assert s.run_once(), events
    assert intent['status'] == 'executed' and e.held[T] == 0
    assert not e.cancelled and len(e.sent) == 1


def test_manual_edit_during_observe_refreshes_instead_of_canceling(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    observe = s.worker.observe
    done = []
    def edit_then_observe(*args):
        if not done:
            done.append(True); reprice(e, oid, '.63')
        return observe(*args)
    s.worker.observe = edit_then_observe
    assert s.run_once(), events
    assert intent['target'] == '0.63' and not e.cancelled and len(e.sent) == 1


def test_repeated_inflight_amendments_preserve_orders_and_block_new_writes(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    observe = s.worker.observe; prices = iter(['.63', '.62', '.61'])
    def edit_every_observe(*args):
        reprice(e, oid, next(prices))
        return observe(*args)
    s.worker.observe = edit_every_observe
    assert not s.run_once()
    assert not e.cancelled and len(e.sent) == 1
    assert any(ev == 'MANUAL_EDIT_RECHECK_PENDING' for ev,_ in events)


@pytest.mark.parametrize('field,value', [
    ('ticker','OTHER'), ('order_id','another-order'), ('subaccount_number',1),
    ('type','market'), ('book_side','bid'), ('initial_count_fp','11'),
    ('remaining_count_fp','11'), ('fill_count_fp','11'), ('client_order_id',''),
    ('yes_price_dollars','NaN'), ('yes_price_dollars','0'), ('yes_price_dollars','1'),
    ('yes_price_dollars','.64001'), ('no_price_dollars','.99'),
])
def test_invalid_identity_or_overallocation_never_adopted(tmp_path, field, value):
    e, s, _, events, intent, oid = opened(tmp_path)
    row = copy.deepcopy(e.remote[oid])
    row.update(yes_price_dollars='.64', no_price_dollars='.36'); row[field] = value
    with pytest.raises(ManualExitBlocked):
        amendment_terms(T, intent, row)
    assert 'manual_override' not in intent and not e.cancelled and len(e.sent) == 1


def test_lost_ack_before_edit_still_anchors_to_saved_exchange_id(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    intent['status'] = 'pending_ack'
    reprice(e, oid, '.64')
    assert s.run_once(), events
    assert intent['target'] == '0.64' and len(e.sent) == 1


def test_edit_does_not_clear_latched_stop_or_cancel_confirmation(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    e.bid = D('.40'); e.delay_cancel = True
    assert not s.run_once()
    assert intent['cancel_requested'] == 'stop_loss'
    reprice(e, oid, '.70')
    assert not s.run_once() and len(e.sent) == 1
    assert intent['cancel_requested'] == 'stop_loss'
    e.delay_cancel = False; e.clock_value += 6
    for _ in range(3): s.run_once()
    assert e.held[T] == 0 and e.sent[-1]['reduce_only']


def test_enabled_manual_edit_policy_cannot_be_silently_reverted(tmp_path):
    e, s, config, events, intent, oid = opened(tmp_path)
    with pytest.raises(ManualExitBlocked, match='silently disabled'):
        ManualExitService(e, replace(config, respect_manual_edits=False), clock=e.clock)


def test_settings_are_explicit_and_preserve_twenty_cent_stop():
    assert Settings.read({}).respect_manual_edits is False
    settings = Settings.read({'MANUAL_EXIT_RESPECT_MANUAL_EDITS':'true',
                              'MANUAL_STOP_LOSS_CENTS':'20'})
    assert settings.respect_manual_edits and settings.stop_loss_cents == D('20')
    assert settings.increment == D('.05')
