"""Read-only, credential-free cash diagnostics for Railway stdout."""
import json
from datetime import datetime, timezone
from decimal import Decimal


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Invalid cash value')
    return result


def balance_report(payload):
    dollars = payload.get('balance_dollars')
    cents = payload.get('balance')
    if dollars is None and cents is None:
        raise ValueError('Cash balance missing')
    cash = amount(dollars) if dollars is not None else amount(cents) / 100
    report = {'cash_dollars': format(cash, 'f'),
              'cash_source': 'balance_dollars' if dollars is not None else 'balance_cents',
              'account_scope': 'primary; all exchange indexes'}
    if cents is not None:
        report['balance_cents'] = format(amount(cents), 'f')
        if dollars is not None:
            report['balance_fields_agree_to_cent'] = abs(cash - amount(cents) / 100) < Decimal('0.01')
    if payload.get('portfolio_value') is not None:
        report['portfolio_value_dollars'] = format(amount(payload['portfolio_value']) / 100, 'f')
    if payload.get('updated_ts') is not None:
        report['balance_updated_ts'] = int(payload['updated_ts'])
    if payload.get('balance_breakdown') is not None:
        report['exchange_balances'] = [
            {'exchange_index': int(row['exchange_index']),
             'cash_dollars': format(amount(row['balance']), 'f')}
            for row in payload['balance_breakdown']]
    return report


def log_api_cash(client):
    """One GET; never place orders, alter state, or print raw errors/credentials."""
    report = {'event': 'API_CASH_BALANCE',
              'time_utc': datetime.now(timezone.utc).isoformat()}
    try:
        report.update(balance_report(client.balance()))
    except Exception as error:
        report.update(event='API_CASH_BALANCE_ERROR', error_type=type(error).__name__)
        status = getattr(error, 'status_code', None)
        if isinstance(status, int):
            report['http_status'] = status
    print(json.dumps(report), flush=True)
    return report
