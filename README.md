# Respect manual edits to Kalshi bot exit orders

STATUS: Tested locally. NOT committed to GitHub, staged in Railway, or deployed.
The connected GitHub code-write action was blocked. The running bot was not changed.

BASE: wiccman/v2 commit 8ce6bb29e16858083ab40b88fdc76d767163c8f1
Current live branch when inspected: codex/manual-stop-20c-20261031

## Behavior
The new policy is opt-in via MANUAL_EXIT_RESPECT_MANUAL_EDITS=true.
Recognize externally edited prices and bounded quantities on the exact saved
exchange order ID, including client_order_id changes. Re-read the order and
verify actual fills before saving the amendment. Preserve immutable market,
side, account, type and exchange order ID checks. No direct financial requests
were made during preparation; tests disable network access and clear credentials.

User price edits remain; quantity reductions are not automatically topped up.
Once a fill's TP has been manually overridden, this policy will not place another
TP for that fill automatically. Its independently configured software stop
continues to monitor the remaining eligible position. New buy fills keep the
+5-cent default TP. The current 20-cent stop setting is not changed by this patch.
A later size increase is accepted only within the order's original allocation.
Unidentified replacement order IDs are NOT automatically adopted. Existing
unconfirmed cancellations are preserved; a patch cannot undo requests already sent.
External, separately placed sells retain the existing stop-blocking limitations.

## Review and apply manually
Use a checkout of the exact base commit above, not an unrelated default branch.
Review the patch before applying. From the repository root:

    git switch -c fix/respect-manual-order-edits 8ce6bb29e16858083ab40b88fdc76d767163c8f1
    git apply --check /path/to/kalshi_respect_manual_edits.patch
    git apply /path/to/kalshi_respect_manual_edits.patch
    python tools/run_offline_tests.py -q test_manual_order_edits.py

The ZIP also includes the complete replacement files under changed_files/.
Do not place a test trade merely to validate code. Review and approve any live
deployment separately. Set MANUAL_EXIT_RESPECT_MANUAL_EDITS=true only when
activating this code. Keep the existing stop-loss, budgets, scope and journals.
After activation, check MANUAL_ORDER_EDIT_ADOPTED and the protection report's
actual tp_target/tp_manual_override, while checking that the software SL is active.
A software stop is not exchange-hosted and does not guarantee a maximum loss.

## Offline results
354 targeted tests passed, including 39 new manual-edit regression cases and
the existing 12 stop-policy migration cases. All previous tests remain enabled.
Full baseline: 1425 passed, 422 failed.
Full patch: 1464 passed, 422 failed.
Exact failing (classname, test name) sets: identical; zero added/removed failures.
The full repository suite is NOT green, and this patch is NOT live-verified.
