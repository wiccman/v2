"""Regression coverage for changed TP prices, terminal orders and cancel races."""
import copy
from decimal import Decimal as D

import pytest

from kalshi import KalshiAPIError
from manual_exit_runner import ManualExitService
from manual_limit_exits import ManualExitBlocked
from order_reconciliation import terminal_cancel_receipt, verified_exit_snapshot
from test_manual_limit_exits import T
from test_manual_stop_loss import setup


def opened(tmp_path, sign=1):
    e, service, config, events = setup(tmp_path)
    e.buy(10, '.42', sign)
    assert service.run_once()
    intent = next(iter(service.worker.intents(T).values()))
    return e, service, config, events, intent, intent['order_id']


def reprice(e, oid, outcome):
    row = e.remote[oid]
    wire = D(outcome) if row['side'] == 'yes' else 1-D(outcome)
    row.update(yes_price_dollars=str(wire), no_price_dollars=str(1-wire))


@pytest.mark.parametrize('sign', [1, -1])
def test_terminal_price_change_recovers_only_with_confirmed_fills(tmp_path, sign):
    e, s, config, events, intent, oid = opened(tmp_path, sign)
    reprice(e, oid, '.43')
    e.fill(oid, 10, '.43')
    assert s.run_once(), events
    assert e.held[T] == 0 and not e.cancelled and len(e.sent) == 1
    assert intent['target'] == '0.47'  # Never lower the strategy target.
    assert intent['status'] == 'executed'
    assert intent['price_recovery']['filled'] == '10'
    assert any(ev == 'MANUAL_PRICE_MISMATCH_RECOVERED' for ev, _ in events)
    restarted = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **k: None)
    assert restarted.run_once() and len(e.sent) == 1
    e.buy(3, '.42', sign)
    e.clock_value += 2
    assert restarted.run_once() and len(e.sent) == 2
    assert e.sent[-1]['count'] == '3'


@pytest.mark.parametrize('sign', [1, -1])
def test_active_price_change_cancels_confirms_and_restores_original_target(tmp_path, sign):
    e, s, _, events, intent, oid = opened(tmp_path, sign)
    reprice(e, oid, '.44')
    assert s.run_once(), events
    assert e.cancelled == [oid] and intent['status'] == 'canceled'
    assert len(e.sent) == 2 and e.sent[-1]['count'] == '10'
    assert e.sent[-1]['price'] == ('0.4700' if sign > 0 else '0.5300')
    assert intent['target'] == '0.47' and e.held[T] == sign * D(10)
    assert s.run_once() and len(e.sent) == 2


@pytest.mark.parametrize('sign', [1, -1])
def test_partial_repriced_fill_reposts_only_verified_remaining_size(tmp_path, sign):
    e, s, _, events, intent, oid = opened(tmp_path, sign)
    reprice(e, oid, '.44')
    e.fill(oid, 3, '.44')
    assert s.run_once(), events
    assert e.cancelled == [oid]
    assert e.held[T] == sign * D(7)
    assert len(e.sent) == 2 and e.sent[-1]['count'] == '7'
    assert intent['filled'] == '3' and intent['target'] == '0.47'


def test_canceled_repriced_tp_keeps_manual_cancel_intent_but_stop_works(tmp_path):
    e, s, _, events, intent, oid = opened(tmp_path)
    reprice(e, oid, '.44')
    e.remote[oid].update(status='canceled', remaining_count_fp='0')
    assert s.run_once(), events
    assert len(e.sent) == 1 and not e.cancelled
    assert intent['fill_id'] in s.worker.ledger(T)['suppressed_fills']
    e.bid = D('.30')
    assert s.run_once(), events
    assert e.held[T] == 0 and len(e.sent) == 2
    assert e.sent[-1]['reduce_only'] is True


def test_delayed_terminal_fill_never_frees_or_duplicates_inventory(tmp_path):
    e, s, _, _, intent, oid = opened(tmp_path)
    reprice(e, oid, '.43')
    fid = e.fill(oid, 10, '.43')
    e.hidden_fills.add(fid)
    assert not s.run_once()
    assert len(e.sent) == 1 and not e.cancelled
    assert intent['status'] != 'executed'
    e.hidden_fills.clear()
    assert s.run_once()
    assert intent['status'] == 'executed' and len(e.sent) == 1


def test_cancel_404_is_not_terminal_evidence(tmp_path):
    e, s, _, _, intent, oid = opened(tmp_path)
    reprice(e, oid, '.44')
    original = e.request
    def request(method, *args, **kwargs):
        if method == 'DELETE':
            raise KalshiAPIError(404, 'unconfirmed')
        return original(method, *args, **kwargs)
    e.request = request
    assert not s.run_once()
    assert not s.run_once()
    assert len(e.sent) == 1 and e.held[T] == 10
    assert intent.get('cancel_requested') and intent['status'] != 'canceled'


