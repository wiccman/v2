"""Change the configured resting TP from five cents to three on a running journal."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from manual_exit_runner import ManualExitService
from manual_limit_exits import ManualExitBlocked
from test_manual_limit_exits import T
from test_manual_order_edits import opened, reprice


def restart(exchange, settings, events):
    return ManualExitService(exchange, settings, clock=exchange.clock,
                             emit=lambda event, **data: events.append((event, data)))


@pytest.mark.parametrize("sign", [1, -1])
def test_reprice_only_remaining_size_after_confirmed_cancel(tmp_path, sign):
    e, _, settings, events, _, original = opened(tmp_path, sign)
    e.fill(original, 3, ".65")
    settings = replace(settings, increment=D(".03"), stop_loss_cents=D("15"))
    service = restart(e, settings, events)
    e.delay_cancel = True
    service.run_once()
    assert original in e.cancelled and len(e.sent) == 1
    e.delay_cancel = False
    e.clock_value += 6
    service = restart(e, settings, events)
    for _ in range(3):
        service.run_once()
    assert len(e.sent) == 2 and e.held[T] == sign * 7
    assert e.sent[-1]["count"] == "7" and e.sent[-1]["price"] == ("0.6300" if sign == 1 else "0.3700")
    assert sum(event == "MANUAL_TP_POLICY_MIGRATED" for event, _ in events) == 1
    assert service.worker.stop_loss_increment == D(".15")


def test_keep_operator_price_and_use_three_cents_for_next_fill(tmp_path):
    e, _, settings, events, _, original = opened(tmp_path)
    reprice(e, original, ".69")
    service = restart(e, replace(settings, increment=D(".03")), events)
    e.buy(5, ".58")
    assert service.run_once()
    assert not e.cancelled and D(e.remote[original]["yes_price_dollars"]) == D(".69")
    assert len(e.sent) == 2 and e.sent[-1]["price"] == "0.6100"


def test_other_offsets_still_reject_without_modifying_journal(tmp_path):
    e, _, settings, events, _, _ = opened(tmp_path)
    before = settings.manual_state.read_bytes()
    with pytest.raises(ManualExitBlocked, match="target policy changed"):
        restart(e, replace(settings, increment=D(".04")), events)
    assert settings.manual_state.read_bytes() == before and not e.cancelled
