# Hybrid co-trading: manual targets plus the existing automatic strategy

## Review status

PR #87 integrates the manual exit lifecycle into the existing exit monitor. It
is **disabled by default and has not been merged or deployed**. All testing in
this change is offline. No claim of live latency, profitability, or production
readiness is implied by the simulator results.

**Important API correction:** this implementation does not post a resting sell
behind a manual buy. Kalshi's current V2 Create Order contract rejects
`reduce_only=true` unless `time_in_force=immediate_or_cancel`. The old draft's
GTC/reduce-only combination was invalid. This implementation stores a monitored
limit target locally, then submits a price-protected, reduce-only IOC when the
best bid reaches that target. Unfilled contracts stay monitored. It neither
silently falls back to an unprotected GTC order nor opens new exposure to exit.

Consequences: the bot must remain running; a brief favorable price can be missed;
there is no resting order earning queue priority; the sell may incur taker fees;
and an IOC can fill partially or not at all. This is a supported alternative to,
not an implementation of, the originally requested exchange-hosted resting sell.

Official API references (reviewed October 12, 2026):

- https://docs.kalshi.com/api-reference/orders/create-order-v2
- https://docs.kalshi.com/getting_started/order_direction
- https://docs.kalshi.com/getting_started/fixed_point_migration
- https://docs.kalshi.com/api-reference/orders/get-orders
- https://docs.kalshi.com/api-reference/portfolio/get-fills
- https://docs.kalshi.com/api-reference/portfolio/get-positions

## What is implemented

`hybrid_runtime.py` is the optional composition entry point. Disabled mode calls
`bot.main()` without wrapping its strategy. Enabled mode installs a separate
manual manager within `TakeProfitMonitor` and shares an execution lock with the
existing `funded_entry` function. The manual manager cannot place entry orders.
The original `bot.py` strategy, sizing, budgets, and automatic profit constants
are unchanged.

The manager discovers primary-account positions in an explicit series allowlist,
retrieves paginated fills/orders, resolves known bot IDs, and replays signed fills
against the actual net position. It manages only new non-bot limit-order exposure
created after its durable activation timestamp. Earlier exposure is not adopted.
A previously resting buy can be adopted as it receives new fills after activation.
Zero-sized, fractional, duplicated, delayed, or contradictory data is validated
rather than guessed. Explicit legacy sell orders are not adopted as new buys.

**Order origin is an operator assumption.** These API responses do not prove that
an order was placed by a human instead of another API client. Enabling requires
`MANUAL_EXTERNAL_ORDERS_ARE_MANUAL=true`: an explicit acknowledgement that other API
writers do not create orders in the managed primary-account series. Unknown bot
acknowledgements are resolved before classifying external inventory. Without this
assumption, leave the feature disabled or use physically separate accounts outside
this implementation; do not classify unknown API writers as human orders.

## Profit target and partial fills

The target is **$0.25 total gross profit per original manual buy order**, across
all its fills. It is not 25 cents per contract or a new 25-cent target on each
partial fill. Separate buy orders retain separate targets, even in one market.
Entry costs come from confirmed fills. Realized proceeds and remaining fill
allocations are replayed rather than inferred from account average cost.

For remaining quantity `q`, remaining acquisition cost `c`, confirmed realized
gross profit `r`, and the saved goal `g = 0.25`:

```
required outcome price = (c + max(0, g - r)) / q
```

The price is rounded upward to a valid outcome price using the market's current
YES price grid (NO is mapped through its complementary YES price). Whole-cent
fallback is used only when price ranges are absent; valid sub-cent ranges are
supported. Fractional quantities use the API's two-decimal quantity precision.
A target that cannot be reached below $1 stays unsubmitted and is logged; the
worker never silently lowers the goal. Once the goal has been earned, additional
fills on that same buy order have a break-even floor, not a second profit goal.

Example on a one-cent grid: ten contracts filled at $0.40 cost $4.00. The raw
$0.425 outcome target rounds to $0.43, giving $0.30 gross if all ten sell there.
Fees are not deducted in this target. Gross profit does not guarantee net profit.

## Ownership and recovery safeguards

Manual exits have their own `state_manual_exits_v2.json` journal, separate from
automatic entry budgets and automatic exit receipts. Each exit records the exact
entry fill IDs and quantities it can consume. Confirmed manual exit allocations
are shared read-only with automatic replay so manual sales cannot be reassigned
to bot lots or counted as recycled automatic entry allowance.