def test_cancel_404_race_requires_terminal_readback_before_reposting(tmp_path):
    e, s, _, events, _, oid = opened(tmp_path)
    reprice(e, oid, '.44')
    original = e.request
    def request(method, *args, **kwargs):
        if method == 'DELETE':
            original(method, *args, **kwargs)
            raise KalshiAPIError(404, 'already removed')
        return original(method, *args, **kwargs)
    e.request = request
    assert s.run_once(), events
    assert e.cancelled == [oid] and len(e.sent) == 2
    assert e.sent[-1]['count'] == '10'


def test_lost_cancel_ack_reconciles_after_restart_without_double_sell(tmp_path):
    e, s, config, _, _, oid = opened(tmp_path)
    reprice(e, oid, '.44')
    e.lose_cancel_ack = True
    assert not s.run_once() and len(e.sent) == 1
    e.lose_cancel_ack = False
    restarted = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **k: None)
    assert restarted.run_once() and len(e.sent) == 2
    assert restarted.run_once() and len(e.sent) == 2
    assert e.held[T] == 10


def test_missing_specific_order_does_not_release_reservation(tmp_path):
    e, s, _, _, intent, oid = opened(tmp_path)
    reprice(e, oid, '.44')
    def missing(*args, **kwargs):
        raise KalshiAPIError(404, 'not found')
    e.order = missing
    assert not s.run_once()
    assert len(e.sent) == 1 and e.held[T] == 10
    assert intent['status'] != 'canceled'


@pytest.mark.parametrize('field,value', [
    ('client_order_id', 'other-client'), ('order_id', 'other-order'),
    ('subaccount_number', 1), ('ticker', 'OTHER'), ('type', 'market'),
    ('initial_count_fp', '11'), ('book_side', 'bid'),
])
def test_price_recovery_does_not_relax_other_identity_checks(tmp_path, field, value):
    e, s, _, _, intent, oid = opened(tmp_path)
    row = copy.deepcopy(e.remote[oid])
    row.update(yes_price_dollars='.44', no_price_dollars='.56')
    row[field] = value
    with pytest.raises(ManualExitBlocked):
        verified_exit_snapshot(s.worker, T, intent, row)
    assert len(e.sent) == 1 and not e.cancelled


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-.01', '0', '1', '.44001'])
def test_invalid_price_cannot_enter_recovery(tmp_path, value):
    e, s, _, _, intent, oid = opened(tmp_path)
    row = copy.deepcopy(e.remote[oid]); row['yes_price_dollars'] = value
    with pytest.raises(ManualExitBlocked):
        verified_exit_snapshot(s.worker, T, intent, row)
    assert len(e.sent) == 1 and not e.cancelled


def test_completed_order_skip_cancel_still_checks_identity_and_counts(tmp_path):
    e, s, _, _, intent, oid = opened(tmp_path)
    e.fill(oid, 10, '.47')
    response = e.cancel_manual_exit(T, intent)
    assert response['already_terminal'] and not e.cancelled
    row = copy.deepcopy(e.remote[oid]); row['client_order_id'] = 'other'
    with pytest.raises(ManualExitBlocked):
        terminal_cancel_receipt(T, intent, row)
    row = copy.deepcopy(e.remote[oid]); row['remaining_count_fp'] = '1'
    with pytest.raises(ManualExitBlocked):
        terminal_cancel_receipt(T, intent, row)


def test_refreshed_correct_order_prevents_unnecessary_cancel(tmp_path):
    e, s, _, _, intent, oid = opened(tmp_path)
    stale = copy.deepcopy(e.remote[oid])
    stale.update(yes_price_dollars='.44', no_price_dollars='.56')
    row, state = verified_exit_snapshot(s.worker, T, intent, stale)
    assert D(row['yes_price_dollars']) == D('.47')
    assert state == ('resting', D(0), D(10))
    assert not e.cancelled and len(e.sent) == 1


def test_terminal_mismatch_with_conflicting_fill_direction_stays_blocked(tmp_path):
    e, s, _, _, _, oid = opened(tmp_path)
    reprice(e, oid, '.43'); e.fill(oid, 10, '.43')
    e.history[-1]['book_side'] = 'bid'
    assert not s.run_once() and len(e.sent) == 1


def test_stop_remains_prioritized_after_repriced_tp_cancel(tmp_path):
    e, s, _, events, _, oid = opened(tmp_path)
    reprice(e, oid, '.44'); e.bid = D('.30')
    assert s.run_once(), events
    assert e.cancelled == [oid] and e.held[T] == 0
    assert len(e.sent) == 2 and e.sent[-1]['time_in_force'] == 'immediate_or_cancel'
