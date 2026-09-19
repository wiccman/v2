# Strike Ruler — v0.9.6

Python bot for Kalshi's 15-minute Bitcoin markets (`KXBTC15M`).
`EXECUTION_STRATEGY=strike_ruler` is the only supported execution strategy.

## Signals and timing

The base signal compares the three immediately preceding finalized settlement
prices with the current strike. Two or three prices below the strike produce YES;
two or three above produce NO. Three on the same side gives HIGH confidence,
two gives MODERATE; otherwise the signal is SKIP. Missing, invalid or conflicting
lookback data blocks signal creation; older settlements cannot fill a gap.
The signal is retained for that market. `ABSOLUTE_GAP_AVERAGE` is a legacy input
and does not change this majority rule.

Predicted-side ask snapshots are scheduled at minutes 2, 4 and 6. A capture may
be at most 15 seconds late and records its actual observation time. A later cycle
marks overdue slots missed instead of inventing earlier prices. The final average
is available only when all three captures exist. These quoted prices are market
implied values, not calibrated probabilities of success.

New regular, dual and historical entries run from minute 2 until strictly before
minute 5. No route may submit after 5:00, including after a slow API call. Entry
orders expire at the absolute 6:00 market boundary; cancellation sweeps retry
failed requests. Exit monitoring continues after both cutoffs.

## Entries, exits and budgets

All routes use these default outcome-price pairs, configurable through
`ENTRY_EXIT_PAIRS_CENTS`:

| Entry limit | Exit target |
| --- | --- |
| 32¢ | 39¢ |
| 39¢ | 46¢ |

During the first two minutes, the bot posts one bias-selected 52¢ entry limit with a 60¢ target. It never posts both complementary opening sides. The order expires and is canceled at 2:00 if it has not filled. This opening route uses the same per-trigger budget and market allowance as every other route.

Regular bias-based entries run first when a valid prediction snapshot is available.
An optional dual batch submits both tiers on both YES and NO. Historical-strike
touches use the side of approach; the optional early spot trigger buys YES when
spot is sufficiently above the current strike. This early route operates before
minute 2 by default; `ENTRY_START_MINUTE` applies to the other three routes.

`ENTRY_BUDGET_DOLLARS` is desired principal for one trigger. Regular, historical
and spot triggers split it across two tiers. A dual batch splits the same amount
across **both sides and both tiers**, rather than receiving a separate allowance
per side. All routes share `MARKET_BUDGET_DOLLARS`, hard-capped at $5 per market.
The current Railway override is $2 per trigger; the source default is $0.77.

Reservations include a conservative 3¢ per-contract entry fee cushion and are
saved before submission. Rejections, cancellations, partial fills, sales and
restarts do not replenish the allowance. This intentionally limits retries and
can leave part of the allowance unused. Exit fees are separate.
`MAX_PURCHASES_PER_MARKET` limits regular trigger batches; it does not allocate
additional money or count each tier as a separate purchase.

An independent worker reconciles fills and net inventory and submits reduce-only
immediate-or-cancel exits at the paired target or better. It retries remaining
holdings after partial fills. These are **bot-managed exits**, not resting exchange
brackets. They require the process, API and executable liquidity to be available.
There is no active stop-loss. Opposite-side fills net against existing holdings;
the monitor does not assume independent YES and NO positions. Inconsistent or
ambiguous fill accounting pauses new entries until reconciliation succeeds.

## Configuration and operation

Copy `.env.example` and provide `KALSHI_API_KEY_ID` plus one private-key source:
`KALSHI_PRIVATE_KEY_PEM`, `KALSHI_PRIVATE_KEY_B64` or `KALSHI_PRIVATE_KEY_PATH`.
The client is production-only. `TRADING_ENABLED=false` performs a read-only
credential check and waits; `python bot.py --check` checks and exits.

Important defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `ENTRY_EXIT_PAIRS_CENTS` | `32:39,39:46` | Entry limits and corresponding exits |
| `ENTRY_BUDGET_DOLLARS` | `0.77` | Principal per trigger or entire dual batch |
| `OPENING_BIAS_PAIR_CENTS` | `52:60` | First-two-minute one-sided entry and exit |
| `OPENING_WINDOW_MINUTES` | `2` | Opening order cutoff and cancellation time |
| `MARKET_BUDGET_DOLLARS` | `5` | Shared cap including entry fee reserves |
| `MAX_PURCHASES_PER_MARKET` | `7` | Maximum regular trigger batches |
| `ENTRY_INTERVAL_SECONDS` | `7` | Minimum interval between regular batches |
| `ENTRY_START_MINUTE` | `2` | Earliest new entry |
| `ENTRY_END_MINUTE` | `5` | Entry cutoff, capped at five minutes |
| `POLL_SECONDS` | `5` | Entry-loop delay |
| `EXIT_POLL_SECONDS` | `1` | Independent exit-loop delay |
| `STATE_PATH` | `/data/state.json` | Durable entry ledger |
| `LOG_PATH` | `/data/trades.csv` | Event CSV, also emitted to stdout |

Railway must mount a persistent volume containing both state and logs. A process
lock prevents two bots from owning the ledger. Missing or malformed state stops
startup instead of creating a fresh spending allowance. Restore the existing
ledger after any volume loss; never clear it to bypass a reconciliation or budget
block. For a genuinely new installation with no previous orders or positions,
explicitly create `state.json` containing `{"markets": {}}` on the mounted volume
before enabling trading. Preserve the adjacent take-profit receipt file too.

Existing 25¢-tier inventory retains its recorded 31¢ exit target after this
price change. New entries use 32¢→39¢ and 39¢→46¢.

Legacy strategy records remain readable only to prevent adopting inventory that
belongs to an archived strategy. The old execution module and its configuration
options have been removed. Existing market signals and reservations survive an
upgrade; newly created signals use the strict lookback validation.

Old `TAKE_PROFIT_CENTS`, `TAKE_PROFIT_PERCENT`, `STOP_EXIT_CENTS`, `ENTRY_MIN_CENTS`,
`ENTRY_MAX_CENTS`, single-price and final-entry settings do not control this
strategy. Startup logs identify ignored settings. The prediction schedule is
fixed at 2, 4 and 6 minutes. Set Railway's entry end to 5 to match the enforced
cutoff, and remove obsolete variables rather than using them to configure exits.

## Validation and deployment

Install `requirements.txt` and pytest, then run `python -m pytest -q`.
The tests use fake exchange clients and do not place live trades.

Review the change branch before updating Railway's deployed branch. Keep the
existing volume and ledger, deploy one replica, and verify startup and event logs.
With `TRADING_ENABLED=true`, deploying starts live operation immediately.
