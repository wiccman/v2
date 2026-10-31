"""A delayed acknowledgement must not cancel unrelated confirmed TPs."""

from test_manual_stop_loss import setup
from test_manual_limit_exits import T
from test_resting_manual_exits import exit_ids


def test_hidden_new_tp_keeps_prior_verified_tp_and_pending_reservations(tmp_path):
    e, svc, settings, events = setup(tmp_path)
    e.buy(10, '.42')
    assert svc.run_once()
    first = exit_ids(e)[0]
    assert e.remote[first]['status'] == 'resting'
    e.hide_post = True
    e.buy(6, '.40')
    # POST is accepted, but order/list and direct readback return 404 for a
    # short interval. There is no evidence the order was canceled or rejected.
    assert svc.run_once() is False
    assert len(exit_ids(e)) == 2
    assert e.remote[first]['status'] == 'resting'
    assert e.cancelled == []
    assert any(i['status'] == 'pending_ack' or i['status'] == 'resting'
               for i in svc.worker.intents(T).values())
    assert not any(evt == 'MANUAL_CANCEL_REQUESTED' for evt, _ in events)
    # Unknown acknowledgement cannot justify another new sell.
    assert not svc.run_once()
    assert len(exit_ids(e)) == 2 and not e.cancelled
    e.hidden_orders.clear()
    e.hide_post = False
    assert svc.run_once()
    assert len(exit_ids(e)) == 2 and not e.cancelled
    assert all(e.remote[oid]['status'] == 'resting' for oid in exit_ids(e))


def test_delayed_existing_tp_ack_does_not_cancel_unrelated_verified_tp(tmp_path):
    e, svc, _, events = setup(tmp_path)
    e.buy(10, '.42')
    e.buy(6, '.40')
    assert svc.run_once()
    first, second = exit_ids(e)
    # Read model temporarily hides one known resting sell.
    e.hidden_orders.add(second)
    assert svc.run_once() is False
    assert e.cancelled == []
    assert e.remote[first]['status'] == 'resting'
    assert len(e.sent) == 2
    e.hidden_orders.clear()
    assert svc.run_once()
    assert not e.cancelled and len(e.sent) == 2
