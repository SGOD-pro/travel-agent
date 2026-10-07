"""Bounded retry helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger("travel.utils.retries")

async def with_retry[T](
    operation: Callable[[], Coroutine[Any, Any, T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    exponential_base: float = 2.0,
) -> T:
    """Execute an async operation with bounded exponential backoff retries."""
    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as e:
            attempt += 1
            if attempt > max_retries:
                logger.error("Operation failed after %d retries: %s", max_retries, e)
                raise
            delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)
            logger.warning("Attempt %d failed (%s). Retrying in %.2fs...", attempt, e, delay)
            await asyncio.sleep(delay)
