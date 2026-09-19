"""Small thread-safe circuit breaker used by remote dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import threading
import time
from typing import Callable


CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitSnapshot:
    state: str
    consecutive_failures: int
    failure_threshold: int
    cooldown_seconds: float
    half_open_in_flight: int
    half_open_max_calls: int
    retry_after_seconds: float

    def to_dict(self) -> dict:
        return asdict(self)


class CircuitBreaker:
    """Closed/open/half-open circuit breaker with bounded probe traffic."""

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        half_open_max_calls: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if int(failure_threshold) < 1:
            raise ValueError("failure_threshold must be at least 1")
        if float(cooldown_seconds) < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        if int(half_open_max_calls) < 1:
            raise ValueError("half_open_max_calls must be at least 1")
        self.failure_threshold = int(failure_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self.half_open_max_calls = int(half_open_max_calls)
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0

    def allow_request(self) -> bool:
        """Admit a normal request or one bounded half-open probe."""

        with self._lock:
            now = self._clock()
            if self._state == OPEN:
                if now - self._opened_at < self.cooldown_seconds:
                    return False
                self._state = HALF_OPEN
                self._half_open_in_flight = 0
            if self._state == HALF_OPEN:
                if self._half_open_in_flight >= self.half_open_max_calls:
                    return False
                self._half_open_in_flight += 1
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = CLOSED
            self._consecutive_failures = 0
            self._opened_at = 0.0
            self._half_open_in_flight = 0

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            if self._state == HALF_OPEN:
                self._state = OPEN
                self._opened_at = now
                self._consecutive_failures = self.failure_threshold
                self._half_open_in_flight = 0
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = OPEN
                self._opened_at = now
                self._half_open_in_flight = 0

    def snapshot(self) -> CircuitSnapshot:
        with self._lock:
            retry_after = 0.0
            if self._state == OPEN:
                elapsed = self._clock() - self._opened_at
                retry_after = max(0.0, self.cooldown_seconds - elapsed)
            return CircuitSnapshot(
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                failure_threshold=self.failure_threshold,
                cooldown_seconds=self.cooldown_seconds,
                half_open_in_flight=self._half_open_in_flight,
                half_open_max_calls=self.half_open_max_calls,
                retry_after_seconds=retry_after,
            )
