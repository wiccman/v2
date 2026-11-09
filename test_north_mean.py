from decimal import Decimal as D
import pytest
import leader_mean15 as leader
import performance_ranked_minute9 as ranked
from test_leader_mean15 import START, Feed, Exchange

@pytest.mark.parametrize('values,expected', [
    ([-.1, 1, 0, 2, -2.9], 'ETH'),
    ([0, 1, -4, 1.5, 1.5], 'ETH'),
    ([-3.1, -2, -3, -1, -5.9], 'ETH'),
    ([-4, .5, .1, 1, 2.4], 'ETH'),
    ([4, -1, -1, -1, -1], 'BTC'),
    ([-1, -1, 4, -1, -1], None),
    ([0, 0, 0, 0, 0], None),
    ([1, 1, -4, 1, 1], None),
])
def test_north_only(values, expected):
    rows = [ranked.Observation(leader.COINS[k], START, D(100),
            D(100)+D(str(v)), START+60) for k,v in zip(leader.COINS, values)]
    if expected is None:
        with pytest.raises(ranked.PerformanceUnavailable):
            leader.coin_mean_choice(rows, START, START+60)
    else:
        coin, mean, returns = leader.coin_mean_choice(rows, START, START+60)
        assert coin == expected
        assert mean == sum(returns.values()) / 5
        assert returns[coin] > mean

def test_old_cached_policy_never_submits(tmp_path):
    runner = leader.LeaderRunner(tmp_path/'leader.json', provider=Feed(None),
                                 clock=lambda:START+180)
    venue = Exchange()
    venue.book['no_ask_dollars'] = '1.00'
    runner.run(venue, live=True)
    saved = runner.journal.data['leader_opening_signals'][str(START)]
    assert saved.pop('selection_policy') == leader.SELECTION_POLICY
    runner.journal.save()
    venue.book['no_ask_dollars'] = '.75'
    runner.run(venue, live=True)
    assert not venue.submissions
