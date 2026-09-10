"""Route monitor sends to an optional secondary Telegram bot."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import re
from typing import Any, Iterator

_active_route: ContextVar[str] = ContextVar("active_bot_route", default="primary")


def x_username_from_feed(feed_link: str | None) -> str | None:
    match = re.search(r"/twitter/user/([A-Za-z0-9_]{1,15})(?:/|$)", feed_link or "")
    return match.group(1).lower() if match else None


def is_secondary_feed(feed_link: str | None, secondary_feeds: set[str]) -> bool:
    key = (feed_link or '').strip().lower()
    username = x_username_from_feed(feed_link)
    return '*' in secondary_feeds or key in secondary_feeds or (username and username in secondary_feeds)


@contextmanager
def route_for_feed(feed_link: str | None, secondary_feeds: set[str]) -> Iterator[None]:
    token = _active_route.set("secondary" if is_secondary_feed(feed_link, secondary_feeds) else "primary")
    try:
        yield
    finally:
        _active_route.reset(token)


class BotRouter:
    """Attribute proxy that keeps existing env.bot call sites unchanged."""

    def __init__(self, primary: Any, secondary: Any = None):
        self.primary = primary
        self.secondary = secondary

    def _target(self) -> Any:
        return self.secondary if _active_route.get() == "secondary" and self.secondary else self.primary

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._target()(*args, **kwargs)

    async def get_input_entity(self, entity: Any) -> Any:
        target = self._target()
        try:
            return await target.get_input_entity(entity)
        except ValueError:
            if target is self.secondary:
                # A secondary bot may not have received the user's /start update
                # yet; the access hash from the primary session is still valid.
                return await self.primary.get_input_entity(entity)
            raise

    async def send_message(self, entity: Any, *args: Any, **kwargs: Any) -> Any:
        target = self._target()
        if target is self.secondary and isinstance(entity, int):
            entity = await self.get_input_entity(entity)
        return await target.send_message(entity, *args, **kwargs)
