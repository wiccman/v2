"""Production entrypoint: one final-two-minute rule, using the durable engine.

The original bot.py remains available as the archived engine/reference. This
entrypoint does not call its legacy strategy cycle, main, or side-switch route.
"""
import argparse
import json
import os
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal as D

import bot as engine
from entry_policy import SETTLEMENT_KIND, initialize, attempt_committed
from price_pairs import InventorySyncError
from take_profit import TakeProfitMonitor, entry_fingerprint
from single_rule_policy import (
    MIN_DISTANCE, ENTRY_PRICE, WINDOW_SECONDS, MAX_CONTRACTS,
    PROFIT_DOLLARS, entry_side, profit_target,
)


def configure_engine():
    """Configure shared execution guards, without enabling retired strategies."""
    if engine.SETTLEMENT_WINDOW != WINDOW_SECONDS or engine.SETTLEMENT_PRICE != ENTRY_PRICE:
        raise RuntimeError('Execution engine does not match the single-rule window/price')
    engine.MIN_STRIKE_DISTANCE_DOLLARS = MIN_DISTANCE
    engine.MAX_OPEN_CONTRACTS = MAX_CONTRACTS
    engine.DIRECTIONAL_ENTRY_POLICY = True


class FillSnapshotClient:
    """Reuse the exact fill snapshot already verified by the durable monitor."""
    def __init__(self, client):
        self.wrapped = client
        self.fill_snapshot = None

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def all_fills(self, ticker):
        self.fill_snapshot = self.wrapped.all_fills(ticker)
        return self.fill_snapshot


class SingleRuleExitMonitor(TakeProfitMonitor):
    """One gross-dollar exit goal; no 98c/99c override or loss-close strategy."""
    def __init__(self, client, read_entries, path, **kwargs):
        for name in ('per_order_profit', 'per_order_increment', 'per_order_percentage',
                     'force_exit_price', 'fill_cost_targets'):
            kwargs.pop(name, None)
        super().__init__(FillSnapshotClient(client), read_entries, path,
                         force_exit_price=None, per_order_profit=None,
                         fill_cost_targets=False, **kwargs)

    def _market(self, ticker, record, ledger):
        # Preserve reconciliation and manual ownership. Retired side-switch
        # instructions must not authorize a second entry or a loss-taking exit.
        return super()._market(ticker, {**record, 'settlement_switch': {}}, ledger)

    def _paired_buckets(self, ticker, record, ledger, held):
        self.client.fill_snapshot = None
        super()._paired_buckets(ticker, record, ledger, held)
        ownership = ledger['ownership']
        quantity = D(ownership['bot_held'])
        if not quantity:
            return {}, {}
        record = self.read_entries().get('markets', {}).get(ticker, record)
        if entry_fingerprint(record) != ownership['entry_fingerprint']:
            raise InventorySyncError('Entry state changed after fill attribution')
        fills = self.client.fill_snapshot
        if fills is None:
            raise InventorySyncError('Verified fill snapshot unavailable')
        recovered = ledger.get('entry_order_ids', {})
        entries = {}
        for item in record.get('entry_intents', []):
            oid = item.get('order_id') or recovered.get(item.get('client_id'))
            if oid:
                entries[oid] = item['side']
        exits = {oid: item['side'] for oid, item in ledger.get('exit_orders', {}).items()}
        side = 'YES' if quantity > 0 else 'NO'
        market = self.client.market(ticker)
        target, details = profit_target(fills, entries, exits, abs(quantity), side,
                                        market.get('price_ranges'))
        details.update(side=side, target=str(target) if target is not None else None)
        event = 'TP_SINGLE_RULE_TARGET' if target is not None else 'TP_PROFIT_UNREACHABLE'
        if ledger.get('single_rule_target') != details:
            ledger['single_rule_target'] = details
            self.save()
            self.emit(event, ticker=ticker, **details)
        if target is None:
            # Partial fills/coarse grids may make $0.40 impossible before close.
            # Never silently lower the requested target or claim a guaranteed exit.
            return {D(1): quantity}, {}
        plan = {'allocations': ownership['allocations'], 'cost_groups': [details]}
        return {target: quantity}, {target: plan}


