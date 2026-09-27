# Strike Ruler — Opposite-Strike Opening Scalp

Python bot for Kalshi's 15-minute Bitcoin markets (`KXBTC15M`).
`EXECUTION_STRATEGY=strike_ruler` is the only supported execution strategy.

## Signals and timing

**Opening direction, 0:00–2:00:** buy the opposite side of the current BTC
reference versus the contract's fixed strike: above → NO; below → YES.
At equality or with an invalid/unavailable reference, wait and retry. Recheck
before every submission. This applies to all opening buy routes, including
regular tiers and optional triggers, independently of saved bias or lookbacks.
An attempt that starts before 2:00 cannot submit after that boundary.

**Early direction, 2:00–6:00:** use `2.1.1 Boruto Scalp No Skip` to select
YES or NO. Read four finalized Kalshi benchmarks at T−60, T−45, T−30 and
T−15 against the current contract's fixed strike. The newest point votes in
its own direction (above → YES); T−45 and T−60 vote inversely (below → YES).
T−30 is recorded but does not vote. Two agreeing votes choose the side; without
a majority, use the first nonzero vote in newest/T−45/T−60 order, with YES only
when all three are equal. Confidence, previous-bias conflicts, the research
YES-only filter and the $32.85 distance filter do not block these scalp signals.

Save the bias and its inputs as `entry_bias` before placing a Boruto-selected order.
Reuse that market's saved bias across polls and restarts; preserve older `signal`
records for audit. From 2:00 to 6:00, every under-70¢ buy route checks the same bias,
including regular, optional limit-batch, historical and spot triggers. Live BTC
crossing the strike cannot change direction in that window. Missing or invalid finalized
lookbacks block Boruto entries until the data is available; they cannot become
an invented signal. Entry logs include the selected side and its source.

**From 6:00 onward:** select YES above the live strike and NO below; wait at
equality or when the live reference is unavailable. Recheck before submitting.
These trades do not require the early bias or its lookbacks. High-price scalp
entries still require the higher ask, at least 70¢. The 75¢→83¢ rule starts at
minute 6; 70¢→76¢ starts at minute 8; 73¢→79¢ and 85¢→91¢ start at minute 11.
The final 97¢ limit continues to follow the live strike side.

Opposite inventory or unresolved opposing buys still block a new scalp until
they clear; only the final 97¢ route can deliberately close the opposite side
at a loss. Existing filled positions keep their saved exits and spending
reservations. Unfilled orders from an older execution policy reconcile before
new exposure. Quote snapshots at minutes 2, 4 and 6 remain diagnostics.

New entries follow fixed price-based windows within each 15-minute market:

| Entry | Window (start inclusive, end exclusive) |
| --- | --- |
| 52¢→60¢ opening entry | 0:00–2:00 |
| New 57¢→62¢ opening entry | 0:00–2:00 |
| 45–59¢ regular tiers | 0:00–6:00 |
| 60–69¢ regular tiers (currently 62¢/64¢/67¢) | 5:00–6:00 |
| New 75¢→83¢ tier | 6:00–12:00 |
| 70¢→76¢ tier | 8:00–12:00 |
| 73¢→79¢ and 85¢→91¢ late tiers | 11:00–12:00 |
| 97¢ settlement limit | 12:00–15:00 |

Eligible early buys can start **immediately at contract open**. There is no
one-minute waiting period. The opening, regular, optional limit-batch, historical
and spot routes still enforce their own prices, side rules, funds and deadlines.
Buy limits of 60¢ or more still wait until minute 5; the 75¢, other 70¢+ and
settlement routes retain their later windows. The shared gateway blocks buys
before contract open, regardless of a caller timestamp or stale start setting.
Order reconciliation and exit monitoring continue throughout the contract.

