"""Bounded retry-with-backoff on `QUOTA_EXHAUSTED`, shared by every live-model
eval runner (`baseline_eval.py`, `got_eval.py`). Enough to ride out a
transient per-minute throttle on a handful of consultations, not a multi-day
pause across a daily quota reset — see `baseline_eval`'s module docstring for
why the harder resumability requirement is left as separate, larger work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from coda_worker_sdk.errors import QuotaExhaustedError, RetryableError

logger = logging.getLogger(__name__)

QUOTA_RETRY_DELAYS_S = (30.0, 60.0, 120.0)
"""Fallback schedule, used only when the provider doesn't tell us how long
to wait."""

_RETRY_AFTER_BUFFER_S = 2.0
"""Added on top of a provider-supplied Retry-After, since a wait timed to
land exactly on the reset boundary routinely still 429s once."""

_RETRY_AFTER_CAP_S = 150.0
"""Found live 2026-09-05: one 429 on a large request reported
`Retry-After: 2661s` (~44 minutes) while the account's actual token bucket
was, verified moments later via a direct request, already healthy
(7915/8000 tokens remaining, resetting in under a second). Every other
observed Retry-After this session was under 130s — the outlier looks like
Groq computing a worst-case refill time for the specific (large) rejected
request rather than the general per-minute window, not a real multi-hour
block. Honoring it verbatim would have blocked this run for 44 minutes for
no reason; capping at the largest window actually observed, and falling back
to the fixed schedule beyond that, trades a small risk of one extra 429
against a large guaranteed waste."""

NETWORK_RETRY_DELAYS_S = (15.0, 30.0, 60.0)
"""For `RetryableError` (network/5xx failures `GroqLLMClient.complete()`
already retried 3 times internally before giving up — found live 2026-09-05,
~35 minutes into the full 8-item `run-got-eval` run: a "Temporary failure in
name resolution" outpaced that internal retry's ~7s total backoff and
propagated all the way up, killing the whole multi-hour run with 7 of 8
items unprocessed. This schedule is a courser outer safety net for exactly
that: a real but transient outage lasting longer than a few seconds, not the
per-minute quota window `QUOTA_RETRY_DELAYS_S` is tuned for."""

_T = TypeVar("_T")


async def complete_with_quota_retry(
    coro_factory: Callable[[], Awaitable[_T]], *, description: str
) -> _T:
    """Retries a QUOTA_EXHAUSTED or transient-network failure a bounded
    number of times, re-raising if still failing after the last attempt of
    each kind. `coro_factory` is a zero-arg callable returning a fresh
    coroutine each call (a coroutine object can only be awaited once).

    Found live 2026-09-05 (`coda-eval run-got-eval`'s first real run, which
    makes far more Groq calls per consultation than the baseline arm's two):
    the fixed (30, 60, 120)s schedule this originally shipped with (still the
    fallback below) guessed blind at the provider's per-minute reset window
    and was too short — a retry fired exactly at 30s still 429'd. Groq's 429
    response carries a `Retry-After` header
    (`GroqLLMClient` already surfaces it as `QuotaExhaustedError.
    retry_after_seconds`); honoring it instead of guessing is both more
    correct and, when the real window is shorter than 30s, faster.
    """
    last_exc: Exception | None = None
    delay = 0.0
    quota_attempts_left = 1 + len(QUOTA_RETRY_DELAYS_S)
    network_attempts_left = 1 + len(NETWORK_RETRY_DELAYS_S)
    quota_attempt = network_attempt = 0
    while quota_attempts_left > 0 and network_attempts_left > 0:
        if delay:
            logger.warning("retrying %s in %.0fs (%s)", description, delay, last_exc)
            await asyncio.sleep(delay)
        try:
            return await coro_factory()
        except QuotaExhaustedError as exc:
            last_exc = exc
            quota_attempts_left -= 1
            if exc.retry_after_seconds is not None and exc.retry_after_seconds <= _RETRY_AFTER_CAP_S:
                delay = exc.retry_after_seconds + _RETRY_AFTER_BUFFER_S
            else:
                delay = QUOTA_RETRY_DELAYS_S[min(quota_attempt, len(QUOTA_RETRY_DELAYS_S) - 1)]
            quota_attempt += 1
            continue
        except RetryableError as exc:
            last_exc = exc
            network_attempts_left -= 1
            delay = NETWORK_RETRY_DELAYS_S[min(network_attempt, len(NETWORK_RETRY_DELAYS_S) - 1)]
            network_attempt += 1
            continue
    assert last_exc is not None
    raise last_exc


__all__ = ["NETWORK_RETRY_DELAYS_S", "QUOTA_RETRY_DELAYS_S", "complete_with_quota_retry"]
