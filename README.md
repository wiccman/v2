# Strike Ruler — 2.0.9 Boruto

Python bot for Kalshi's 15-minute Bitcoin markets (`KXBTC15M`).
`EXECUTION_STRATEGY=strike_ruler` is the only supported execution strategy.

## Signals and timing

The signal build is **2.0.1 Boruto Four-Point Current Bias**. Four finalized
Kalshi settlements at T−60, T−45, T−30 and T−15 are compared with the official
strike at opening T. At least three below gives YES; at least three above gives
NO. Four agreeing votes are HIGH, three are MODERATE. Other splits are LOW and use the latest non-equal lookback; a fully flat window defaults to YES.
The current YES/NO bias controls entries even when the previous bias disagrees.
Previous-window data is optional diagnostic context and never blocks the current
signal. Missing or unfinalized current lookbacks still block signal creation.
Quotes, gaps and live spot never change the bias.

BASE_SIGNAL logs include build, both raw biases, vote counts, exact lookback
boundaries and source tickers, final decision and selection reason. The current window
must be verified before the signal is saved. Older saved market signals and
spending records are preserved; new entries wait until a new market, while
the independent exit monitor continues managing existing inventory.

Predicted-side ask snapshots are scheduled at minutes 2, 4 and 6. A capture may
be at most 15 seconds late and records its actual observation time. A later cycle
marks overdue slots missed instead of inventing earlier prices. The final average
is available only when all three captures exist. These quoted prices are market
implied values, not calibrated probabilities of success.

New regular, bias-limit and historical entries run from minute 0 until strictly before
minute 8. These routes cannot submit at or after 8:00, including after a slow API call. Their
orders expire at the absolute 8:00 market boundary; cancellation sweeps retry
failed requests. Exit monitoring continues after both cutoffs.

## Entries, exits and budgets

All routes use these default outcome-price pairs, configurable through
`ENTRY_EXIT_PAIRS_CENTS`:

| Entry limit | Exit target |
| --- | --- |
| 38¢ | 43¢ |
| 39¢ | 46¢ |
| 49¢ | 59¢ |
| 55¢ | 62¢ |
| 56¢ | 61¢ |
| 61¢ | 70¢ |

During the first two minutes, the bot posts one bias-selected 52¢ entry limit with a 60¢ target. It never posts both complementary opening sides. The order expires and is canceled at 2:00 if it has not filled. This opening route uses the same five-contract sizing and shared market allowance as every other route.

Regular bias-based entries run first when a valid prediction snapshot is available.
An optional limit batch submits the regular tiers only on the current bias side. Historical-strike
touches use the side of approach; the optional early spot trigger buys YES when
spot is sufficiently above the current strike. This early route operates before
minute 2 by default; the other three routes start at minute 0.

Earlier entry orders request exactly **5 contracts**, on opening, regular,
limit-batch, historical, spot and late routes. All routes share a fixed **$25
allowance per 15-minute market**, including entry fee reserves. Of that, **$10 is
reserved for the final-two-minute settlement entry**, leaving **$15 for all
earlier routes combined**. An earlier order is not
submitted if five contracts plus the fee reserve will not fit the remaining
allowance; the bot does not shrink it to a fractional order. Exchange partial
fills remain possible, and exits sell only verified filled inventory.
Legacy `ENTRY_BUDGET_DOLLARS` and `MARKET_BUDGET_DOLLARS` settings are ignored.
Existing reservations remain intact when upgrading or restarting.

