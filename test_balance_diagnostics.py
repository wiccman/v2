import json
from types import SimpleNamespace
import pytest
from balance_diagnostics import balance_report, log_api_cash
from kalshi import KalshiAPIError

@pytest.mark.parametrize('payload,expected', [
    ({'balance':5000},'50'), ({'balance_dollars':'50.0000'},'50.0000'),
    ({'balance':0},'0'), ({'balance_dollars':'0','balance':5000},'0')])
def test_balance_units_and_zero(payload, expected):
    assert balance_report(payload)['cash_dollars'] == expected


def test_separates_portfolio_and_exchange_cash_without_exposing_extra_fields():
    report = balance_report({'balance':5000,'balance_dollars':'50.00',
        'portfolio_value':12345,'updated_ts':123,
        'private_key':'SECRET', 'balance_breakdown':[
            {'exchange_index':0,'balance':'7.00','secret':'SECRET'},
            {'exchange_index':1,'balance':'43.00'}]})
    assert report['portfolio_value_dollars']=='123.45'
    assert report['exchange_balances'][0]['cash_dollars']=='7.00'
    assert report['balance_fields_agree_to_cent'] is True
    assert 'SECRET' not in json.dumps(report)

@pytest.mark.parametrize('payload',[{}, {'balance_dollars':'NaN'}, {'balance':'bad'}])
def test_invalid_balance_does_not_become_zero(payload,capsys):
    r=log_api_cash(SimpleNamespace(balance=lambda:payload))
    assert r['event']=='API_CASH_BALANCE_ERROR' and 'cash_dollars' not in r


def test_read_only_and_error_redaction(capsys):
    calls=[]
    def read():
        calls.append('balance')
        raise KalshiAPIError(401,'PRIVATE KEY MUST NOT BE PRINTED')
    r=log_api_cash(SimpleNamespace(balance=read))
    assert calls==['balance'] and r['http_status']==401
    assert 'PRIVATE KEY' not in capsys.readouterr().out


def test_subaccount_cash_is_dollars_and_scopes_stay_separate():
    from balance_diagnostics import subaccount_report
    payload = {'subaccount_balances': [
        {'subaccount_number': 0, 'exchange_index': 2, 'balance': '0.0028', 'updated_ts': 1},
        {'subaccount_number': 1, 'exchange_index': 2, 'balance': '50.4700', 'secret': 'HIDDEN'},
        {'subaccount_number': 1, 'exchange_index': 0, 'balance': '0.0000'}]}
    rows = subaccount_report(payload)['subaccount_balances']
    assert [r['cash_dollars'] for r in rows] == ['0.0028', '50.4700', '0.0000']
    assert [r['subaccount_number'] for r in rows] == [0, 1, 1]
    assert [r['exchange_index'] for r in rows] == [2, 2, 0]
    assert 'HIDDEN' not in json.dumps(rows)


def test_monitor_reads_both_scopes_after_default_error(capsys):
    from balance_diagnostics import BalanceMonitor
    calls = []
    def default():
        calls.append('default')
        raise KalshiAPIError(403, 'SECRET')
    def all_accounts():
        calls.append('all')
        return {'subaccount_balances': []}
    BalanceMonitor(SimpleNamespace(balance=default, subaccount_balances=all_accounts)).run_once()
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert calls == ['default', 'all']
    assert lines[0]['event'] == 'API_CASH_BALANCE_ERROR'
    assert lines[1]['event'] == 'API_SUBACCOUNT_BALANCES'
    assert 'SECRET' not in json.dumps(lines)


def test_subaccount_restricted_error_is_not_reported_as_zero(capsys):
    from balance_diagnostics import log_subaccount_cash
    def denied():
        raise KalshiAPIError(403, 'SECRET')
    report = log_subaccount_cash(SimpleNamespace(subaccount_balances=denied))
    assert report['http_status'] == 403
    assert 'subaccount_balances' not in report
    assert 'SECRET' not in capsys.readouterr().out


def test_subaccount_endpoint_only_performs_get():
    from kalshi import KalshiClient
    client = KalshiClient()
    calls = []
    client.request = lambda *args, **kwargs: calls.append((args, kwargs)) or {'subaccount_balances': []}
    assert client.subaccount_balances() == {'subaccount_balances': []}
    assert calls == [(('GET', '/portfolio/subaccounts/balances'), {'auth': True})]
