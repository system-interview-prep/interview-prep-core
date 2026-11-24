from collections import defaultdict, deque
from threading import Lock
from time import monotonic


class LoginRateLimiter:
    def __init__(self, max_failures: int = 5, window_seconds: float = 60.0) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._active_attempts(key)) >= self.max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._active_attempts(key).append(monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()

    def _active_attempts(self, key: str) -> deque[float]:
        attempts = self._failures[key]
        cutoff = monotonic() - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        return attempts


login_rate_limiter = LoginRateLimiter()