A shared in-process lock serializes bot entry execution and exit reconciliation.
Before either exit worker posts in hybrid mode, position, fill history, order
history, and the bot ownership record are checked again. Stale or inconsistent
snapshots pause submissions. Existing primary-account resting orders that could
reduce the position pause conflicting exits; the feature does not cancel user
orders. Account/series/schema mismatches in a saved journal fail closed.

The pending client-order ID and allocations are fsynced before POST, followed by
an atomic file replacement and directory fsync. An ambiguous timeout, 409, or
server error retains the pending intent and blocks resubmission. Recovery is by
matching order identity or a valid saved IOC receipt, never by assuming that a
missing GET result means cancellation. Confirmed fills must converge with the
fill endpoint before any contracts are reused. Definitive rejections and local
rate-limit deferrals can retry after backoff; known zero-fill IOCs pause three
seconds. The existing shared request coordinator remains in use.

A manual sale outside the bot reduces lots using the existing chronological FIFO
replay, updates realized gross profit, and reduces what remains available. Market
close stops new submissions and preserves unresolved acknowledgements for audit;
remaining contracts are not falsely labeled sold.

**Logical separation is not physical custody isolation.** Kalshi nets the account
position. A person or other client can still change it after the final snapshot
and before matching. Reduce-only prevents increasing the total position, but the
exchange does not reserve a particular software owner's lot. Unexpected netting
or inconsistent allocations pause the system for reconciliation. Do not promise
perfect isolation against arbitrary concurrent external trading.

## Configuration and review gates

The committed defaults are:

```dotenv
MANUAL_EXITS_ENABLED=false
MANUAL_EXITS_DRY_RUN=true
MANUAL_EXTERNAL_ORDERS_ARE_MANUAL=false
MANUAL_EXIT_SERIES=KXBTC15M
MANUAL_EXIT_MODE=monitored_reduce_only
```

Startup remains subject to the original `TRADING_ENABLED`, credential, persistent
volume, and process-lock safeguards. Running `python bot.py` directly bypasses
hybrid composition; Docker/Railway configuration in this PR uses
`python hybrid_runtime.py`. No Railway setting was changed through the connector.
Merging a deployment-tracked branch can nevertheless trigger its existing deploy
integration; this PR must not be merged as a substitute for explicit review.

No production flags have been enabled. The candidate should stay draft until the
mode difference and existing test failures are reviewed. A later validation should
start with manual dry-run on an isolated controlled account/series. Dry-run never
submits manual exits but does not turn off the separately enabled automatic bot.
Its first activation timestamp is retained if the same journal is later used in
live mode; do not delete the journal to resolve an unknown acknowledgement.

The exit monitor defaults to a one-second polling interval, but a full pass also
performs reconciliation reads and shares a lock with entry execution. No one- or
two-second fill-to-exit latency is guaranteed or measured by these offline tests.

## Tests and baseline comparison

The targeted suite uses the real API request serializer, ownership replay, exit
monitor, and manual lifecycle with an in-memory exchange. Network access is
blocked in the test runner and no live credentials are provided. It covers YES/NO,
multiple original orders, partial buys/sells, fractional quantities, price grids,
duplicate/conflicting fills, restart recovery, missing ACKs, rate-limit/HTTP
errors, failed persistence, external resting orders, dry-run, close, configuration,
shared-lock composition, and randomized partial-fill sequences.

Local candidate results:

- Targeted hybrid and primitive tests: **102 passed**.
- Full repository: **1,188 passed, 342 failed** (1,530 tests).
- Pre-change baseline: **1,096 passed, 342 failed** (1,438 tests).
- New failed test identities relative to baseline: **0**.

The 342 existing failures are not waived, skipped, or described as a green suite.
This PR intentionally does not rewrite the existing automatic strategy merely to
make archived strategy expectations pass. GitHub Actions runs targeted and full
checks separately and archives results. Offline success is not exchange testing.

To repeat locally without network access during tests:

```bash
python -m pip install -r requirements.txt
python tools/run_offline_tests.py -q test_manual_limit_exits.py test_hybrid_runtime.py test_co_trading.py
python tools/run_offline_tests.py -q --junitxml=hybrid-full-results.xml
```
