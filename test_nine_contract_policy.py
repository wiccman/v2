"""Offline regression tests for the requested 9-contract sizing change.

These test the pure policy and receipt math, not live order execution.
"""
import socket

import pytest

from two_rule_policy import (
    D, FINAL, DIRECTIONAL, RULES, FEE_RESERVE,
    Receipt, candidate, config, receipt, target,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network forbidden in policy tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def market(ask='0.96'):
    return {'floor_strike': '100000', 'yes_ask_dollars': ask,
            'no_ask_dollars': ask}


GRID = [{'start': '0', 'end': '1', 'step': '.001'}]


def test_config_preserves_sizes_and_sets_final_30c_goal():
    assert config()['rules'] == [
        {'name': 'final_2m_50', 'strike_distance_dollars': '50',
         'contracts': 9, 'gross_profit_dollars': '0.30',
         'last_seconds': 120, 'exact_ask': '0.96'},
        {'name': 'directional_100', 'strike_distance_dollars': '100',
         'contracts': 9, 'gross_profit_dollars': '1.00',
         'last_seconds': None, 'exact_ask': None},
    ]
    assert config()['profit_basis'] == 'gross_before_fees'
    assert config()['fixed_98c_or_99c_exit'] is False
    assert FEE_RESERVE == D('.03')


@pytest.mark.parametrize('side,sign', [('YES', 1), ('NO', -1)])
@pytest.mark.parametrize('left', ['0.001', '120'])
def test_final_boundary(side, sign, left):
    result = candidate(FINAL, market(), D('100000') + sign * 50, left)
    assert (result.side, result.contracts, result.price, result.profit) == (
        side, 9, D('.96'), D('.30'))
    assert D(result.contracts) * (result.price + FEE_RESERVE) == D('8.91')


@pytest.mark.parametrize('left', ['-1', '0', '120.001', '901'])
def test_final_time_rejection_unchanged(left):
    assert candidate(FINAL, market(), '100050', left) is None


@pytest.mark.parametrize('ask', ['.959', '.961', '.99'])
def test_final_exact_ask_unchanged(ask):
    assert candidate(FINAL, market(ask), '100050', 60) is None


@pytest.mark.parametrize('rule', RULES)
@pytest.mark.parametrize('sign', [1, -1])
def test_distance_just_under_threshold_does_not_qualify(rule, sign):
    spot = D('100000') + sign * (rule.distance - D('.01'))
    assert candidate(rule, market(), spot, 60) is None


@pytest.mark.parametrize('side,sign', [('YES', 1), ('NO', -1)])
@pytest.mark.parametrize('left', ['900', '121', '1'])
@pytest.mark.parametrize('ask', ['.0001', '.60', '.96', '.9999'])
def test_directional_has_no_final_window_or_96c_filter(side, sign, left, ask):
    result = candidate(DIRECTIONAL, market(ask), D('100000') + sign * 100, left)
    assert (result.side, result.contracts, result.price, result.profit) == (
        side, 9, D(ask), D('1.00'))
    assert 9 * (result.price + FEE_RESERVE) < D('10')


@pytest.mark.parametrize('rule', RULES)
@pytest.mark.parametrize('left', ['0', '-1', '900.001'])
def test_closed_or_unopened_market_cannot_qualify(rule, left):
    assert candidate(rule, market(), '100100', left) is None


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_saved_nine_contract_40c_goal_is_not_rewritten(side):
    observed = Receipt(D(9), D(0), D('8.64'), D(0))
    assert D(9) - observed.cost == D('.36')
    assert target({'side': side, 'profit': '.40'}, observed, GRID) is None


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_directional_target_uses_nine_actual_fills(side):
    observed = Receipt(D(9), D(0), D('5.40'), D(0))
    wanted = target({'side': side, 'profit': '1.00'}, observed, GRID)
    assert wanted == D('.712')
    assert wanted * 9 - observed.cost >= D(1)


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_directional_high_entry_target_unreachable_without_fallback(side):
    observed = Receipt(D(9), D(0), D('8.64'), D(0))
    assert target({'side': side, 'profit': '1.00'}, observed, GRID) is None


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_partial_exit_keeps_original_total_profit_goal(side):
    observed = Receipt(D(9), D(4), D('5.40'), D('2.88'))
    assert target({'side': side, 'profit': '1.00'}, observed, GRID) == D('.704')


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_existing_eleven_contract_receipt_is_not_resized(side):
    observed = Receipt(D(11), D(0), D('10.56'), D(0))
    assert target({'side': side, 'profit': '.40'}, observed, GRID) == D('.997')


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_partial_fill_receipt_does_not_claim_all_nine_filled(side):
    trade = {'side': side, 'orders': [
        {'order_id': 'entry', 'role': 'entry', 'price': '.60', 'quantity': '9'}]}
    fills = [{'order_id': 'entry', 'count_fp': '3',
              'book_side': 'bid' if side == 'YES' else 'ask',
              'yes_price_dollars': '.60' if side == 'YES' else '.40'}]
    observed = receipt(trade, fills)
    assert (observed.entered, observed.remaining, observed.cost) == (
        D(3), D(3), D('1.80'))
    assert target({'side': side, 'profit': '1.00'}, observed, GRID) == D('.934')


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('step,wanted', [('.001', '.994'), ('.0001', '.9934'), ('.01', None)])
def test_final_30c_target_rounds_up_to_actual_market_grid(side, step, wanted):
    observed = Receipt(D(9), D(0), D('8.64'), D(0))
    grid = [{'start': '0', 'end': '1', 'step': step}]
    result = target({'side': side, 'profit': str(FINAL.profit)}, observed, grid)
    assert result == (D(wanted) if wanted is not None else None)
    if result is not None:
        assert result * observed.remaining - observed.cost >= D('.30')
        assert (result - D(step)) * observed.remaining - observed.cost < D('.30')


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('quantity', range(1, 10))
def test_final_30c_target_uses_actual_fill_count(side, quantity):
    observed = Receipt(D(quantity), D(0), D(quantity) * D('.96'), D(0))
    wanted = target({'side': side, 'profit': str(FINAL.profit)}, observed, GRID)
    expected = {8: D('.998'), 9: D('.994')}.get(quantity)
    assert wanted == expected


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_final_partial_exit_retains_total_30c_goal(side):
    observed = Receipt(D(9), D(4), D('8.64'), D('3.976'))
    wanted = target({'side': side, 'profit': str(FINAL.profit)}, observed, GRID)
    assert wanted == D('.993')
    assert observed.proceeds + wanted * observed.remaining - observed.cost == D('.301')


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_final_30c_goal_uses_price_improved_fill_cost(side):
    observed = Receipt(D(9), D(0), D('8.595'), D(0))
    assert target({'side': side, 'profit': str(FINAL.profit)}, observed, GRID) == D('.989')


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_final_30c_target_does_not_sell_flat_inventory(side):
    observed = Receipt(D(9), D(9), D('8.64'), D('8.946'))
    assert target({'side': side, 'profit': str(FINAL.profit)}, observed, GRID) is None
