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