The 35¢ buy is retired. The 57¢ rule requires an exact quoted ask; the 75¢ rule
triggers at an ask of at least 75¢. Neither limit can pay more, although exchange
price improvement can produce a cheaper fill. The 57¢ rule has its own persisted attempt, independent
of the 52¢ opening flag. Quote/cash waits can retry before minute 2; an accepted
or ambiguous attempt cannot be duplicated after restart.
The 75¢ tier is the only new entry available from 6:00 to 8:00. Other 70¢+
limits wait until 8:00, and ordinary limits below 70¢ stop at 6:00. The shared
gateway caps the 57¢ route at 2:00 on every path. Slow calls cannot extend a deadline.
Every route also blocks buy limits of 60¢ or higher before 5:00, even when the
current ask is cheaper. The later 75¢, 70¢, 73¢/85¢ and settlement windows still
apply. Buy logs identify each order's tier and batch separately; the batch number
is not an order count. Regular batches can repeat after the configured interval
while allowance remains. The earlier allowance is not replenished by sales.
Funds, quote checks and existing batch limits still apply. Exit monitoring
continues until market close.

## Entries, exits and budgets

All routes use these default outcome-price pairs, configurable through
`ENTRY_EXIT_PAIRS_CENTS`. Each pair defines an entry limit and a **gross profit
increment** (second price minus first). The exit column below assumes a fill
at the entry limit; cheaper fills now produce lower exit targets.

| Entry limit | Exit if filled at the limit |
| --- | --- |
| 45¢ | 55¢ |
| 48¢ | 53¢ |
| 51¢ | 56¢ |
| 53¢ | 58¢ |
| 56¢ | 61¢ |
| 59¢ | 64¢ |
| 62¢ | 67¢ |
| 64¢ | 69¢ |
| 67¢ | 72¢ |
| 70¢ (from minute 8) | 76¢ |
| 75¢ (from minute 6) | 83¢ |
| 73¢ (from minute 11) | 79¢ |
| 85¢ (from minute 11) | 91¢ |

From contract open until 2:00, the bot may submit an opposite-strike 52¢ limit
with a 60¢ target and an independent 57¢ limit with a 62¢ target. Each route
uses the shared allowance and at most $2.80 per order, including entry fee room.
Opening orders are IOC; an unfilled quantity is canceled immediately, and no
new opening submission may occur at or after 2:00. Each attempt rechecks direction
and opposing inventory. Quote or funding waits do not consume the opportunity.

Regular entries and optional limit batches use the active window's side rule.
Historical-strike touches may trigger an attempt but cannot override the active side rule.
The optional spot trigger still requires BTC sufficiently above the current strike;
it buys NO during the opening window. If configured to extend beyond minute 2,
it retains the existing YES-bias requirement there.

Earlier entry orders request **up to 5 whole contracts**, on opening, regular,
limit-batch, historical, spot and late routes. All routes share a fixed **$21
allowance per 15-minute market**, including entry fee reserves. Of that, **$6 is
reserved for the final-three-minute settlement entry**, leaving **$15 for all
earlier routes combined**. Each earlier order uses at most **$2.80 including fees**,
subject to the $15 earlier allowance. For example, an initial 57¢ or 67¢ order requests
4 contracts, and a 75¢ or 85¢ order requests 3. The final order may shrink further
to the whole contracts affordable from the remaining allowance. The earlier allowance covers five maximum $2.80 order allocations; it does not force buys
outside their price/time rules or guarantee fills. Exchange partial fills remain possible, and exits sell only
verified filled inventory.
Legacy `ENTRY_BUDGET_DOLLARS` and `MARKET_BUDGET_DOLLARS` settings are ignored.
Existing reservations survive upgrades and restarts until reconciled.

Reservations include a conservative 3¢ per-contract entry fee cushion and are
saved before submission. Explicit HTTP 400 `insufficient_balance` rejections
release their reservation because no order was accepted. A terminal order with
verified fill counts releases only its unfilled allowance. Partial fills also
require verified maker/taker fees; retained allowance covers filled contracts
and at least their actual entry fees. Missing counts, missing partial-fill fees
and ambiguous submissions retain their allowance while reconciliation retries.
Sales do not replenish filled-entry allowance. Exit fees are separate.
`MAX_PURCHASES_PER_MARKET` counts individual committed regular orders, including
unresolved submissions; proven zero-fill orders do not consume this count.

