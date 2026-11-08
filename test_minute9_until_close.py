"""Offline regressions for M9_ENTRY_END_SECONDS=900; never use real accounts."""
from dataclasses import replace
from decimal import Decimal as D
import pytest
import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
import cash_checked_minute9 as safe
from test_single_coin_minute9 import START, market
from test_cash_checked_minute9 import CashExchange
from test_performance_ranked_minute9 import observations


@pytest.fixture(autouse=True)
def until_close(monkeypatch):
    monkeypatch.setattr(core, 'ENTRY_END_SECONDS', 900)


@pytest.mark.parametrize('elapsed,allowed', [(0,False),(539.999,False),(540,True),
    (599.999,True),(600,True),(660,True),(720,True),(780,True),(840,True),
    (899.999,True),(900,False),(901,False)])
def test_entry_boundaries(elapsed, allowed):
    assert bool(core.plan_single_coin(market(),START+elapsed,'KXBTC15M')) is allowed


class Exchange(CashExchange):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadlines=[]
    def place_entry(self,*args,**kwargs):
        self.deadlines.append((args[4],kwargs['submit_before']))
        return super().place_entry(*args,**kwargs)


class Provider:
    def __init__(self, now):
        self.now=now
    def snapshot(self, names, start):
        return [replace(r, observed_at=self.now[0]) for r in observations()]


def run(c,j,now):
    with safe.installed_guard():
        ranked.run_ranked_pass(c,j,tuple(ranked.PRODUCTS),D('10'),100,True,now[0],
                              provider=Provider(now),clock=lambda:now[0])


@pytest.mark.parametrize('elapsed',[540,600,660,720,780,840,899.5])
def test_funded_ranked_entry_after_nine_and_exact_close_deadline(tmp_path,monkeypatch,elapsed):
    now=[START+elapsed]
    monkeypatch.setattr(core.time,'time',lambda:now[0])
    j=core.Journal(tmp_path/'journal.json'); c=Exchange()
    run(c,j,now)
    assert len(c.posted)==1
    assert c.posted[0][0].startswith('KXETH15M-')
    assert c.posted[0][2]==4 and c.posted[0][3]==D('.75')
    assert c.deadlines==[(START+900,START+900)]
    # The longer window must not allow a second market after an uncertain first.
    now[0]=START+899.75
    run(c,core.Journal(j.path),now)
    assert len(c.posted)==1


def test_priced_out_leader_moves_to_second_after_minute_twelve(tmp_path,monkeypatch):
    now=[START+750]; monkeypatch.setattr(core.time,'time',lambda:now[0])
    c=Exchange(); c.initial['KXETH15M']='.94'
    run(c,core.Journal(tmp_path/'fallback.json'),now)
    assert len(c.posted)==1 and c.posted[0][0].startswith('KXSOL15M-')


def test_price_can_become_eligible_later_in_same_window(tmp_path,monkeypatch):
    now=[START+550]; monkeypatch.setattr(core.time,'time',lambda:now[0])
    c=Exchange(); c.initial={s:'.94' for s in ranked.PRODUCTS}
    j=core.Journal(tmp_path/'later.json'); run(c,j,now)
    assert not c.posted and not j.data['windows']
    now[0]=START+730; c.initial['KXETH15M']='.75'
    run(c,j,now)
    assert len(c.posted)==1 and c.posted[0][0].startswith('KXETH15M-')


def test_cash_read_crossing_close_cannot_submit(tmp_path,monkeypatch):
    now=[START+899.5]; monkeypatch.setattr(core.time,'time',lambda:now[0])
    class Slow(Exchange):
        def balance(self):
            result=super().balance(); now[0]=START+900
            return result
    c=Slow();j=core.Journal(tmp_path/'deadline.json');run(c,j,now)
    assert not c.posted
    assert next(iter(j.data['windows'].values()))['outcome']['reason']=='entry_deadline_passed'


@pytest.mark.parametrize('elapsed',[539.99,900,901])
def test_ranker_does_not_fetch_or_buy_before_start_or_after_close(tmp_path,monkeypatch,elapsed):
    now=[START+elapsed];monkeypatch.setattr(core.time,'time',lambda:now[0])
    class Forbidden:
        def snapshot(self,*args): pytest.fail('Out-of-window feed request')
    c=Exchange();j=core.Journal(tmp_path/'closed.json')
    with safe.installed_guard():
        ranked.run_ranked_pass(c,j,tuple(ranked.PRODUCTS),D(10),100,True,now[0],
                              provider=Forbidden(),clock=lambda:now[0])
    assert not c.posted


def test_confirmed_zero_fill_can_use_backup_late(tmp_path,monkeypatch):
    now=[START+850];monkeypatch.setattr(core.time,'time',lambda:now[0])
    c=Exchange({'KXETH15M':'zero'})
    c.after_post=lambda: now.__setitem__(0,now[0]+1.1)
    j=core.Journal(tmp_path/'zero.json');run(c,j,now)
    assert [r[0].split('-')[0] for r in c.posted]==['KXETH15M','KXSOL15M']


def test_partial_fill_cannot_use_backup_late(tmp_path,monkeypatch):
    now=[START+850];monkeypatch.setattr(core.time,'time',lambda:now[0])
    c=Exchange({'KXETH15M':'partial'});j=core.Journal(tmp_path/'partial.json');run(c,j,now)
    assert len(c.posted)==1 and core.window_entry_count(j,START*1000)==1


def test_stop_take_profit_and_inverted_budget_sequence():
    assert core.exit_decision('.75','.45')[0]=='stop_loss'
    assert core.exit_decision('.75','.80')[0]=='take_profit'
    assert core.exit_decision('.75','.46')==(None,None)
    streak=core.starting_streak()
    assert core.trade_budget(streak,D(10))==D(10)/3
    streak=core.next_streak(streak,'loss')
    assert core.trade_budget(streak,D(10))==D(10)
    streak=core.next_streak(core.next_streak(streak,'win'),'win')
    assert core.trade_budget(streak,D(10))==D(10)/3
