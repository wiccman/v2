"""Minute 9 through close; top two overall performers only; BTC excluded.

Reuse the deployed entry, exit, cash, ownership and streak implementations.
One filled or potentially-filled market per period remains the limit. A second
candidate attempt requires a proven no-fill and a full 60-second cooldown.
No trading on import, no transfers, no sleeps in the exit-monitoring loop.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
import cash_checked_minute9 as cash
import late_basket_minute9 as late
import leader_mean15 as leader
import os
from pathlib import Path

POLICY = "minute9-top2-sol-central-overnight-v6"
START_SECONDS = 540
END_SECONDS = 900
TOP_COUNT = 2
BTC_SERIES = "KXBTC15M"
SOL_SERIES = "KXSOL15M"
CENTRAL_TIME = ZoneInfo("America/Chicago")
SOL_BLACKOUT_START_HOUR = 1
SOL_BLACKOUT_END_HOUR = 4
BACKUP_DELAY_SECONDS = 60
MAX_CANDIDATE_ATTEMPTS = 2


def sol_entry_blackout(now):
    """Do not submit SOL entries from 01:00 inclusive until 04:00 Central daily.

    America/Chicago handles CST/CDT and the fall DST repeated hour.
    Other coins' entries and ALL existing-position exits remain available.
    """
    if not math.isfinite(now):
        raise ValueError("Invalid entry clock")
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(CENTRAL_TIME)
    return SOL_BLACKOUT_START_HOUR <= local.hour < SOL_BLACKOUT_END_HOUR


class DirectionCheckedQueue(ranked.QueueDiagnostics):
    """Veto opposing entry signals; never reinterpret a venue strike or flip sides."""
    def entry_direction_check(self, ticker, side, window_start_ms):
        series = ticker.split("-")[0]
        row = self.observations.get(series)
        details = {"source": "coinbase_window_open", "side": side}
        if series == SOL_SERIES and sol_entry_blackout(self.clock()):
            return {"source": "central_time_entry_window", "side": side,
                    "allowed": False, "reason": "entry_time_restricted",
                    "timezone": "America/Chicago",
                    "blocked_start_hour": SOL_BLACKOUT_START_HOUR,
                    "blocked_end_hour": SOL_BLACKOUT_END_HOUR}
        try:
            if (row is None or side not in {"YES", "NO"}
                    or window_start_ms != self.start * 1000):
                raise ranked.PerformanceUnavailable("entry_observation_missing")
            ranked.ranked_observations([row], (series,), self.start, self.clock())
        except ranked.PerformanceUnavailable:
            return dict(details, allowed=False, reason="entry_direction_unavailable")
        move = row.return_pct
        conflict = (side == "NO" and move > 0) or (side == "YES" and move < 0)
        return dict(details, allowed=not conflict,
                    reason="entry_direction_conflict" if conflict else "no_direction_conflict",
                    return_pct=str(move), observed_at=row.observed_at,
                    window_open=str(row.opening), observed_price=str(row.price))


def window_attempts(journal, start):
    """Include confirmed non-submissions, but never count a different period."""
    return [r for r in journal.data["windows"].values()
            if r.get("window_start_ms") == start * 1000
            and r.get("entry_kind") != late.LATE_KIND
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


def regular_pass(client, journal, series_list, base_budget, max_contracts, live, now,
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
    # Restrict ranks BEFORE all exclusions. Never promote rank three just
    # because rank one/two is BTC or a time-blocked SOL.
    blackout = sol_entry_blackout(clock())
    top_two = list(enumerate(rows[:TOP_COUNT], 1))
    selected = [(position, row) for position, row in top_two
                if row.series != BTC_SERIES
                and not (row.series == SOL_SERIES and blackout)]
    selected_series = {row.series for _, row in selected}
    if blackout and any(row.series == SOL_SERIES for _, row in top_two):
        core.event("M8_SOL_TIME_BLOCK", policy=POLICY, window_start_ms=start * 1000,
                   timezone="America/Chicago", blocked_start_hour=SOL_BLACKOUT_START_HOUR,
                   blocked_end_hour=SOL_BLACKOUT_END_HOUR, no_rank_three_promotion=True)
    core.event("M8_TOP2_QUEUE", policy=POLICY, entry_zone=core.ZONE1,
               window_start_ms=start * 1000,
               ranking_scope="available_coin_data",
               excluded_series=[s for s in names if s not in {r.series for r in rows}],
               skipped_btc_rank=next((i for i, r in enumerate(rows, 1)
                                      if r.series == BTC_SERIES), None),
               sol_time_restricted=blackout,
               skipped_sol_rank=(next((i for i, r in enumerate(rows[:TOP_COUNT], 1)
                                       if r.series == SOL_SERIES), None) if blackout else None),
               queue=[{"rank": position, "series": row.series,
                       "return_pct": str(row.return_pct)} for position, row in selected],
               skipped_lower_ranks=[r.series for r in rows[TOP_COUNT:]
                                    if r.series not in selected_series and r.series != BTC_SERIES])
    proxy = DirectionCheckedQueue(client, clock=clock, emit=core.event)
    proxy.start = start
    proxy.observations = {row.series: row for _, row in selected}
    proxy.ranks = {row.series: position for position, row in selected}
    if not live:
        # Preserve preview's one signal per period and no writes/no orders.
        return cash.run_cash_checked_pass(proxy, journal,
            tuple(row.series for _, row in selected), base_budget, max_contracts, False,
            clock(), max_entries_per_window)

    for _, row in selected:
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


def zone_buying_enabled():
    """Entry-only switch: disabling the zones must never disable owned exits."""
    return os.getenv("ZONE_BUYING_ENABLED", "true").strip().lower() == "true"


def run_pass(client, journal, series_list, base_budget, max_contracts, live, now,
             max_entries_per_window=1, *, provider, clock=None):
    """Run zone entries when enabled; always reconcile previously owned exits."""
    clock = clock or core.time.time
    if not zone_buying_enabled():
        # The core reconciles BOTH zones from the shared persistent journal
        # before examining new candidates. An empty series list prevents any
        # fresh single-coin entries, and bypasses late.run_late_pass entirely.
        return cash.run_cash_checked_pass(client, journal, (), base_budget,
                                          max_contracts, live, now,
                                          max_entries_per_window)
    regular_pass(client, journal, series_list, base_budget, max_contracts, live,
                 now, max_entries_per_window, provider=provider, clock=clock)
    # Regular pass always reconciles all owned positions first. Late entries
    # use separate durable records and half of the ACTIVE budget in aggregate.
    return late.run_late_pass(client, journal, series_list, base_budget,
                              max_contracts, live, now, provider=provider,
                              clock=clock, sol_blocked=sol_entry_blackout)


@contextmanager
def installed_policy(provider=None):
    """Explicit entrypoint only; importing leaves the legacy runner untouched."""
    previous = (core.ENTRY_START_SECONDS, core.ENTRY_END_SECONDS, core.run_pass)
    provider = provider or ranked.CoinbasePerformance()
    core.ENTRY_START_SECONDS, core.ENTRY_END_SECONDS = START_SECONDS, END_SECONDS
    leader_runner = None
    if os.getenv("LEADER_TRADING_ENABLED", "false").lower() == "true":
        try:
            path = Path(os.getenv("LEADER_STATE_PATH", "/data/crypto_leader15_live.json"))
            if os.getenv("RAILWAY_ENVIRONMENT_ID"):
                mount = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/missing-volume"))
                if not os.path.ismount(mount) or not path.resolve().is_relative_to(mount.resolve()):
                    raise RuntimeError("Leader journal requires the persistent volume")
            leader_runner = leader.LeaderRunner(path)
            core.event("LEADER_READY", live_enabled=True, market_series=leader.SERIES,
                       signal_source=leader.SIGNAL_SOURCE,
                       entry_start_second=leader.ENTRY_SECOND,
                       entry_end_second=leader.ENTRY_END_SECOND,
                       hold_until_settlement=leader.HOLD_UNTIL_SETTLEMENT,
                       side="NO", min_entry_cents=65,
                       budget_dollars=str(leader.WINDOW_BUDGET),
                       budget_after_loss_dollars=str(leader.AFTER_LOSS_BUDGET),
                       stop_loss_enabled=False)
        except Exception as error:
            core.event("LEADER_INITIALIZATION_FAILED", error_type=type(error).__name__)
            leader_runner = None

    def execute(*args, **kwargs):
        # Leader settlement reconciliation gets priority even when regular-
        # market processing has an unrelated exception. No shared journal,
        # automated pre-settlement exit, or single-coin risk-rule mutation.
        if leader_runner is not None:
            try:
                leader_runner.run(args[0], live=args[5])
            except Exception as error:
                core.event("LEADER_PASS_PAUSED", error_type=type(error).__name__)
        return run_pass(*args, **kwargs, provider=provider)

    core.run_pass = execute
    try:
        with cash.installed_guard():
            yield
    finally:
        core.ENTRY_START_SECONDS, core.ENTRY_END_SECONDS, core.run_pass = previous


def main(argv=None):
    with installed_policy():
        core.event("M8_TOP2_POLICY_READY", policy=POLICY,
                   entry_start_seconds=START_SECONDS, entry_end_seconds=END_SECONDS,
                   performance_top_n=TOP_COUNT, excluded_entry_series=[BTC_SERIES],
                   sol_blocked_entry_hours_central="01:00-04:00",
                   sol_blocked_timezone="America/Chicago",
                   backup_delay_seconds=BACKUP_DELAY_SECONDS,
                   max_candidate_attempts_per_window=MAX_CANDIDATE_ATTEMPTS,
                   max_filled_or_pending_markets=1, transfers_enabled=False,
                   buy_zones=[{"zone": core.ZONE1, "start_second": START_SECONDS,
                               "end_second": END_SECONDS},
                              {"zone": core.ZONE2, "start_second": late.LATE_START_SECONDS,
                               "end_second": late.LATE_END_SECONDS}],
                   late_entry_policy="ranked-top-two-96c-plus-v1",
                   late_entry_start_seconds=late.LATE_START_SECONDS,
                   late_entry_end_seconds=late.LATE_END_SECONDS,
                   late_entry_min_cents=89, late_entry_max_cents=97,
                   late_take_profit_cents=98,
                   late_take_profit_execution="gtc_after_confirmed_fill",
                   late_budget_fraction="1/2-active-trade-budget",
                   late_max_coins_per_window=late.MAX_LATE_COINS,
                   late_win_streak_independent=True)
        if not zone_buying_enabled():
            core.event("M8_ZONE_ENTRIES_DORMANT", new_zone1_buys=False,
                       new_zone2_buys=False, owned_tp_sl_reconciliation=True,
                       leader_runner_independent=True)
        return core.main(argv)


if __name__ == "__main__":
    main()

