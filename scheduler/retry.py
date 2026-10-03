"""
scheduler/retry.py
-------------------
Exponential backoff decorator and retry utilities for upload jobs.
"""

from __future__ import annotations

import asyncio
import functools
import random
from typing import Callable, Type

from core.logging_config import get_logger

log = get_logger(__name__)


async def with_exponential_backoff(
    coro_func: Callable,
    *args,
    max_retries: int = 5,
    base_delay: float = 30.0,  # seconds
    max_delay: float = 3600.0,  # 1 hour max
    jitter: bool = True,
    retriable_exceptions: tuple[Type[Exception], ...] = (Exception,),
    **kwargs,
) -> any:
    """
    Execute an async coroutine with exponential backoff retry logic.

    Delay formula: min(base_delay * 2^attempt + jitter, max_delay)
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except retriable_exceptions as exc:
            last_exc = exc
            exc_str = str(exc)

            # Check if this exception is unrecoverable (daily channel upload limit or daily quota)
            is_fatal = (
                exc.__class__.__name__ in ("UploadLimitExceededError", "QuotaExceededError")
                or "uploadLimitExceeded" in exc_str
                or "quotaExceeded" in exc_str
                or "The user has exceeded the number of videos" in exc_str
            )
            if is_fatal:
                log.warning(
                    "Non-retriable YouTube error detected; aborting retries immediately",
                    func=coro_func.__name__,
                    error=exc_str,
                )
                raise exc

            if attempt == max_retries:
                log.error(
                    "All retries exhausted",
                    func=coro_func.__name__,
                    attempts=attempt + 1,
                    error=str(exc),
                )
                raise

            delay = min(base_delay * (2 ** attempt), max_delay)
            if jitter:
                delay += random.uniform(0, delay * 0.1)

            log.warning(
                "Retrying after failure",
                func=coro_func.__name__,
                attempt=attempt + 1,
                max_retries=max_retries,
                delay=round(delay, 1),
                error=str(exc),
            )
            await asyncio.sleep(delay)

    raise last_exc  # unreachable but satisfies type checkers
