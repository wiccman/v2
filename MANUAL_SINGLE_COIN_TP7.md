# Manual single-coin take-profit: +7 cents

Prepared and tested offline; not activated. The existing manual exit engine
already supports a configurable seven-cent increment. This adds a read-only
profile and tests, without changing the running bot, its start command, or
Crypto Leader settings.

For a new manual YES or NO buy, the target is the actual fill price plus $0.07
per contract before fees. A 40c fill targets 47c. Partial fills at different
prices receive their own targets and quantities. Limit and mobile market buys
are supported. The engine submits a resting sell after detecting and reconciling
the fill; it does not wait for the bid to reach the target. Existing sell
coverage and saved order IDs prevent duplicate orders. Targets at or above
$1 are skipped instead of reduced. Tick-size rounding can increase the target.

Scope: BTC, ETH, SOL, XRP and HYPE individual 15-minute markets. Crypto Leader,
WTI, hourly and other series are excluded. The preview profile is TP-only,
with no stop-loss. It covers new fills after its activation cutoff.

## Offline validation

`python tools/run_offline_tests.py -q test_manual_single_coin_tp7.py`

Validation: 136 tests passed across this profile, resting manual exits, mobile
entries and manual order edits, with TCP connections blocked by the test runner.
The credential-free configuration check reported `increment_cents=7.00`, the
five intended series, `dry_run=true` and `automatic_entries=false`.

To validate the profile without credentials, exchange requests or journal writes:

```sh
set -a
. ./manual_single_coin_tp7.preview.env
set +a
python manual_exit_runner.py --check-config
```

## Current deployment and operator handoff

The inspected Railway service starts `python minute8_top2.py --live`, which
does not start the manual exit watcher. Changing only
`MANUAL_EXIT_INCREMENT_CENTS=7` on that service will not implement this request.
The proposed profile has all live gates disabled and does not change the
current service. This is an offline preparation, not a production integration.

Before operator activation, resolve single-coin ownership and worker coexistence.
The current minute-nine journal uses a different ownership schema from the
legacy journals read by `manual_exit_runner.py`. Do not start the manual worker
alongside an automatic single-coin buyer: its orders could otherwise be mistaken
for manual fills. Do not replace the current start command blindly either,
because that would also interrupt the separate Crypto Leader workflow.

Use verified, compatible entry/exit ownership journals; never create empty
journals to bypass existing-account checks. A live journal must be separate
from the preview journal. Existing +3c/+5c journals are intentionally rejected
by the engine when changing to +7c; they need a reviewed migration, not deletion.
No production journal or open order was changed by this preparation.