Reservations include a conservative 3¢ per-contract entry fee cushion and are
saved before submission. Explicit HTTP 400 `insufficient_balance` rejections
release their reservation because no order was accepted. Cancellations, partial fills, sales,
ambiguous failures and restarts do not replenish the allowance. This intentionally limits retries and
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
| `ENTRY_EXIT_PAIRS_CENTS` | `38:43,39:46,49:59,55:62,56:61,61:70` | Entry limits and corresponding exits |
| `ENTRY_BUDGET_DOLLARS` | Ignored | Earlier entries request 5 contracts; final settlement entry requests 10 |
| `OPENING_BIAS_PAIR_CENTS` | `52:60` | First-two-minute one-sided entry and exit |
| `OPENING_WINDOW_MINUTES` | `2` | Opening order cutoff and cancellation time |
| `MARKET_BUDGET_DOLLARS` | Ignored | Market allowance is fixed at $25 including entry fee reserves |
| `MAX_PURCHASES_PER_MARKET` | `7` | Maximum regular trigger batches |
| `ENTRY_INTERVAL_SECONDS` | `7` | Minimum interval between regular batches |
| `ENTRY_START_MINUTE` | `0` (fixed) | Earliest new entry |
| `ENTRY_END_MINUTE` | `8` (fixed) | Older environment overrides are ignored |
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
price change. Existing 32¢ inventory retains its 39¢ target; new regular entries use the six pairs listed above.
The retired 32¢ regular tier is ignored even if an old environment setting lists it.

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

## 2.0.1 entry change

Removed the 32¢ regular buy tier and the previous/current bias conflict gate.
Opening 52¢→60¢ and late entries are unchanged. This does not guarantee a fill
or add a buy at the minute-6 snapshot after the regular minute-5 cutoff.

## API cash in Railway logs

Before each new entry, the bot reads the market's authoritative `exchange_index`
and checks cash on that shard, including its entry fee reserve. Cash elsewhere
does not fund that order. Missing or malformed funding data blocks new entries;
low cash and explicit balance rejections wait 30 seconds before retrying. The
independent exit worker keeps running. No transfers or automatic rebalancing
are performed. Funding the market's shard is a separate account action.

`ENTRY_WAIT_MARKET_CASH` logs the shard, its available cash and the required
amount. `ENTRY_CASH_UNAVAILABLE` means the read could not be verified, not a zero
balance. These checks preserve five-contract sizing and the $10 settlement
allowance. They cannot guarantee acceptance if funds change before submission.

Cancellation checks terminal status first. An already executed, canceled or
expired order is reconciled without another DELETE. A cancellation 404 by
itself is never treated as proof that an order is finished.

Reduce-only exits remain IOC: Kalshi V2 rejects reduce-only GTC orders. Keeping
this protection prevents a stale exit from opening an opposite position after
a manual close or other fill. A `TP_SUBMITTED` message alone does not prove a sale;
`TP_FILL` reports a reconciled fill, and `TP_POSITION_FLAT` only reports inventory.