The 75¢ tier triggers from minute 6 when the selected ask is at least 75¢ and
rests at a maximum purchase price of 75¢ until minute 12. Only one 75¢ order may
be pending at once; it is canceled if the live strike side changes. The final
97¢ limit triggers at an ask of at least 97¢ and rests until contract close.
Both need executable liquidity at or below their limits to fill. Other entry
orders remain IOC. Opening and late tiers retry eligible checks until their
cutoffs, using persisted per-tier intents to prevent duplicate filled or
ambiguous attempts. Confirmed zero-fill attempts may retry.

`ENTRY_SKIP` records each attempted tier's rejection reason. Terminal allowance
recovery emits `ENTRY_UNUSED_ALLOWANCE_RELEASED`; incomplete proof emits
`ENTRY_ALLOWANCE_RECONCILIATION_PENDING`. Order counts and fee fields follow the
[Get Order response](https://docs.kalshi.com/api-reference/orders/get-order).

An independent worker reconciles fills and net inventory and submits reduce-only
immediate-or-cancel exits at the calculated target or better. It retries remaining
holdings after partial fills. These are **bot-managed exits**, not resting exchange
brackets. They require the process, API and executable liquidity to be available.
There is no general stop-loss. The explicit 97¢ settlement transition may close
opposite inventory at a loss, as described below. Opposite-side fills net against existing holdings;
the monitor does not assume independent YES and NO positions. Inconsistent or
ambiguous fill accounting pauses new entries until reconciliation succeeds.

### Take profit follows actual fills

The live worker groups remaining shares by their saved profit increment, then
sets the target to their quantity-weighted **actual fill cost plus that increment**.
It rounds up to a whole cent, so a 57.3¢ average with a 5¢ increment targets 63¢.
A 57¢ fill still targets 62¢; a 75¢ fill targets 83¢; a minute-8 70¢ fill targets
76¢. Entry and exit fees are excluded from this gross price increment.

Each consistent fill/position snapshot recalculates both target and full remaining
quantity. For example, five shares at 57¢ plus five at 47¢, both using the 5¢
rule, produce ten shares averaging 52¢ and a 57¢ target. Different increments
retain separate averages. The settlement position remains held to settlement.

Before submitting an exit, the worker saves the exact entry fill IDs and quantities
it covers. Partial exits consume those allocations FIFO; sold shares are removed
from the next average. Manual reductions also consume FIFO. Pending submissions
must reconcile before another exit can use that inventory. This survives partial
fills, lost acknowledgements and restarts without selling a share twice.

On upgrade, existing recorded exit orders replay against their original fixed
targets. Remaining verified scalp inventory then adopts fill-based pricing using
its saved pair's profit increment, including older entry intents. Missing,
inconsistent or over-limit fill prices pause the worker instead of substituting
a quote, buy limit or displayed account average. `TP_ARMED` logs include the
average fill cost, profit increment, quantity and rounded target.

Price inputs follow Kalshi's [fill payload](https://docs.kalshi.com/api-reference/portfolio/get-fills).
Whole-cent rounding uses prices valid across the documented
[price grids](https://docs.kalshi.com/getting_started/fixed_point_migration),
without introducing a quote lookup into exit monitoring.

## Configuration and operation

Copy `.env.example` and provide `KALSHI_API_KEY_ID` plus one private-key source:
`KALSHI_PRIVATE_KEY_PEM`, `KALSHI_PRIVATE_KEY_B64` or `KALSHI_PRIVATE_KEY_PATH`.
The client is production-only. `TRADING_ENABLED=false` performs a read-only
credential check and waits; `python bot.py --check` checks and exits.

Important defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `ENTRY_EXIT_PAIRS_CENTS` | `45:50,47:52,49:59,55:62,56:61,61:70` | Entry limits and corresponding exits |
| `ENTRY_BUDGET_DOLLARS` | Ignored | Earlier entries use up to $2.80 and 5 contracts; 97¢ settlement requests 6 |
| `OPENING_BIAS_PAIR_CENTS` | `52:60` | Existing opening entry; independent 57:62 rule is fixed in code |
| `OPENING_WINDOW_MINUTES` | `2` | Opening order cutoff and cancellation time |
| `MARKET_BUDGET_DOLLARS` | Ignored | Market allowance is fixed at $21 including entry fee reserves |
| `MAX_PURCHASES_PER_MARKET` | `7` | Maximum committed regular orders; proven zero-fill attempts excluded |
| `ENTRY_INTERVAL_SECONDS` | `7` | Minimum interval between regular batches |
| `ENTRY_START_MINUTE` | `0` (fixed) | Early buys are eligible at contract open |
| `ENTRY_END_MINUTE` | Ignored | Fixed price-based windows listed above |
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

Existing 25¢, 32¢, 38¢ and 39¢ inventory retains its original pair for historical
exit attribution and profit-increment calculation. These tiers are retired and
cannot be reintroduced by an old Railway setting.
The retired 32¢ regular tier is ignored even if an old environment setting lists it.
Existing 35¢ inventory retains the 7¢ increment from its recorded 35¢→42¢ pair.
Stale environment settings cannot restore new 35¢ buys, extend 57¢ buys past
minute 2, or change the new rules' profit increments.

Legacy strategy records remain readable only to prevent adopting inventory that
belongs to an archived strategy. The old execution module and its configuration
options have been removed. Existing market signals and reservations survive an
upgrade; newly created signals use the strict lookback validation.

Old `TAKE_PROFIT_CENTS`, `TAKE_PROFIT_PERCENT`, `STOP_EXIT_CENTS`, `ENTRY_MIN_CENTS`,
`ENTRY_MAX_CENTS`, single-price and final-entry settings do not control this
strategy. Startup logs identify ignored settings. The prediction schedule is
fixed at 2, 4 and 6 minutes. Remove obsolete timing variables; the price-based
windows above are enforced in code.

## Validation and deployment

Order-history recovery uses the `ticker` filter on `GET /portfolio/orders`.
This aggregate endpoint rejects `exchange_index=-1`; omitting `exchange_index`
returns matching orders across shards without requiring another market lookup.
The API2 auto-routing parameters used for individual order and cancellation
requests must not be copied into this aggregate query. Exact order ID/ticker
matching, pagination, and unresolved-exit protection still apply.

The V2 order endpoint quotes the YES leg: an intentional NO entry is sent as a
YES ask at `1 - NO price`, so an account-history label of "Sold Yes" alone does
not identify a take-profit exit or an accidental position reversal. Exit orders
remain reduce-only immediate-or-cancel orders.

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
balance. These checks preserve each order's allocation and the $6 settlement
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

The versioned notes below describe historical releases; current rules and caps
are listed in **Entries, exits and budgets** above.

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

## Entry quote floors

New entries require a fresh selected-outcome ask of at least 45 cents and below
100 cents. IOC routes also require an ask no higher than their limit. The retired
35¢ exception is removed. New 57¢ entries require an ask of exactly 57¢. The 75¢
and 97¢ routes trigger at or above their respective limits and may rest there;
the submitted maximum prices remain 75¢ and 97¢. Missing or invalid quotes block entry.
High-price scalps require the selected side to have the strictly higher ask and
a quote of at least 70¢. The explicit 75¢ tier opens at 6:00; other high-price
limits open from 8:00, with the existing 73¢/85¢ route starting at 11:00.

Other buys use immediate-or-cancel. A quote check cannot enforce a minimum
exchange fill price if the book changes before matching; favorable execution
below the limit remains possible, including for resting orders. New rules share the $21 cap ($15 earlier /
$6 settlement), and existing inventory keeps its recorded exits.

## Request and order-status recovery

The entry, exit and balance-diagnostic clients share a thread-safe cooldown after
any HTTP 429. Without a usable `Retry-After`, repeated limits back off for 2, 4,
8, 16 and up to 30 seconds; a longer seconds/date header is honored. Exits can
resume first, entries one second later, diagnostics two seconds later. Requests
already in flight cannot be recalled. There is no lock held during HTTP and no
automatic replay of order POSTs. A locally deferred request was never sent;
only that new reservation may be released, with its audit record retained.
Accepted or ambiguous orders keep their reservations and recovery IDs until
terminal fill counts prove an unused allowance as described above.

Adjacent tier/funding checks reuse market snapshots for at most half a second,
measured from request start. Mutating requests invalidate the cache. Cash is
still read per attempt, and expiry, quote-floor, side and budget checks apply.

When a submitted exit is absent from both order lookups, has no recoverable
acknowledgement yet, or remains nonterminal, `TP_STATUS_PENDING` reports a
persisted retry time. Status reads back off for 2, 4, 8 and up to 15 seconds.
The pending ID survives restart; new entries remain paused, and no replacement
exit is submitted until terminal status and remaining inventory are reconciled.
An unresolved exit is retained for audit at market close. This handles delayed
visibility conservatively; it does not assume every 404 will eventually resolve.

API reference: [Kalshi rate limits](https://docs.kalshi.com/getting_started/rate_limits).

## Final-three-minute 97-cent limit and side transition

In minutes 12–15, the settlement route selects the live BTC strike side: YES
above the current strike or NO below it. It requires that side's ask to be at
least 97 cents and below 100 cents, rechecks before submission, and submits a
fixed 97-cent GTC limit expiring at contract close. Quotes of 97.2, 98 or 99 cents
can therefore trigger a resting order. The order cannot buy above 97 cents;
it may remain unfilled if offers never reach its limit. A better execution price
is still possible. The ordinary minute-12 cancellation sweep preserves this
order's separate contract-close deadline.
The $6 reserve covers six contracts plus the existing 3-cent per-contract fee
cushion. The overall cap is $21. One filled or unresolved settlement attempt per
market and monitor-health gating remain; a confirmed zero-fill cancellation can
retry before close. Existing 98/99-cent settlement lots remain
recognized, keep their $1 target, and are excluded from scalp exits. No fill or
profit is guaranteed.

If the 97¢ side opposes existing inventory or the prior side lock, persist a
settlement-switch request and block other entries. Confirm prior buy orders are
terminal first. The independent exit worker reconciles any existing exit, then
submits a reduce-only IOC for the opposite holding at its observed bid, even
when that realizes a loss. This close may include manually held opposite inventory.
Partial closes retry only after status reconciliation; missing acknowledgements,
404s and inconsistent fills pause the transition across restarts.

The entry worker switches its side lock and buys only after the exit worker has
confirmed no opposite inventory, with exit fills reflected in history. It rereads
the position, live strike side and qualifying quote before submission. Loss-taking exits use FIFO
inventory accounting and do not reset the $21 spending ledger. Missing liquidity,
an unavailable qualifying quote, insufficient cash/budget, or market close can prevent the
transition from completing. Logs distinguish `SETTLEMENT_CLOSE_*` from normal
take-profit events and record the requested/ready side transition.

## 2.2.8 — $21 shared market allowance

Raise the per-market cap to $21 including entry fee reserves: $15 for all earlier routes and $6 reserved for the 97-cent settlement entry. Existing spending reservations remain counted across upgrades and restarts; sales do not replenish the allowance. Entry timing, price pairs, per-order sizing and exit tracking are unchanged.

## 2.2.9 — Shared inventory and IOC recovery

Manual trades and other bots can share the account. Normal take-profit exits
cover only lots linked to this bot's durable entry intents; outside lots are
excluded from exit quantities and average costs and logged as
`TP_OUTSIDE_INVENTORY`. Full fill history must still reconcile to the account's
net position. External reductions consume lots FIFO; they can reduce this bot's
remaining attributable quantity. The existing settlement transition still has
its separate, explicit authorization to close opposite net inventory.

The monitor refreshes entry state after fetching fills and retries one
inconsistent position snapshot before pausing. IOC placement receipts are
persisted across restarts and validated against order/client IDs and quantity.
A receipt with a final zero remaining count proves completion without waiting
for the order lookup to appear. Missing or invalid receipts still require
read-only recovery. Partial entry fees must be verified before releasing unused
allowance, and confirmed exit fills must appear in history before another exit.
The $21 cap, $6 settlement reserve, entry rules and profit increments remain.

Receipt semantics: https://docs.kalshi.com/api-reference/orders/create-order-v2
