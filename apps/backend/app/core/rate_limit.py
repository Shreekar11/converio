"""In-process sliding-window rate limiter.

Single-process, in-memory limiter sufficient for the proof-of-work demo.
The contract is intentionally narrow: keyed by an opaque string (e.g.
`company_id`) and parameterized by a max count + window seconds. The bucket
is a `deque` of monotonic timestamps; on each call we evict timestamps
older than the window before deciding whether the request fits.

Production limitations (NOT addressed here — flagged for the F-phase
follow-up):
- State is per-process. Multiple uvicorn workers / pods will each enforce
  the limit independently, so the effective ceiling is `_MAX * worker_count`.
- State resets on process restart.
- No distributed coordination. A Redis-backed limiter (e.g. `slowapi` with
  Redis storage, or a token-bucket Lua script) is the production path.

Why a custom limiter rather than pulling in `slowapi`:
- `slowapi` adds a dependency + middleware wiring for one endpoint. The
  rest of the surface is unauthenticated externally (operators only) or
  upload-bounded (resume) and does not currently need rate limiting.
- The interface here is trivially swappable for `slowapi` later — the
  endpoint depends on a `check(key) -> bool` contract, not the
  implementation.
"""
from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from time import monotonic


class SlidingWindowRateLimiter:
    """Sliding-window rate limiter keyed by a caller-supplied string.

    Thread-safe via a single mutex around bucket mutation. FastAPI runs
    request handlers concurrently in the same event loop, but uvicorn can
    also dispatch to a thread pool for sync deps; the lock keeps the
    invariant simple under either model.
    """

    def __init__(self, *, max_requests: int, window_seconds: int) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    @property
    def max_requests(self) -> int:
        return self._max_requests

    def check(self, key: str) -> bool:
        """Return `True` if the request should proceed; `False` if rate-limited.

        On `True`, a timestamp is recorded against the key's bucket so the
        request counts toward the window. On `False`, no timestamp is
        recorded — clients hitting the limit do not extend their own
        cooldown.
        """
        now = monotonic()
        cutoff = now - self._window_seconds
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._max_requests:
                return False
            bucket.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        """Clear bucket state. Test-only seam — not invoked from request paths."""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


# Module-level singleton for the `/jobs/intake` endpoint. 10 requests /
# hour / company_id per the design doc (Q4 in `docs/plans/job_intake_plan.md`).
job_intake_rate_limiter = SlidingWindowRateLimiter(
    max_requests=10,
    window_seconds=3600,
)


# Keyed by Supabase `sub` (user ID). Prevents repeated signup attempts
# from the same identity. Applied to /auth/company/signup and
# /auth/recruiter/signup endpoints.
signup_rate_limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=3600)


__all__ = [
    "SlidingWindowRateLimiter",
    "job_intake_rate_limiter",
    "signup_rate_limiter",
]
