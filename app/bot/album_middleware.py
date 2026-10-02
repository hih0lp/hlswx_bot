from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

# Альбом из нескольких фото Telegram присылает отдельными сообщениями с общим
# media_group_id. Бот обрабатывает только первое: иначе каждое фото запускало бы
# сценарий заново (две «Объявление получено», два шага подряд). Остальные
# сообщения альбома молча отбрасываются.
_TTL_SECONDS = 120.0
_PURGE_AT = 2000
_seen: dict[tuple[int, int, str], float] = {}


class AlbumFirstOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        group_id = getattr(event, "media_group_id", None) if isinstance(event, Message) else None
        if not group_id:
            return await handler(event, data)

        now = time.monotonic()
        if len(_seen) > _PURGE_AT:
            for key in [k for k, at in _seen.items() if now - at > _TTL_SECONDS]:
                _seen.pop(key, None)

        bot = data.get("bot")
        key = (bot.id if bot else 0, event.chat.id, str(group_id))
        seen_at = _seen.get(key)
        if seen_at is not None and now - seen_at <= _TTL_SECONDS:
            return None
        # Между проверкой и записью нет await — два сообщения альбома не пройдут оба.
        _seen[key] = now
        return await handler(event, data)