API references: [exchange sharding](https://docs.kalshi.com/getting_started/exchange_sharding),
[order constraints](https://docs.kalshi.com/api-reference/orders/create-order-v2).

At startup, search deployment logs for `API_CASH_BALANCE`. `cash_dollars` is
cash returned by the connected API account, separate from `portfolio_value_dollars`.
The read uses the primary account and includes all exchange indexes; returned
`exchange_balances` show the breakdown when available. Dollar-format balances
are preferred; legacy integer cents are divided by 100. Missing or malformed
balances produce `API_CASH_BALANCE_ERROR`, not a fabricated zero.

This makes one authenticated GET and prints only selected numeric balance fields.
It does not place a test trade, change sizing, or fix rejected-order reservations.
It does not prove that the API account matches an account displayed on a phone,
and aggregate cash alone does not establish that a specific order is fundable.
The 2.0.1 signal identifier remains unchanged so this diagnostic-only release does
not invalidate existing signals. After deployment, the balance line is visible
from a phone in v2's deployment logs. `python bot.py --check` also emits it.

## 2.0.3 — Additional regular entry pairs

Added 55¢→62¢, 49¢→59¢, 38¢→43¢, 56¢→61¢, and 61¢→70¢ alongside 39¢→46¢. These additions are applied even with an older Railway ENTRY_EXIT_PAIRS_CENTS setting. Trigger budgets are divided among regular tiers; the shared market cap is now $10; timing, opening and late pairs are unchanged. See 2.0.5 for explicit balance-rejection reservation recovery.

## 2.0.4 — Direction on every valid window

Four-point majorities keep their existing direction (3 below → YES, 3 above → NO). Mixed windows use the most recent non-equal lookback: below → YES, above → NO. If all four equal the strike, the explicit fallback is YES. These fallback signals have LOW confidence and are eligible for regular entries. Previous bias does not veto a signal. Valid four-point data always produces YES or NO; missing/invalid data still blocks execution. Timing, $10 allowance, entry pairs and order checks remain in place. A saved older-build window waits until the next market before using this signal revision.

## 2.0.5 — Fresh account-scope diagnostics

Every 60 seconds a separate GET-only worker logs available cash from the default API account and balances returned by `/portfolio/subaccounts/balances`, preserving each subaccount number, exchange index and update timestamp. Default-account scope is explicit: the report is not the entire website portfolio. Failed/restricted lookups log only error type and status, never a fabricated zero or credentials. The diagnostic client has a five-second timeout and does not block the entry or exit workers. Order routing, account selection, trading rules and signal build are unchanged.

### Entry reservation recovery

A structured HTTP 400 `insufficient_balance` rejection releases only that new intent's reservation, preserves the rejected attempt and released amount for audit, and logs `ENTRY_RESERVATION_RELEASED`. Timeouts, 409/5xx responses, unrecognized errors and accepted orders retain their budget; filled, canceled and sold orders do not recycle allowance. Old closed intents are not retroactively refunded because the earlier state did not record proof of rejection. The $10 cap, prices, account routing and take-profit checks remain unchanged. This fixes false exhaustion of the bot allowance; it cannot make funds in another account available to this API account.

## 2.0.6 — Eight-minute regular entry window

Regular, bias-limit and historical-strike entries are eligible from 0:00 through 7:59 of each 15-minute market. Unfilled regular orders expire at 8:00, and no regular POST may occur at or after that boundary. Previous Railway start/end values cannot shorten this window. The 52¢ opening rule remains limited to the first two minutes; the separate late rules remain at minutes 11–13. Exit monitoring continues throughout the market. The $10 shared cap, purchase-count limit and entry interval still apply, so eligibility for eight minutes does not guarantee continuous purchases. Existing saved order expirations are respected; new regular orders use the eight-minute deadline.

## 2.0.7 — Five-contract orders and $25 shared allowance

All entry routes request five contracts per order. The shared market allowance is $25 including entry fee reserves. Prior reservations are preserved; insufficient remaining allowance prevents a new order rather than reducing its quantity. Existing exits and price targets are unchanged. Five contracts do not guarantee 25¢ net profit at every price pair.

## 2.0.8 — Reserved $10 final-two-minute settlement entry

Between 13:00 inclusive and 15:00 exclusive, buy whichever single side has an
ask of exactly 97¢, independent of Strike Ruler bias. Reserve $10 of the existing
$25 cap; earlier routes share $15. Request 10 whole contracts ($9.70 principal
plus fee room) in one 97¢ limit IOC order. Partial or zero fills are possible;
the bot does not repeatedly buy after an acknowledged or ambiguous attempt.
The intent is persisted before submission and survives restarts. Slow calls
cannot submit past close. Existing reservations are preserved on upgrade.

These lots are held until settlement; the exit worker reconciles them but never
sends a take-profit order for them. Earlier scalp inventory keeps its existing
exit targets. If opposite-side inventory or unresolved opposing entry orders
remain, wait for them to clear so the new purchase does not merely net them out.
The strategy checks on the regular polling cadence and needs available prediction
account funds; the budget reservation does not transfer cash between accounts.

During the final two minutes, `SETTLEMENT_97_CHECK` records both observed asks,
time remaining, reserved allowance and why the route waits or proceeds. An ask
of 96¢, 98¢ or 99¢ does not meet the existing exact-97¢ rule. The bot checks on
its polling cadence, so a brief 97¢ quote can be missed between polls. An
acknowledged order ID is required for `SETTLEMENT_97_ENTRY`; it does not prove
that the IOC filled. Funding waits, conflicting inventory and prior attempts
remain subject to the existing protections and one-attempt policy.
