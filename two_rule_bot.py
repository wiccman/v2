"""Rule-A-only runner using the existing KalshiClient and persistent ledger.

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

from two_rule_policy import (D, ZERO, FEE_RESERVE, BUILD, RULES, FINAL,
                             candidate, evaluate_final, number, unique_fills,
                             receipt, target, config)

from manual_trade_guard import ManualTradeGuard

TERMINAL = {'executed', 'canceled', 'expired'}
PREFIX = '53523200-'  # UUID-format marker for recovery; the remaining UUID stays random.
PARTITION_BUDGET = D('10')
PARTITION_VERSION = 'rule-a-only-preserve-10-v1'
# Historical identifiers are accounting aliases, never additional entry rules.
FINAL_NAMES = frozenset({FINAL.name, 'final_2m_50'})
KNOWN_SAVED_RULES = FINAL_NAMES | {'directional_100'}


def budget_config(market_budget: Any = '20') -> dict[str, Any]:
    return {'market_budget': str(number(market_budget, 'market budget')),
            'budget_partition_version': PARTITION_VERSION,
            'rule_budgets': {r.name: str(PARTITION_BUDGET) for r in RULES},
            'cross_rule_borrowing': False,
            'unused_budget_dollars': str(max(ZERO, number(market_budget, 'market budget') - PARTITION_BUDGET)),
            'entry_fee_reserve_per_contract': str(FEE_RESERVE)}


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
    """Name retained for import compatibility; only Rule A creates entries."""
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
        self.manual_guard = ManualTradeGuard(self, Pending)

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
        self.manual_guard.require_active(record)
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
        self.manual_guard.inspect(ticker, record, fills, known)
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
            self.manual_guard.pause(ticker, record, 'ownership_or_position_mismatch',
                                    bot_quantity=str(own), account_quantity=str(account))
        self._audit_fills(ticker, record, observations)
        return observations, account, fills

    def _audit_fills(self, ticker: str, record: dict[str, Any],
                     observations: dict[str, Any]) -> None:
        """Log fill-backed changes only after receipt/position checks succeed."""
        for trade in record['trades']:
            observed = observations[trade['id']]
            if observed.entered == 0 and observed.sold == 0:
                continue
            snapshot = {
                'entry_quantity': str(observed.entered),
                'exit_quantity': str(observed.sold),
                'remaining_quantity': str(observed.remaining),
                'verified_entry_cost': str(observed.cost),
                'verified_sale_proceeds': str(observed.proceeds),
                # Open inventory value is not realized profit. Partial exits
                # retain their original trade-level goal in target().
                'gross_profit_when_flat': (str(observed.proceeds - observed.cost)
                                           if observed.remaining == 0 else None),
                'fees_included': False,
            }
            if trade.get('verified_fill_snapshot') == snapshot:
                continue
            trade['verified_fill_snapshot'] = snapshot
            self.store.save()
            self.emit('TWO_RULE_FILL_VERIFIED', ticker=ticker, trade_id=trade['id'],
                      rule=trade['rule'], side=trade['side'], fill_confirmed=True,
                      **snapshot)

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

    def funding_allowed(self, ticker: str, record: dict[str, Any],
                        observations: dict[str, Any], selected: Any, cash: Any) -> bool:
        """Apply Rule A's existing $10 allocation and the overall market cap.

        Reuse fill-backed accounting for pending reservations, partial fills and
        released principal. Sales only replenish their own rule; losses remain
        charged. Existing over-cap positions can still exit and are not reset.
        Sizes and profit goals are not silently changed to make a trade fit.
        """
        if selected.rule != FINAL.name or any(t.get('rule') not in KNOWN_SAVED_RULES for t in record['trades']):
            raise Pending('Unknown strategy identity; cannot allocate its budget')
        cash = number(cash, 'market cash')
        if cash < 0:
            raise ValueError('Market cash must be nonnegative')
        required = D(selected.contracts) * (selected.price + FEE_RESERVE)
        market_used = self.exposure(record, observations)
        partition = {'trades': [t for t in record['trades'] if t['rule'] in FINAL_NAMES]}
        rule_used = self.exposure(partition, observations)
        reason = ('requested_size_exceeds_partition' if required > PARTITION_BUDGET else
                  'rule_partition_exhausted' if required + rule_used > PARTITION_BUDGET else
                  'shared_market_budget_exhausted' if required + market_used > self.budget else
                  'insufficient_market_cash' if required > cash else None)
        if reason is None:
            return True
        self.notice('TWO_RULE_BUDGET_SIZE_CONFLICT' if required > PARTITION_BUDGET
                    else 'TWO_RULE_BUDGET_WAIT', ticker + ':' + selected.rule,
                    rule=selected.rule, reason=reason, requested_quantity=selected.contracts,
                    selected_ask=str(selected.price), required=str(required),
                    rule_budget=str(PARTITION_BUDGET), rule_used=str(rule_used),
                    market_budget=str(self.budget), market_used=str(market_used),
                    cash_available=str(cash), fee_reserve_per_contract=str(FEE_RESERVE),
                    cross_rule_borrowing=False)
        return False

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
        """Retire ineligible bot entries; never consult BTC or the strike.

        Old strategy requests remain reserved until exchange reconciliation.
        Their confirmed fills retain normal saved-target exit management.
        """
        pending = [(t, o) for t in record['trades'] for o in t['orders']
                   if o['role'] == 'entry' and not o.get('terminal')]
        if not pending:
            return False
        selected = None
        reason = 'outside_final_120_seconds'
        if 0 < record['close'] - self.clock() <= 120:
            try:
                market = self.client.market(ticker)
                selected, reason = evaluate_final(market, record['close'] - self.clock())
            except Exception:
                reason = 'quote_unavailable_or_invalid'
        changed = False
        for trade, order in pending:
            qualifies = (trade['rule'] in FINAL_NAMES and selected is not None
                         and selected.side == trade['side']
                         and number(order['price'], 'entry limit') == FINAL.exact_ask
                         and number(order['quantity'], 'entry quantity') <= FINAL.contracts)
            if qualifies:
                continue
            # Resolve unknown acknowledgements and independently verify ownership.
            remote = self._remote(ticker, order)
            if ((remote.get('ticker') or remote.get('market_ticker')) != ticker
                    or remote.get('client_order_id') != order['client_id']):
                raise Pending('Cannot verify cancellation ownership')
            if remote.get('status') in TERMINAL:
                continue  # Next reconcile pass confirms counts and releases reserves.
            self.client.cancel(order['order_id'], ticker)
            changed = True
            self.emit('TWO_RULE_CANCEL_REQUESTED', ticker=ticker, rule=trade['rule'],
                      order_id=order['order_id'], fill_confirmed=False,
                      reason=('retired_entry_rule' if trade['rule'] not in FINAL_NAMES
                              else reason if selected is None else 'entry_no_longer_matches'))
        return changed

    def exits_for_market(self, ticker: str, record: dict[str, Any], observations: dict[str, Any]) -> bool:
        self.manual_guard.require_active(record)
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
            # Manual fills can occur during a quote read, including close/rebuy
            # that leaves the same net size. Reconcile again before authorizing
            # a sell; never resize into untracked/manual replacement inventory.
            latest = self.manual_guard.before_submit(ticker, record, observations)
            signed = observed.remaining * (1 if trade['side'] == 'YES' else -1)
            if signed * latest <= 0 or abs(signed) > abs(latest):
                self.manual_guard.pause(ticker, record, 'exit_exceeds_verified_position')
            if self.clock() >= record['close']:
                continue
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

    def entry_candidate(self, ticker: str, record: dict[str, Any],
                        market: Mapping[str, Any], stage: str) -> Any:
        left = record['close'] - self.clock()
        selected, reason = evaluate_final(market, left)
        self.notice('TWO_RULE_CANDIDATE_DECISION', ticker + ':' + stage,
                    rule=FINAL.name, stage=stage, eligible=(selected is not None),
                    reason=reason, seconds_remaining=left,
                    yes_ask=market.get('yes_ask_dollars'),
                    no_ask=market.get('no_ask_dollars'),
                    side=selected.side if selected is not None else None,
                    selection_basis='exact_96c_ask_only')
        return selected

    def attempt(self, ticker: str, record: dict[str, Any], rule: Any,
                observations: dict[str, Any], account: D) -> bool:
        if rule != FINAL:
            return False
        self.manual_guard.require_active(record)
        if not 0 < record['close'] - self.clock() <= 120:
            self.notice('TWO_RULE_ENTRY_WAIT', ticker,
                        reason='outside_final_120_seconds', rule=FINAL.name)
            return False
        for trade in record['trades']:
            if trade['rule'] not in FINAL_NAMES:
                continue
            observed = observations[trade['id']]
            if any(not o.get('terminal') for o in trade['orders']) or observed.remaining > 0:
                self.notice('TWO_RULE_ENTRY_WAIT', ticker,
                            reason='existing_entry_or_position', rule=FINAL.name)
                return False
            if observed.entered > 0:
                self.notice('TWO_RULE_ENTRY_WAIT', ticker,
                            reason='one_filled_entry_per_market', rule=FINAL.name)
                return False
        market = self.client.market(ticker)
        selected = self.entry_candidate(ticker, record, market, 'initial')
        if selected is None:
            return False
        cash = number(self.client.market_cash(ticker)['cash_dollars'], 'market cash')
        try:
            self.manual_guard.before_submit(ticker, record, observations, account)
        except Exception:
            self.manual_guard.cancel_bot_entries(ticker, record)
            raise
        # Refresh the quote after slow cash/ownership reads. No BTC request.
        market = self.client.market(ticker)
        selected = self.entry_candidate(ticker, record, market, 'confirm')
        if selected is None:
            return False
        if account and ((account > 0) != (selected.side == 'YES')):
            self.notice('TWO_RULE_ENTRY_WAIT', ticker,
                        reason='opposing_account_position', rule=FINAL.name)
            return False
        if any(t['side'] != selected.side and any(not o.get('terminal') for o in t['orders'])
               for t in record['trades']):
            self.notice('TWO_RULE_ENTRY_WAIT', ticker,
                        reason='opposing_unresolved_order', rule=FINAL.name)
            return False
        if not self.funding_allowed(ticker, record, observations, selected, cash):
            return False
        if candidate(FINAL, market, None, record['close'] - self.clock()) is None:
            return False
        trade = {'id': uuid.uuid4().hex, 'rule': FINAL.name, 'side': selected.side,
                 'profit': str(FINAL.profit), 'orders': []}
        record['trades'].append(trade)
        order = self._save_request(trade, 'entry', D(FINAL.contracts), selected.price)
        order['signal'] = {'selection_basis': 'exact_96c_ask_only',
                           'yes_ask': market.get('yes_ask_dollars'),
                           'no_ask': market.get('no_ask_dollars'),
                           'seconds_remaining': record['close'] - self.clock()}
        self.store.save()
        try:
            response = self.client.place_entry(
                ticker, selected.side, D(FINAL.contracts), selected.price, record['close'],
                submit_before=record['close'], client_order_id=order['client_id'], ioc=False)
        except self.deferred_errors:
            order.update(terminal=True, confirmed='0', unsubmitted=True)
            self.store.save()
            self.emit('TWO_RULE_LOCAL_DEFERRAL', ticker=ticker, role='entry')
            return False
        self._save_ack(order, response)
        self.emit('TWO_RULE_BUY_SUBMITTED', ticker=ticker, rule=FINAL.name,
                  side=selected.side, quantity=FINAL.contracts, limit=str(selected.price),
                  seconds_remaining=record['close'] - self.clock(),
                  profit_goal=str(FINAL.profit), signal=order['signal'], fill_confirmed=False)
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
                self.manual_guard.cancel_bot_entries(ticker, record)
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
            self.attempt(ticker, record, FINAL, observed, account)



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--config', action='store_true')
    args = parser.parse_args()
    if args.config or not args.live:
        print(json.dumps({**config(), **budget_config()}, indent=2))
        return
    # Import the existing execution adapter only after explicit live selection.
    from dotenv import load_dotenv
    load_dotenv()
    if os.getenv('TRADING_ENABLED', 'false').lower() != 'true':
        default_emit('TWO_RULE_CONFIG', **config(),
                     **budget_config(os.getenv('MARKET_BUDGET_DOLLARS', '20')),
                     trading_enabled=False,
                     manual_guard=ManualTradeGuard.VERSION,
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
        default_emit('TWO_RULE_CONFIG', **config(), **budget_config(runner.budget),
                     manual_guard=ManualTradeGuard.VERSION,
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
