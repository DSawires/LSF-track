"""Login throttling.

In-memory and per-process, which matches how the app is deployed (one uvicorn
process). With scrypt at ~50ms a verification, the throttle is not what makes
brute force expensive -- it is what makes it *loud and slow* from the internet.

Two keys per attempt: the username (stops a distributed guess against one
account) and the client IP (stops one host spraying many accounts). The IP
budget is larger because a factory NATs all its engineers behind one address;
locking the whole floor out because one person fat-fingered a password five
times would be worse than the attack.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

WINDOW_SECONDS = 900
MAX_PER_USERNAME = 5
MAX_PER_IP = 25


class LoginThrottle:
    def __init__(
        self,
        window: int = WINDOW_SECONDS,
        max_per_username: int = MAX_PER_USERNAME,
        max_per_ip: int = MAX_PER_IP,
    ) -> None:
        self.window = window
        self.limits = {"user": max_per_username, "ip": max_per_ip}
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        entries = self._failures[key]
        while entries and now - entries[0] > self.window:
            entries.popleft()
        return entries

    def retry_after(self, username: str, ip: str) -> int:
        """Seconds until another attempt is allowed; 0 means go ahead."""
        now = time.monotonic()
        worst = 0.0
        with self._lock:
            for kind, key in (("user", f"user:{username}"), ("ip", f"ip:{ip}")):
                entries = self._prune(key, now)
                if len(entries) >= self.limits[kind]:
                    worst = max(worst, self.window - (now - entries[0]))
        return int(worst) + 1 if worst else 0

    def record_failure(self, username: str, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._failures[f"user:{username}"].append(now)
            self._failures[f"ip:{ip}"].append(now)

    def record_success(self, username: str) -> None:
        # A real sign-in clears that account's slate. The IP budget is left
        # alone: one guessed password must not reset the spray counter.
        with self._lock:
            self._failures.pop(f"user:{username}", None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


login_throttle = LoginThrottle()
