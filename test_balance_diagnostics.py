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
