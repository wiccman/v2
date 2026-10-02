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
