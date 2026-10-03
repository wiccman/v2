"""Manual-trade priority for the two-rule runner; no I/O at import.

Manual controls belong to the user. The bot must not inherit flip-selling.
A confirmed external fill on a watched market latches a pause until close.
Polling is not atomic with another trader; exchange reduce-only IOC exits
remain mandatory as the last defense against a position reversal.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from two_rule_policy import D, ZERO, number


class ManualTradeGuard:
    VERSION = 'manual-priority-v1'
    TERMINAL = {'executed', 'canceled', 'expired'}

    def __init__(self, runner: Any, pending_error: type[Exception]):
        self.runner = runner
        self.pending_error = pending_error

    def require_active(self, record: dict[str, Any]) -> None:
        if record.get('manual_control_pause'):
            raise self.pending_error('Manual-control pause remains until market close')

    def pause(self, ticker: str, record: dict[str, Any], reason: str, **details: Any) -> None:
        if not record.get('manual_control_pause'):
            record['manual_control_pause'] = {
                'version': self.VERSION, 'reason': reason,
                'detected_at': self.runner.clock(), **details,
            }
            self.runner.store.save()  # Restart must not silently re-enable this market.
            self.runner.emit('TWO_RULE_MANUAL_PAUSE', ticker=ticker,
                             **record['manual_control_pause'])
        raise self.pending_error('Manual-control pause: ' + reason)

    @staticmethod
    def fill_time(fill: dict[str, Any]) -> D:
        if fill.get('ts') is not None:
            return number(fill['ts'], 'external fill timestamp')
        value = datetime.fromisoformat(fill['created_time'].replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError('External fill time lacks timezone')
        return D(str(value.timestamp()))

    def inspect(self, ticker: str, record: dict[str, Any], fills: list[Any],
                known: dict[str, Any]) -> None:
        """Call after resolving bot ACKs, before attributing ownership or selling."""
        self.require_active(record)
        external = {str(f.get('fill_id') or f.get('trade_id')): f
                    for f in fills if f.get('order_id') not in known}
        baseline = record.get('manual_fill_baseline')
        if baseline is None:
            # For an upgraded ledger do not baseline away a manual close/rebuy
            # that happened after an existing bot request.
            requests = [o for t in record['trades'] for o in t['orders']
                        if o['role'] == 'entry' and not o.get('unsubmitted')]
            if requests and external:
                first = min(number(o['requested_at'], 'bot request time') for o in requests)
                if any(self.fill_time(f) >= first for f in external.values()):
                    self.pause(ticker, record, 'external_fill_after_bot_request')
            record['manual_fill_baseline'] = sorted(external)
            self.runner.store.save()
        elif not isinstance(baseline, list):
            raise self.pending_error('Invalid manual-fill baseline; no reset permitted')
        elif set(external) - set(baseline):
            self.pause(ticker, record, 'new_external_fill',
                       new_fill_ids=sorted(set(external) - set(baseline)))
        elif set(baseline) - set(external):
            raise self.pending_error('External fill history incomplete; wait for consistent history')
        # A resting manual/other-client order can flip the account on its next
        # fill. Wait without canceling that order or silently treating it as ours.
        for remote in self.runner.exits.all_orders(ticker):
            if remote.get('status') in self.TERMINAL:
                continue
            if (remote.get('ticker') or remote.get('market_ticker')) != ticker:
                raise self.pending_error('Unscoped open order; ownership unavailable')
            if (remote.get('order_id') not in known or
                    remote.get('client_order_id') != known[remote['order_id']]['client_id']):
                self.runner.notice('TWO_RULE_EXTERNAL_ORDER_WAIT', ticker,
                                   order_id=remote.get('order_id'),
                                   action='leave_manual_order_unchanged')
                raise self.pending_error('Untracked open order in this market; bot waits')

    def before_submit(self, ticker: str, record: dict[str, Any], observations: dict[str, Any],
                      account: D | None = None) -> D:
        """Fresh ownership/position/working-order snapshot for either buy or sell."""
        self.require_active(record)
        fresh, latest, _ = self.runner.reconcile(ticker, record)
        if fresh != observations:
            raise self.pending_error('Bot fills changed; recalculate quantity and target next cycle')
        if account is not None and latest != account:
            raise self.pending_error('Account changed before submission; recheck ownership')
        if any(o['role'] == 'exit' and not o.get('terminal')
               for t in record['trades'] for o in t['orders']):
            raise self.pending_error('Existing exit unresolved; do not reuse its inventory')
        return latest

    def cancel_bot_entries(self, ticker: str, record: dict[str, Any]) -> None:
        """On a pause/sync failure, retire only independently identified bot buys.

        A cancel request is NOT a confirmed cancellation or released allowance.
        Unknown acknowledgements are recovered by exact client ID, never guessed.
        Manual orders, credentials, positions and profit targets are untouched.
        """
        for trade in record['trades']:
            for order in trade['orders']:
                if (order['role'] != 'entry' or order.get('terminal') or
                        order.get('guard_cancel_confirmed')):
                    continue
                if self.runner.clock() < order.get('guard_cancel_retry_at', 0):
                    continue
                try:
                    remote = self.runner._remote(ticker, order)
                    if ((remote.get('ticker') or remote.get('market_ticker')) != ticker or
                            remote.get('client_order_id') != order['client_id']):
                        raise self.pending_error('Cannot verify cancellation ownership')
                    if remote.get('status') in self.TERMINAL:
                        order['guard_cancel_confirmed'] = True
                        self.runner.store.save()
                        self.runner.emit('TWO_RULE_GUARD_ORDER_TERMINAL', ticker=ticker,
                                         order_id=order['order_id'], status=remote['status'])
                        continue
                    order['guard_cancel_retry_at'] = self.runner.clock() + 3
                    self.runner.store.save()
                    self.runner.client.cancel(order['order_id'], ticker)
                    self.runner.emit('TWO_RULE_GUARD_CANCEL_REQUESTED', ticker=ticker,
                                     order_id=order['order_id'], fill_confirmed=False)
                except Exception as error:
                    self.runner.notice('TWO_RULE_GUARD_CANCEL_PENDING', ticker + ':' + order['client_id'],
                                       error=repr(error), allowance_released=False)
