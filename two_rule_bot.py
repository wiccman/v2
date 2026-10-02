"""Reviewable two-rule runner for wiccman/v2's existing KalshiClient.

No live actions occur on import, in --config mode, or in the test suite.
Use --live AND TRADING_ENABLED=true to enable execution after review.
The old bot's entry cycle is never called. Its exit monitor can finish legacy
positions during a cutover; a legacy-active market gets no new entries.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from two_rule_policy import (D, ZERO, FEE_RESERVE, BUILD, RULES, FINAL, BY_NAME,
                             candidate, direction, number, unique_fills, receipt,
                             target, config)

TERMINAL = {'executed', 'canceled', 'expired'}
PREFIX = '53523200-'  # UUID-format marker for recovery; the remaining UUID stays random.


class Pending(RuntimeError):
    """Exchange snapshots/acknowledgements have not converged. Do not resubmit."""


class AtomicStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.marker = self.path.with_suffix('.initialized')
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.data.get('schema') != 1 or not isinstance(self.data.get('markets'), dict):
                raise RuntimeError('Unknown/corrupt two-rule ledger; do not reset it')
        elif self.marker.exists():
            raise RuntimeError('Two-rule ledger missing; restore it rather than reset spending')
        else:
            self.data = {'schema': 1, 'build': BUILD, 'markets': {}}
            self.save()
        self.marker.touch(exist_ok=True)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(self.data, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Market close_time must include a timezone')
    return parsed.timestamp()


def default_emit(event: str, **details: Any) -> None:
    print(json.dumps({'event': event, 'time_utc': datetime.now(timezone.utc).isoformat(),
                      **details}, default=str), flush=True)


class TwoRuleBot:
    def __init__(self, client: Any, store: Any, *, exit_client: Any = None,
                 budget: Any = '20', clock: Callable[[], float] = time.time,
                 emit: Callable[..., None] = default_emit,
                 legacy_blocked: Callable[[str], bool] = lambda ticker: False,
                 deferred_errors: tuple[type[Exception], ...] = ()):
        self.client = client
        self.exits = exit_client if exit_client is not None else client
        self.store, self.clock, self.emit = store, clock, emit
        self.budget = number(budget, 'market budget')
        if self.budget <= 0:
            raise ValueError('Market budget must be positive')
        self.legacy_blocked = legacy_blocked
        self.deferred_errors = deferred_errors
        self.notice_cache: dict[str, Any] = {}
        self.last_heartbeat = float('-inf')

    def notice(self, event: str, ticker: str, **details: Any) -> None:
        key = event + ':' + ticker
        if self.notice_cache.get(key) != details:
            self.notice_cache[key] = dict(details)
            self.emit(event, ticker=ticker, **details)

    def _remote(self, ticker: str, order: dict[str, Any]) -> Mapping[str, Any]:
        if not order.get('order_id'):
            matches = [o for o in self.exits.all_orders(ticker)
                       if o.get('client_order_id') == order['client_id']]
            if len(matches) != 1:
                raise Pending('Unacknowledged order; no duplicate submission')
            order['order_id'] = matches[0]['order_id']
            self.store.save()
        remote = self.exits.order(order['order_id'], ticker)
        if remote.get('order_id') != order['order_id']:
            raise Pending('Order identity mismatch')
        if remote.get('ticker', ticker) != ticker:
            raise Pending('Order ticker mismatch')
        return remote

    def reconcile(self, ticker: str, record: dict[str, Any]) -> tuple[dict[str, Any], D, list[Any]]:
        # Resolve requests before reading fills. Never discard an unknown ACK.
        for trade in record['trades']:
            for order in trade['orders']:
                if order.get('terminal'):
                    continue
                remote = self._remote(ticker, order)
                filled = number(remote.get('fill_count_fp', remote.get('fill_count')), 'order fills')
                if not ZERO <= filled <= number(order['quantity'], 'saved quantity'):
                    raise Pending('Order fill count outside saved quantity')
                order['confirmed'] = str(filled)
                order['terminal'] = remote.get('status') in TERMINAL
                order['status'] = remote.get('status')
                self.store.save()
        fills = unique_fills(self.exits.all_fills(ticker), ticker)
        known = {o.get('order_id'): o for t in record['trades'] for o in t['orders'] if o.get('order_id')}
        if len(known) != sum(bool(o.get('order_id')) for t in record['trades'] for o in t['orders']):
            raise Pending('Order assigned to multiple trades')
        for oid, order in known.items():
            visible = sum((number(f.get('count_fp', f.get('count')), 'fill quantity')
                           for f in fills if f.get('order_id') == oid), ZERO)
            confirmed = number(order.get('confirmed', '0'), 'confirmed fills')
            if visible < confirmed or (order.get('terminal') and visible != confirmed):
                raise Pending('Fill history and order receipt disagree')
            if visible > number(order['quantity'], 'saved quantity'):
                raise Pending('Visible fills exceed saved order size')
        observations = {t['id']: receipt(t, fills) for t in record['trades']}
        own = sum((r.remaining * (1 if t['side'] == 'YES' else -1)
                   for t in record['trades'] for r in (observations[t['id']],)), ZERO)
        own_sides = {t['side'] for t in record['trades'] if observations[t['id']].remaining > 0}
        if len(own_sides) > 1:
            raise Pending('Opposite bot positions require manual reconciliation')
        rows = [p for p in self.exits.positions(ticker) if p.get('ticker') == ticker]
        if len(rows) > 1:
            raise Pending('Ambiguous position rows')
        account = number(rows[0].get('position_fp'), 'account position') if rows else ZERO
        if own and (own * account <= 0 or abs(account) < abs(own)):
            raise Pending('Manual reduction or inconsistent position; no unowned sale')
        # An untracked opposite fill after a bot entry can change lot ownership.
        # Freeze this market rather than sell a manual replacement by accident.
        entry_times = [number(f.get('ts'), 'fill timestamp') if f.get('ts') is not None
                       else D(str(timestamp(f['created_time'])))
                       for f in fills if f.get('order_id') in known
                       and known[f['order_id']]['role'] == 'entry']
        if own and entry_times:
            expected_opposite = 'ask' if own > 0 else 'bid'
            for f in fills:
                if f.get('order_id') in known or str(f.get('book_side', '')).lower() != expected_opposite:
                    continue
                stamp = number(f.get('ts'), 'fill timestamp') if f.get('ts') is not None else D(str(timestamp(f['created_time'])))
                if stamp >= min(entry_times):
                    raise Pending('Manual opposite fill changed ownership; market paused')
        return observations, account, fills

    def exposure(self, record: dict[str, Any], observations: dict[str, Any]) -> D:
        used = ZERO
        for trade in record['trades']:
            observed = observations[trade['id']]
            # Sale proceeds restore principal only; a loss remains charged.
            used += max(ZERO, observed.cost - observed.proceeds) + FEE_RESERVE * observed.remaining
            for order in trade['orders']:
                if order['role'] == 'entry' and not order.get('terminal'):
                    left = number(order['quantity'], 'quantity') - number(order.get('confirmed', '0'), 'confirmed')
                    used += left * (number(order['price'], 'price') + FEE_RESERVE)
        return used

    def _save_request(self, trade: dict[str, Any], role: str, quantity: D, price: D) -> dict[str, Any]:
        order = {'client_id': PREFIX + str(uuid.uuid4())[9:], 'role': role,
                 'quantity': str(quantity), 'price': str(price), 'confirmed': '0',
                 'terminal': False, 'requested_at': self.clock()}
        trade['orders'].append(order)
        self.store.save()  # Must succeed before any write to the exchange.
        return order

    def _save_ack(self, order: dict[str, Any], response: Mapping[str, Any]) -> None:
        if response.get('order_id'):
            order['order_id'] = response['order_id']
        order['receipt'] = dict(response)
        self.store.save()

    def cancel_ineligible(self, ticker: str, record: dict[str, Any]) -> bool:
        pending = [(t, o) for t in record['trades'] for o in t['orders']
                   if o['role'] == 'entry' and not o.get('terminal')]
        if not pending:
            return False
        try:
            market = self.client.market(ticker)
            spot = self.client.btc_reference_price()
        except Exception:
            # A resting order cannot retain authorization on a missing quote.
            market = spot = None
        left = record['close'] - self.clock()
        changed = False
        for trade, order in pending:
            rule = BY_NAME[trade['rule']]
            qualifies = (market is not None and 0 < left <= 900 and
                         (rule.last_seconds is None or left <= rule.last_seconds) and
                         direction(spot, market['floor_strike'], rule.distance) == trade['side'])
            if not qualifies and order.get('order_id'):
                self.client.cancel(order['order_id'], ticker)
                changed = True
                self.emit('TWO_RULE_CANCEL_REQUESTED', ticker=ticker, rule=rule.name,
                          order_id=order['order_id'])
        return changed

    def exits_for_market(self, ticker: str, record: dict[str, Any], observations: dict[str, Any]) -> bool:
        did_submit = False
        for trade in record['trades']:
            observed = observations[trade['id']]
            if observed.remaining <= 0:
                continue
            if any(not o.get('terminal') and o['role'] == 'exit' for o in trade['orders']):
                continue
            if self.clock() < trade.get('exit_retry_at', 0):
                continue
            market = self.exits.market(ticker)
            wanted = target(trade, observed, market.get('price_ranges'))
            if wanted is None:
                self.notice('TWO_RULE_PROFIT_UNREACHABLE', ticker + ':' + trade['id'],
                            rule=trade['rule'], profit_goal=trade['profit'],
                            remaining=str(observed.remaining), entry_cost=str(observed.cost))
                continue  # No invented 98c/99c fallback and no new entry filter.
            self.notice('TWO_RULE_PROFIT_TARGET', ticker + ':' + trade['id'],
                        rule=trade['rule'], remaining=str(observed.remaining),
                        sell_limit=str(wanted), gross_profit_goal=trade['profit'])
            bid = number(market[trade['side'].lower() + '_bid_dollars'], 'bid')
            if not ZERO <= bid <= 1:
                raise ValueError('Invalid executable bid')
            if bid < wanted:
                continue
            # Stop any unfilled remainder before selling to avoid late refills.
            buys = [o for o in trade['orders'] if o['role'] == 'entry' and not o.get('terminal')]
            if buys:
                for o in buys:
                    if o.get('order_id'):
                        self.client.cancel(o['order_id'], ticker)
                continue  # Confirm cancellation/fills on the next pass.
            if self.clock() >= record['close']:
                continue
            signed = observed.remaining * (1 if trade['side'] == 'YES' else -1)
            order = self._save_request(trade, 'exit', observed.remaining, wanted)
            trade['exit_retry_at'] = self.clock() + 3
            self.store.save()
            try:
                response = self.exits.place_take_profit(
                    ticker, signed, wanted, record['close'], client_order_id=order['client_id'])
            except self.deferred_errors:
                order.update(terminal=True, confirmed='0', unsubmitted=True)
                self.store.save()
                self.emit('TWO_RULE_LOCAL_DEFERRAL', ticker=ticker, role='exit')
                return False
            self._save_ack(order, response)
            self.emit('TWO_RULE_SELL_SUBMITTED', ticker=ticker, rule=trade['rule'],
                      quantity=str(observed.remaining), limit=str(wanted),
                      profit_goal=trade['profit'], fill_confirmed=False)
            # Reconcile this sale before touching another trade's holdings.
            return True
        return did_submit

    def attempt(self, ticker: str, record: dict[str, Any], rule: Any,
                observations: dict[str, Any], account: D) -> bool:
        for trade in record['trades']:
            if trade['rule'] != rule.name:
                continue
            observed = observations[trade['id']]
            if any(not o.get('terminal') for o in trade['orders']) or observed.remaining > 0:
                return False
            if rule == FINAL and observed.entered > 0:
                return False  # Preserve existing one filled final entry per market.
        market = self.client.market(ticker)
        spot = self.client.btc_reference_price()
        selected = candidate(rule, market, spot, record['close'] - self.clock())
        if selected is None:
            return False
        cash = number(self.client.market_cash(ticker)['cash_dollars'], 'market cash')
        # Re-read quotes and BTC after potentially slow funding reads.
        market = self.client.market(ticker)
        spot = self.client.btc_reference_price()
        selected = candidate(rule, market, spot, record['close'] - self.clock())
        if selected is None:
            return False
        rows = [p for p in self.exits.positions(ticker) if p.get('ticker') == ticker]
        latest = number(rows[0]['position_fp'], 'position') if len(rows) == 1 else ZERO
        if len(rows) > 1 or latest != account:
            raise Pending('Position changed before entry')
        if account and ((account > 0) != (selected.side == 'YES')):
            return False  # Do not net/close an opposing manual or bot position.
        if any(t['side'] != selected.side and any(not o.get('terminal') for o in t['orders'])
               for t in record['trades']):
            return False
        required = D(selected.contracts) * (selected.price + FEE_RESERVE)
        if required > cash or required + self.exposure(record, observations) > self.budget:
            self.notice('TWO_RULE_BUDGET_WAIT', ticker + ':' + rule.name,
                        required=str(required), market_budget=str(self.budget))
            return False
        # Check the actual clock immediately before reserving and submitting.
        if candidate(rule, market, spot, record['close'] - self.clock()) is None:
            return False
        trade = {'id': uuid.uuid4().hex, 'rule': rule.name, 'side': selected.side,
                 'profit': str(selected.profit), 'orders': []}
        record['trades'].append(trade)
        order = self._save_request(trade, 'entry', D(selected.contracts), selected.price)
        order['signal'] = {'btc_reference': str(spot), 'strike': str(market['floor_strike']),
                           'distance_dollars': str(number(spot, 'spot') - number(market['floor_strike'], 'strike')),
                           'seconds_remaining': record['close'] - self.clock()}
        self.store.save()
        try:
            response = self.client.place_entry(
                ticker, selected.side, D(selected.contracts), selected.price, record['close'],
                submit_before=record['close'], client_order_id=order['client_id'],
                ioc=(rule != FINAL))
        except self.deferred_errors:
            order.update(terminal=True, confirmed='0', unsubmitted=True)
            self.store.save()
            self.emit('TWO_RULE_LOCAL_DEFERRAL', ticker=ticker, role='entry')
            return False
        self._save_ack(order, response)
        self.emit('TWO_RULE_BUY_SUBMITTED', ticker=ticker, rule=rule.name,
                  side=selected.side, quantity=selected.contracts, limit=str(selected.price),
                  seconds_remaining=record['close'] - self.clock(),
                  profit_goal=str(selected.profit), signal=order['signal'], fill_confirmed=False)
        return True

    def cycle(self) -> None:
        if self.clock() - self.last_heartbeat >= 60:
            self.emit('TWO_RULE_HEARTBEAT', build=BUILD, rules=[r.name for r in RULES])
            self.last_heartbeat = self.clock()
        markets = self.store.data['markets']
        snapshots = {}
        paused = set()
        # Exits/reconciliation always precede discovery and fresh buy decisions.
        for ticker, record in list(markets.items()):
            if self.clock() >= record['close']:
                self.notice('TWO_RULE_MARKET_CLOSED', ticker, close=record['close'],
                            note='No settlement/P&L assertion; exchange determines settlement')
                continue
            try:
                observed, account, _ = self.reconcile(ticker, record)
                if self.cancel_ineligible(ticker, record):
                    paused.add(ticker)
                    continue
                if self.exits_for_market(ticker, record, observed):
                    paused.add(ticker)
                    continue
                snapshots[ticker] = (observed, account)
            except Exception as error:
                paused.add(ticker)
                self.notice('TWO_RULE_RECONCILE_WAIT', ticker, error=repr(error))
        for market in self.client.markets(series_ticker='KXBTC15M', status='open', limit=100):
            ticker, close = market['ticker'], timestamp(market['close_time'])
            if not close - 900 <= self.clock() < close or ticker in paused:
                continue
            if self.legacy_blocked(ticker):
                self.notice('TWO_RULE_LEGACY_CUTOVER_WAIT', ticker,
                            note='Legacy exit monitor retains this market until close')
                continue
            if ticker not in markets:
                # Recovery safeguard: no recreated ledger may ignore our orders.
                if any(str(o.get('client_order_id', '')).startswith(PREFIX)
                       for o in self.client.all_orders(ticker)):
                    raise Pending('Existing two-rule orders without ledger; restore state')
                markets[ticker] = {'close': close, 'trades': []}
                self.store.save()
            record = markets[ticker]
            if record['close'] != close:
                raise Pending('Market close_time changed')
            observed, account = snapshots.get(ticker, (None, None))
            if observed is None:
                observed, account, _ = self.reconcile(ticker, record)
            for rule in RULES:  # Final rule wins a simultaneous funding conflict.
                if self.attempt(ticker, record, rule, observed, account):
                    break  # Reconcile this request before another risk decision.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--config', action='store_true')
    args = parser.parse_args()
    if args.config or not args.live:
        print(json.dumps(config(), indent=2))
        return
    # Import the existing execution adapter only after explicit live selection.
    from dotenv import load_dotenv
    load_dotenv()
    if os.getenv('TRADING_ENABLED', 'false').lower() != 'true':
        default_emit('TWO_RULE_CONFIG', **config(), trading_enabled=False,
                     commit=os.getenv('RAILWAY_GIT_COMMIT_SHA', 'unavailable'))
        default_emit('TWO_RULE_LOCKED', message='Live order routing is disabled')
        while True:
            time.sleep(3600)
    import fcntl
    import bot as legacy
    from take_profit import TakeProfitMonitor
    from kalshi import KalshiClient
    from request_coordinator import RequestCoordinator, RequestDeferred
    legacy.validate_storage()
    state_path = legacy.STATE.with_name('two_rule_state.json')
    coordinator = RequestCoordinator()
    def connection(role: str) -> Any:
        return KalshiClient(os.getenv('KALSHI_API_KEY_ID', ''),
                            os.getenv('KALSHI_PRIVATE_KEY_PATH', ''),
                            os.getenv('KALSHI_PRIVATE_KEY_B64', ''), timeout=5,
                            coordinator=coordinator, role=role)
    # Same lock as bot.py, so the old and new entry loops cannot coexist.
    with legacy.STATE.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another bot owns this persistent state volume')
        legacy_snapshot = legacy.load_state()
        blocked = {ticker: rec.get('close_timestamp')
                   for ticker, rec in legacy_snapshot['markets'].items()
                   if rec.get('entry_intents') or rec.get('orders') or rec.get('buys')}
        if any(close is None for close in blocked.values()):
            raise SystemExit('Legacy close timestamp missing; reconcile old state first')
        old_monitor = None
        if any(close > time.time() for close in blocked.values()):
            old_monitor = TakeProfitMonitor(
                connection('exit'), lambda: legacy_snapshot,
                legacy.STATE.with_name(legacy.STATE.stem + '_take_profit.json'),
                pairs=legacy.ALL_ENTRY_EXIT_PAIRS, poll=1, fill_cost_targets=True,
                per_order_profit=legacy.PER_ORDER_PROFIT_DOLLARS,
                force_exit_price=D('.98'), no_fill_pause=3, quote_gate=True)
            old_monitor.start()  # Legacy exits only, never legacy entries.
        store = AtomicStore(state_path)
        runner = TwoRuleBot(connection('entry'), store, exit_client=connection('exit'),
                            budget=os.getenv('MARKET_BUDGET_DOLLARS', '20'),
                            legacy_blocked=lambda ticker: blocked.get(ticker, 0) > time.time(),
                            deferred_errors=(RequestDeferred,))
        default_emit('TWO_RULE_CONFIG', **config(), market_budget=str(runner.budget),
                     commit=os.getenv('RAILWAY_GIT_COMMIT_SHA', 'unavailable'))
        def stop(signum: int, frame: Any) -> None:
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            while True:
                try:
                    runner.cycle()
                except Exception as error:
                    runner.notice('TWO_RULE_LOOP_ERROR', 'service', error=repr(error))
                time.sleep(max(1, float(os.getenv('POLL_SECONDS', '5'))))
        except KeyboardInterrupt:
            store.save()
        finally:
            if old_monitor is not None:
                old_monitor.stop()


if __name__ == '__main__':
    main()
