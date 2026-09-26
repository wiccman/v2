"""Read-only, credential-free cash diagnostics for Railway stdout."""
import json
import threading
from datetime import datetime, timezone
from decimal import Decimal


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Invalid cash value')
    return result


def balance_report(payload, exchange_index=None):
    dollars = payload.get('balance_dollars')
    cents = payload.get('balance')
    if dollars is None and cents is None:
        raise ValueError('Cash balance missing')
    cash = amount(dollars) if dollars is not None else amount(cents) / 100
    report = {'cash_dollars': format(cash, 'f'),
              'cash_source': 'balance_dollars' if dollars is not None else 'balance_cents',
              'account_scope': 'default API account; all exchange indexes',
              'balance_kind': 'available_cash', 'requested_subaccount': 'omitted'}
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
    if exchange_index is not None:
        report['exchange_index'] = exchange_index
        report['account_scope'] = f'primary account; exchange shard {exchange_index}'
        # Never substitute the aggregate or another shard for a missing row.
        if 'exchange_balances' in report:
            matches = [r for r in report['exchange_balances'] if r['exchange_index'] == exchange_index]
            if len(matches) != 1:
                raise ValueError('Requested exchange balance missing or ambiguous')
            report['cash_dollars'] = matches[0]['cash_dollars']
            report['cash_source'] = 'exchange_balance_breakdown'
            if cents is not None:
                report['balance_fields_agree_to_cent'] = abs(amount(report['cash_dollars']) - amount(cents) / 100) < Decimal('0.01')
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


def subaccount_report(payload):
    # Do not print raw API payloads or treat a failed lookup as a zero balance.
    rows = []
    for row in payload['subaccount_balances']:
        item = {'subaccount_number': int(row['subaccount_number']),
                'exchange_index': int(row['exchange_index']),
                'cash_dollars': format(amount(row['balance']), 'f')}
        if row.get('updated_ts') is not None:
            item['balance_updated_ts'] = int(row['updated_ts'])
        rows.append(item)
    return {'account_scope': 'all subaccounts returned to this API key',
            'subaccount_balances': rows}


def log_subaccount_cash(client):
    report = {'event': 'API_SUBACCOUNT_BALANCES',
              'time_utc': datetime.now(timezone.utc).isoformat()}
    try:
        report.update(subaccount_report(client.subaccount_balances()))
    except Exception as error:
        report.update(event='API_SUBACCOUNT_BALANCES_ERROR', error_type=type(error).__name__)
        status = getattr(error, 'status_code', None)
        if isinstance(status, int):
            report['http_status'] = status
    print(json.dumps(report), flush=True)
    return report


class BalanceMonitor:
    """Independent GET-only diagnostics; no entry state or order routing access."""
    def __init__(self, client, interval=60):
        self.client = client
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = None

    def run_once(self):
        log_api_cash(self.client)
        log_subaccount_cash(self.client)

    def run(self):
        while not self.stopped.is_set():
            self.run_once()
            self.stopped.wait(self.interval)

    def start(self):
        self.thread = threading.Thread(target=self.run, name='balance-diagnostics', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
