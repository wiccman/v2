# Buy Rule Version Log

This file is the reference point for retired and active entry behavior. Retired rules belong here instead of remaining executable in bot.py.

## 2026-10-01 — Two-rule cleanup baseline

### Active
1. **Final 3-minute settlement buy** — existing settlement route; intentionally preserved unchanged. Uses the live strike side and the configured settlement trigger/limit/budget.
2. **±$100 strike directional buy** — requested next rule: when BTC is at least $100 above strike, buy YES; when at least $100 below strike, buy NO; exit for a $0.25 profit gain. Exact operating window/budget still to be finalized before implementation.

### Retired from executable path
- Regular live-strike price ladder (45c, 48c, 51c, 53c, 56c, 59c, 62c, 64c [removed earlier], 67c, 70c, 75c and associated targets).
- Opening-bias 52c route.
- Opening 57c route.
- Late-bias price-pair route.
- Dual-limit buy route.
- Historical-strike entry route.
- Spot-trigger entry route.
- Older opposite-strike/opening variants and legacy 35c/38c/39c entry references.

### Notes
The retired rules above are retained only as historical reference. They must not be re-enabled merely because stale Railway environment variables still exist.

## 2026-10-02 — Stable pre-fill-change snapshot

- Git commit before settlement fill adjustment: `12b73148539e6da768d69a42a4ec4ea383cebc1f` / stable deployed lineage.
- Final 3-minute settlement route: 96c-or-higher trigger, 96c resting limit, 11-contract max, $20 settlement budget.
- This snapshot is the rollback/reference build before changing fill behavior.

## 2026-10-02 — Exact-96 fill behavior

- Preserve the same final 3-minute window, live-strike direction, 11-contract cap, $20 settlement budget, and hold-to-settlement behavior.
- Change the settlement trigger to **live ask exactly 96c** and submit the existing **96c limit** at that moment. This avoids triggering at 97–99c and leaving a 96c order behind the market.

## 2026-10-02 — Final 2-minute settlement window

- Settlement buy window changed from the final 3 minutes (180 seconds) to the final 2 minutes (120 seconds).
- Settlement entry price/direction, contract cap, budget, and other execution behavior are otherwise unchanged.

## 2026-10-02 — Manual flip-selling protection (PR #77, not deployed)

The user enabled flip selling for manual trades and requested bot-specific protections, without changing the manual setting.

- Added `manual_trade_guard.py`, version `manual-priority-v1`, and integrated it into the two-rule runner.
- Bot exits retain the existing explicit `reduce_only=True` and immediate-or-cancel wire parameters. No ordinary sell or side-switch fallback was introduced.
- Fresh bot-fill ownership, account-position and open-order checks precede buys and sells. An unresolved exit cannot fund a second order against the same inventory.
- A newly observed untracked/manual fill in a watched market, or incompatible bot/account ownership, persists a market-local manual-control pause until that contract closes. Both bot entries AND take-profit management stop for that market; the user must manage any remaining position. Restarting does not clear the pause. Other market records are not paused.
- Pre-existing manual same-side holdings are excluded from bot exit sizing. Close-and-rebuy at the same net quantity is detected from fill identities, not quantity alone.
- Untracked open orders cause a temporary wait and are left untouched. An order canceled without filling can clear that wait. A confirmed new manual fill instead latches the pause.
- On a pause or reconciliation failure, request cancellation only for bot-owned pending entries whose ticker, order ID and client ID are verified. Cancellations are retried and never reported as confirmed solely from the cancellation request; allowances are not reset.
- The two requested strategy thresholds, entry sizes, profit goals and shared $20 market budget were not changed. `two_rule_policy.py` remains blob `238bdb4f49200ce7d9fe7c2402ad2290b027b9a3`.
- Runtime change: `fb868c3c28d8e7c5c627a44617607480ddfe84f8`; new guard tests committed in `f806138207a426ada2939f871d92f33822949a25`.
- Local isolated verification: 146 tests passed (123 original offline cases from the earlier package plus 23 new manual-guard cases); Python compilation passed. Network was blocked in test fixtures. The two adapter methods were exercised with a recording transport, not a real exchange. Runner, guard and new test Git blob hashes match the tested local files.
- New tests are included as `test_manual_trade_guard.py`. The older `test_two_rules.py` is still not published in this PR. This is not a full-repository CI result or production verification.
- Limits: polling cannot atomically coordinate with simultaneous manual orders. An order already dispatched can race a manual trade, and cancel requests can fail or arrive after a fill. Reduce-only is the exchange-level defense against exit reversals, not a guarantee of perfect manual/bot lot isolation. Legacy cutover positions retain their previous monitor; this new guard applies to the two-rule runner's markets.
- No merge, deployment, live order, account-setting change, state reset or `TRADING_ENABLED` change was performed while adding these protections.
