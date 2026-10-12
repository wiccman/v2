"""Composition and locking tests; no real account, credentials or network."""
import copy
import threading
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import hybrid_runtime
from manual_limit_exits import ManualLimitExitWorker
from take_profit import TakeProfitMonitor
from test_manual_limit_exits import Exchange, T


def module(tmp_path):
    return SimpleNamespace(TakeProfitMonitor=TakeProfitMonitor,
        STATE=tmp_path/'state.json', funded_entry=lambda *a, **k: ('unchanged', a, k))


def enabled(**extra):
    return {'MANUAL_EXITS_ENABLED':'true', 'MANUAL_EXTERNAL_ORDERS_ARE_MANUAL':'true', **extra}


def test_disabled_does_not_wrap_or_create_journals(tmp_path):
    bot=module(tmp_path)
    original=dict(vars(bot))
    assert hybrid_runtime.configure(bot, {}) is False
    assert vars(bot)==original and not list(tmp_path.iterdir())


def test_factory_uses_separate_journal_and_default_dry_run(tmp_path):
    bot=module(tmp_path)
    assert hybrid_runtime.configure(bot,enabled())
    e=Exchange()
    monitor=bot.TakeProfitMonitor(e, lambda:{'markets':{}}, tmp_path/'auto.json',
        pairs={D('.45'):D('.55')},per_order_profit=D('.20'))
    assert isinstance(monitor,TakeProfitMonitor)
    manual=monitor.manual_exits
    assert manual.dry_run and manual.profit_goal==D('.25')
    assert monitor.per_order_profit==D('.20')
    assert manual.path==tmp_path/'state_manual_exits_v2.json'
    assert bot.funded_entry(1, foo='bar')==('unchanged',(1,),{'foo':'bar'})


def test_factory_real_monitor_runs_manual_detection(tmp_path,monkeypatch):
    bot=module(tmp_path)
    # Factory production clock is real wall time; offline fills use that epoch.
    e=Exchange()
    import time
    e.clock_value=time.time()+.01
    hybrid_runtime.configure(bot,enabled(MANUAL_EXITS_DRY_RUN='false'))
    monitor=bot.TakeProfitMonitor(e, lambda:{'markets':{}}, tmp_path/'auto.json',
        pairs={D('.45'):D('.55')}, quote_gate=True)
    e.close=e.clock_value+900
    e.buy();e.bid=D('.43')
    monitor.run_once();monitor.run_once()
    assert len(e.sent)==1 and e.held[T]==0


@pytest.mark.parametrize('config',[
    {'MANUAL_EXITS_ENABLED':'yes'},
    {'MANUAL_EXITS_ENABLED':'true'},
    enabled(MANUAL_EXIT_MODE='resting'),
    enabled(MANUAL_EXITS_DRY_RUN='1'),
    enabled(MANUAL_EXIT_SERIES=''),
    enabled(MANUAL_EXIT_SERIES='*'),
])
def test_invalid_or_unacknowledged_configuration_does_not_mutate_bot(tmp_path,config):
    bot=module(tmp_path);before=dict(vars(bot))
    with pytest.raises(ValueError):hybrid_runtime.configure(bot,config)
    assert vars(bot)==before


def test_configure_twice_is_rejected(tmp_path):
    bot=module(tmp_path);hybrid_runtime.configure(bot,enabled())
    with pytest.raises(RuntimeError):hybrid_runtime.configure(bot,enabled())


def test_entry_execution_serializes_against_exit_reconciliation(tmp_path):
    entered=threading.Event();finished=threading.Event();started=threading.Event()
    bot=module(tmp_path)
    bot.funded_entry=lambda:entered.set()
    hybrid_runtime.configure(bot,enabled())
    monitor=bot.TakeProfitMonitor(Exchange(),lambda:{'markets':{}},tmp_path/'auto.json',
        pairs={D('.45'):D('.55')})
    def run():
        started.set();bot.funded_entry();finished.set()
    with monitor.execution_lock:
        worker=threading.Thread(target=run)
        worker.start();assert started.wait(1)
        assert not entered.wait(.02)
    worker.join(1)
    assert entered.is_set() and finished.is_set()


def test_main_disabled_delegates_without_network(monkeypatch,tmp_path):
    import sys
    called=[]
    fake=module(tmp_path);fake.main=lambda:called.append(True)
    monkeypatch.setitem(sys.modules,'bot',fake)
    monkeypatch.setenv('MANUAL_EXITS_ENABLED','false')
    hybrid_runtime.main()
    assert called==[True] and not getattr(fake,'_hybrid_configured',False)
