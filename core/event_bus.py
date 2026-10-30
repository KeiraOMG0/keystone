from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event_type: str, **kwargs) -> None:
        for handler in self._handlers.get(event_type, []):
            try:
                await handler(**kwargs)
            except Exception:
                log.exception("EventBus handler error for event '%s'", event_type)