def entry(record, state, ticker, closed):
    remaining = closed.timestamp() - time.time()
    if not 0 < remaining <= WINDOW_SECONDS:
        return
    if any(i.get('kind') == SETTLEMENT_KIND and attempt_committed(i)
           for i in record.get('entry_intents', [])):
        return
    market = engine.client.market(ticker)
    spot = engine.client.btc_reference_price()
    remaining = closed.timestamp() - time.time()
    side = entry_side(spot, market['floor_strike'], remaining,
                      market['yes_ask_dollars'], market['no_ask_dollars'])
    if side is None:
        engine.write_log('SINGLE_RULE_SKIP', ticker, details=json.dumps({
            'seconds_remaining': round(remaining, 3), 'btc_reference': str(spot),
            'strike': str(market['floor_strike']),
            'yes_ask': str(market['yes_ask_dollars']),
            'no_ask': str(market['no_ask_dollars']),
            'required_distance_dollars': str(MIN_DISTANCE),
            'required_ask': str(ENTRY_PRICE),
        }))
        return
    held = engine.settlement_position(ticker)
    if (side == 'YES' and held < 0) or (side == 'NO' and held > 0):
        engine.write_log('SINGLE_RULE_OPPOSITE_INVENTORY_WAIT', ticker)
        return
    result, quantity = engine.funded_entry(
        record, state, ticker, side, ENTRY_PRICE, closed, SETTLEMENT_KIND,
        submit_before=closed.timestamp(), cancel_at=closed.timestamp(),
    )
    if result.get('order_id'):
        engine.write_log('SINGLE_RULE_BUY', ticker, prediction=side,
                         price=str(ENTRY_PRICE), quantity=str(quantity),
                         details=json.dumps({'order_id': result['order_id'],
                             'seconds_remaining': round(closed.timestamp() - time.time(), 3),
                             'gross_profit_goal': str(PROFIT_DOLLARS)}))


def cycle(state):
    engine.reconcile_entries(state)
    if engine.EXIT_MONITOR is None:
        return
    market, started, closed = engine.active_market(datetime.now(timezone.utc))
    if not market:
        return
    ticker = market['ticker']
    if ticker in state.get('mm', {}).get('markets', {}):
        return
    record = state['markets'].setdefault(ticker, {
        'buys': 0, 'last_buy': 0, 'orders': [], 'signal': None, 'predictions': [],
    })
    initialize(record)
    record['entry_cancel_at'] = closed.timestamp() - WINDOW_SECONDS
    record['close_timestamp'] = closed.timestamp()
    engine.save_state(state)
    engine.reconcile_entries(state)
    if not engine.EXIT_MONITOR.healthy:
        engine.write_log('ENTRY_WAIT_TAKE_PROFIT', ticker, details='Exit monitor recovering')
        return
    remaining = closed.timestamp() - time.time()
    if not 0 < remaining <= WINDOW_SECONDS:
        engine.write_log('ENTRY_FINAL_TWO_MINUTES_WAIT', ticker,
                         details=json.dumps({'seconds_remaining': round(remaining, 3),
                                             'window_seconds': WINDOW_SECONDS}))
        return
    entry(record, state, ticker, closed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    configure_engine()
    engine.write_log('SINGLE_RULE_CONFIG', details=json.dumps({
        'rule_count': 1, 'window_seconds': WINDOW_SECONDS,
        'minimum_strike_distance_dollars': str(MIN_DISTANCE),
        'ask_trigger': str(ENTRY_PRICE), 'buy_limit': str(ENTRY_PRICE),
        'maximum_open_contracts': str(MAX_CONTRACTS),
        'gross_profit_goal_dollars': str(PROFIT_DOLLARS),
        'market_budget_dollars': str(engine.MARKET_BUDGET),
        'forced_price_exit': None, 'other_buy_routes': 'disabled',
    }))
    if args.check:
        engine.check()
        return
    if not engine.ENABLED:
        print('LOCKED: live order routing disabled', flush=True)
        while True:
            time.sleep(3600)
    import fcntl
    engine.validate_storage()
    with engine.STATE.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another bot already owns this state volume')
        state = engine.load_state()
        client = engine.KalshiClient(os.getenv('KALSHI_API_KEY_ID', ''),
            os.getenv('KALSHI_PRIVATE_KEY_PATH', ''), os.getenv('KALSHI_PRIVATE_KEY_B64', ''),
            timeout=5, coordinator=engine.REQUEST_COORDINATOR, role='exit')
        engine.EXIT_MONITOR = SingleRuleExitMonitor(
            client, engine.load_state,
            engine.STATE.with_name(engine.STATE.stem + '_take_profit.json'),
            pairs=engine.ALL_ENTRY_EXIT_PAIRS, poll=float(os.getenv('EXIT_POLL_SECONDS', '1')),
            no_fill_pause=3.0, quote_gate=True,
        )
        engine.EXIT_MONITOR.start()
        def stop(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            while True:
                try:
                    cycle(state)
                except Exception as error:
                    engine.write_log('ERROR', details=repr(error))
                time.sleep(float(os.getenv('POLL_SECONDS', '5')))
        except KeyboardInterrupt:
            engine.save_state(state)
        finally:
            engine.EXIT_MONITOR.stop()


if __name__ == '__main__':
    main()
