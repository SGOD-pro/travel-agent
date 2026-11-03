"""Small per-process rate limiter for public API submission routes."""

from __future__ import annotations

import time
from collections import OrderedDict, deque

from fastapi import HTTPException, Request, status

from config.config import settings


class PublicRequestRateLimiter:
    """Bounds request bursts per direct client IP within one API process."""

    def __init__(self) -> None:
        self._requests: OrderedDict[str, deque[float]] = OrderedDict()

    async def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - settings.PUBLIC_RATE_LIMIT_WINDOW_SECONDS
        recent = self._requests.get(client_ip)
        if recent is None:
            if len(self._requests) >= 10_000:
                self._requests.popitem(last=False)
            recent = deque()
            self._requests[client_ip] = recent
        else:
            self._requests.move_to_end(client_ip)
        while recent and recent[0] <= cutoff:
            recent.popleft()
        if len(recent) >= settings.PUBLIC_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests; retry later.",
                headers={"Retry-After": str(settings.PUBLIC_RATE_LIMIT_WINDOW_SECONDS)},
            )
        recent.append(now)


public_request_rate_limiter = PublicRequestRateLimiter()
