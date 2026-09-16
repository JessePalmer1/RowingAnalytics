import threading
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int, clock=time.monotonic, sleep=time.sleep):
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                self._sleep((1 - self.tokens) / self.rate)
