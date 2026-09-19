from decimal import Decimal as D
import pytest
from mm_reconcile import external_reductions
from market_maker import ledger


def fill(fid, oid, side, quantity, price, fee='0', time='2026-09-19T02:30:36Z'):
    return dict(fill_id=fid, order_id=oid, ticker='BTC', book_side=side,
                outcome_side='yes' if side == 'bid' else 'no', count_fp=quantity,
                yes_price_dollars=price, fee_cost=fee, created_time=time,
                # These legacy fields intentionally contradict the canonical ones.
                action='sell', side='no' if side == 'ask' else 'yes')


def record():
    return {'orders': [{'order_id': str(i), 'side': 'ask', 'filled': '1', 'price': str(D('.45')+i*D('.01')), 'fees': '0'} for i in range(5)]}


def history():
    return [fill('f'+str(i), str(i), 'ask', '1', str(D('.45')+i*D('.01'))) for i in range(5)]


def test_live_incident_external_close_reconciles_without_resetting_pnl():
    r = record(); rows = history()+[fill('manual', 'manual-order', 'bid', '5', '.41', '.0847', '2026-09-19T02:35:30Z')]
    r['external_fills'] = external_reductions(r, rows, 'BTC', D(0))
    assert ledger(r) == (D(0), D('.2153'))
    r['external_fills'] = external_reductions(r, rows+rows, 'BTC', D(0))
    assert ledger(r) == (D(0), D('.2153'))  # duplicate pages and restarts do not double-credit


def test_partial_external_close_keeps_remaining_inventory_and_fees():
    r=record(); rows=history()+[fill('m', 'manual', 'bid', '2', '.50', '.03', '2026-09-19T02:35:30Z')]
    r['external_fills']=external_reductions(r, rows, 'BTC', D(-3))
    assert ledger(r)==(D(-3),D('1.32'))


@pytest.mark.parametrize('change', ['missing', 'wrong_direction', 'missing_fee', 'conflict', 'unexplained', 'overclose', 'increase'])
def test_ambiguous_or_foreign_exposure_remains_blocked(change):
    r=record(); rows=history(); actual=D(-5)
    if change=='missing': rows.pop()
    if change=='wrong_direction': rows[0]['book_side']='bid'
    if change=='missing_fee': rows[0].pop('fee_cost')
    if change=='conflict': rows.append(dict(rows[0], count_fp='2'))
    if change=='unexplained': actual=D(0)
    if change in ('overclose','increase'):
        rows.append(fill('m','manual','bid' if change=='overclose' else 'ask','6' if change=='overclose' else '1','.5',time='2026-09-19T02:35:30Z'))
    with pytest.raises((ValueError,KeyError)):
        external_reductions(r,rows,'BTC',actual)
    assert 'external_fills' not in r


def test_finalized_position_zero_does_not_erase_settlement_exposure():
    r=record(); r['external_fills']=external_reductions(r,history(),'BTC',D(0),finalized=True)
    r.update(settled=True,settlement='1')
    assert ledger(r)==(D(0),D('-2.65'))
