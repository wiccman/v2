"""Minute 8 through close; current top-two performers; delayed backup only.

Reuse the deployed entry, exit, cash, ownership and streak implementations.
One filled or potentially-filled market per period remains the limit. A second
candidate attempt requires a proven no-fill and a full 60-second cooldown.
No trading on import and no sleeps in the exit-monitoring loop.
"""
from __future__ import annotations

import math
import os
from contextlib import contextmanager

import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
import cash_checked_minute9 as cash

POLICY = "minute8-top2-delayed-backup-v1"
START_SECONDS = 480
END_SECONDS = 900
TOP_COUNT = 2
BACKUP_DELAY_SECONDS = 60
MAX_CANDIDATE_ATTEMPTS = 2


def configured_target_exchange_index():
    raw = os.getenv("M9_TARGET_EXCHANGE_INDEX", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("M9_TARGET_EXCHANGE_INDEX must be an integer from 0 through 100") from error
    if not 0 <= value <= 100:
        raise ValueError("M9_TARGET_EXCHANGE_INDEX must be an integer from 0 through 100")
    return value


def target_balance_ready(client, journal, base_budget, now):
    """Pause entries, not exits, until automatic rebalancing funds the target shard."""
    target = configured_target_exchange_index()
    if target is None:
        return True
    state = getattr(journal, "_entry_cash_state", None)
    if state is None:
        state = cash.CashState()
        journal._entry_cash_state = state
    try:
        total, shards = state.read(client, now)
    except Exception as error:
        core.event("M8_TARGET_BALANCE_UNAVAILABLE", target_exchange_index=target,
                   **ranked.error_fields(error))
        return False
    required = core.trade_budget(journal.data["streak"], base_budget)
    target_cash = shards.get(target) if shards is not None else None
    if target_cash is None or target_cash < required:
        core.event("M8_TARGET_BALANCE_WAIT", target_exchange_index=target,
                   total_cash_dollars=str(total),
                   target_cash_dollars=(str(target_cash) if target_cash is not None else None),
                   required_entry_budget_dollars=str(required))
        return False
    return True


def window_attempts(journal, start):
    """Include confirmed non-submissions, but never count a different period."""
    return [r for r in journal.data["windows"].values()
            if r.get("window_start_ms") == start * 1000
            and isinstance(r.get("buy"), dict)]


def backup_not_before(records):
    """Wait after the latest attempt AND its confirmation; restart-safe."""
    if not records:
        return None
    stamps = []
    for record in records:
        values = [record["buy"].get("created_at")]
        outcome = record.get("outcome")
        if outcome:
            values.append(outcome.get("completed_at"))
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("missing_or_invalid_attempt_timestamp")
            if not math.isfinite(value) or value < 0:
                raise ValueError("missing_or_invalid_attempt_timestamp")
            stamps.append(value)
    return max(stamps) + BACKUP_DELAY_SECONDS


def run_pass(client, journal, series_list, base_budget, max_contracts, live, now,
             max_entries_per_window=1, *, provider, clock=None):
    clock = clock or core.time.time
    if max_entries_per_window != 1:
        raise ValueError("This backup policy requires one filled/pending market per window")

    # Reconcile owned positions on EVERY poll, even during cooldown/feed failure.
    cash.run_cash_checked_pass(client, journal, (), base_budget, max_contracts,
                               live, now, max_entries_per_window)
    checked = clock()
    if not math.isfinite(checked):
        return
    start = int(checked // 900) * 900
    if not START_SECONDS <= checked - start < END_SECONDS:
        return
    if live and not target_balance_ready(client, journal, base_budget, checked):
        return
    if core.window_entry_count(journal, start * 1000) >= 1:
        return  # A fill, partial fill, or unknown acceptance blocks the backup.
    records = window_attempts(journal, start)
    if len(records) >= MAX_CANDIDATE_ATTEMPTS:
        return  # Ranking changes never authorize a third distinct coin attempt.
    try:
        deadline = backup_not_before(records)
    except ValueError:
        core.event("M8_BACKUP_BLOCKED", reason="invalid_attempt_timestamp",
                   window_start_ms=start * 1000)
        return
    if deadline is not None and checked < deadline:
        core.event("M8_BACKUP_WAIT", window_start_ms=start * 1000,
                   not_before=deadline, remaining_seconds=round(deadline - checked, 3))
        return

    names = tuple(series_list)
    try:
        rows = provider.snapshot(names, start)
        rows = ranked.ranked_observations(rows, names, start, clock(), allow_partial=True)
    except Exception as error:
        core.event("M8_PERFORMANCE_UNAVAILABLE", **ranked.error_fields(error))
        return
    if not START_SECONDS <= clock() - start < END_SECONDS:
        return
    # Slice BEFORE looking at contract prices. A priced-out top two never
    # promotes the third-ranked coin merely because its contract is affordable.
    selected = rows[:TOP_COUNT]
    core.event("M8_TOP2_QUEUE", policy=POLICY, window_start_ms=start * 1000,
               ranking_scope="available_coin_data",
               excluded_series=[s for s in names if s not in {r.series for r in rows}],
               queue=[{"rank": i + 1, "series": r.series,
                       "return_pct": str(r.return_pct)} for i, r in enumerate(selected)],
               skipped_lower_ranks=[r.series for r in rows[TOP_COUNT:]])
    proxy = ranked.QueueDiagnostics(client, clock=clock, emit=core.event)
    proxy.start = start
    proxy.ranks = {r.series: i + 1 for i, r in enumerate(selected)}
    if not live:
        # Preserve preview's one signal per period and no writes/no orders.
        return cash.run_cash_checked_pass(proxy, journal,
            tuple(r.series for r in selected), base_budget, max_contracts, False,
            clock(), max_entries_per_window)

    for row in selected:
        if not START_SECONDS <= clock() - start < END_SECONDS:
            return
        if core.window_entry_count(journal, start * 1000) >= 1:
            return
        before = {r["ticker"] for r in window_attempts(journal, start)}
        if len(before) >= MAX_CANDIDATE_ATTEMPTS:
            return
        # One series per call prevents the old core's immediate same-pass
        # zero-fill fallback. All original checks and durable order IDs remain.
        cash.run_cash_checked_pass(proxy, journal, (row.series,), base_budget,
                                   max_contracts, True, clock(), max_entries_per_window)
        after = {r["ticker"] for r in window_attempts(journal, start)}
        if after != before:
            core.event("M8_CANDIDATE_ATTEMPT_RECORDED", window_start_ms=start * 1000,
                       candidate_series=row.series, backup_delay_seconds=BACKUP_DELAY_SECONDS,
                       candidate_attempt_count=len(after))
            return  # Next candidate can only be considered on a later poll.


@contextmanager
def installed_policy(provider=None):
    """Explicit entrypoint only; importing leaves the legacy runner untouched."""
    previous = (core.ENTRY_START_SECONDS, core.ENTRY_END_SECONDS, core.run_pass)
    provider = provider or ranked.CoinbasePerformance()
    core.ENTRY_START_SECONDS, core.ENTRY_END_SECONDS = START_SECONDS, END_SECONDS

    def execute(*args, **kwargs):
        return run_pass(*args, **kwargs, provider=provider)

    core.run_pass = execute
    try:
        with cash.installed_guard():
            yield
    finally:
        core.ENTRY_START_SECONDS, core.ENTRY_END_SECONDS, core.run_pass = previous


def main(argv=None):
    with installed_policy():
        target = configured_target_exchange_index()
        core.event("M8_TOP2_POLICY_READY", policy=POLICY,
                   entry_start_seconds=START_SECONDS, entry_end_seconds=END_SECONDS,
                   performance_top_n=TOP_COUNT, backup_delay_seconds=BACKUP_DELAY_SECONDS,
                   max_candidate_attempts_per_window=MAX_CANDIDATE_ATTEMPTS,
                   max_filled_or_pending_markets=1,
                   automatic_rebalancing_enabled=bool(target),
                   target_exchange_index=target)
        return core.main(argv)


if __name__ == "__main__":
    main()
