"""Account-wide 429 backoff shared by otherwise independent HTTP workers."""
import math
import threading
import time
from email.utils import parsedate_to_datetime


def retry_delay(value, fallback, now):
    """Accept delay seconds or an HTTP date; invalid headers use the fallback."""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError, AttributeError):
            return fallback
    return max(0.0, delay) if math.isfinite(delay) else fallback


class RequestDeferred(RuntimeError):
    """Raised before HTTP dispatch; unlike a timeout, no order was sent."""
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("Shared API cooldown; request not sent")


class RequestCoordinator:
    # Exits get the first recovery window; diagnostics resume last. These are
    # additional delays after a 429, not locks held during network requests.
    PRIORITY_DELAY = {"exit": 0.0, "entry": 1.0, "diagnostics": 2.0}

    def __init__(self, clock=None, wall_clock=None):
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time
        self._lock = threading.Lock()
        self._resume_at = None
        self._last_limit = None
        self._strikes = 0

    def check(self, role):
        with self._lock:
            if self._resume_at is not None:
                remaining = self._resume_at + self.PRIORITY_DELAY[role] - self.clock()
                if remaining > 0:
                    raise RequestDeferred(remaining)

    def limited(self, retry_after=None):
        with self._lock:
            now = self.clock()
            # Successful requests in another worker do not erase an active
            # throttle. Only a quiet minute resets the exponential fallback.
            if self._last_limit is None or now - self._last_limit >= 60:
                self._strikes = 0
            self._strikes = min(self._strikes + 1, 5)
            fallback = min(30.0, 2.0 ** self._strikes)
            delay = max(fallback, retry_delay(retry_after, fallback, self.wall_clock()))
            self._resume_at = max(self._resume_at or now, now + delay)
            self._last_limit = now
            return self._resume_at - now
