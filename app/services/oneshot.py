"""Разовые платные посты из Hammer/W → тишина группы (ТЗ 6.6).

Основной бот должен узнавать о выходе разового поста в момент публикации.
Точка входа — вебхук /api/hwls/oneshot-published, который дёргает бот-партнёр;
та же логика доступна администратору вручную, пока интеграция не поднята.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Chat, OneshotEvent
from app.services import settings_store
from app.services.publish_log import log_event
from app.services.queue_slots import defer_chat_jobs, set_silence

logger = logging.getLogger("oneshot")


async def resolve_chat(
    session: AsyncSession,
    *,
    telegram_chat_id: int | None = None,
    telegram_username: str | None = None,
) -> Chat | None:
    if telegram_chat_id:
        chat = await session.scalar(select(Chat).where(Chat.telegram_chat_id == telegram_chat_id))
        if chat:
            return chat
    uname = (telegram_username or "").strip().lstrip("@").lower()
    if uname:
        return await session.scalar(
            select(Chat).where(func.lower(Chat.telegram_username) == uname),
        )
    return None


async def register_oneshot_publication(
    session: AsyncSession,
    *,
    telegram_chat_id: int | None = None,
    telegram_username: str | None = None,
    network: str = "hammer",
    message_id: int | None = None,
    published_at: datetime | None = None,
    minutes: int | None = None,
) -> dict:
    """Зафиксировать выход разового поста и увести группу в тишину на 20 минут.

    Каждый новый разовый пост пересчитывает окно заново от времени ЭТОГО поста,
    даже если группа уже в тишине (ТЗ 6.4). Повторный вызов с тем же message_id
    ничего не двигает — Hammer может ретраить доставку.
    """
    chat = await resolve_chat(
        session, telegram_chat_id=telegram_chat_id, telegram_username=telegram_username,
    )
    if not chat:
        return {"ok": False, "error": "chat_not_found"}

    resolved_chat_id = int(chat.telegram_chat_id or telegram_chat_id or 0)
    published_at = published_at or datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)

    if message_id is not None:
        existing = await session.scalar(
            select(OneshotEvent).where(
                OneshotEvent.network == network,
                OneshotEvent.telegram_chat_id == resolved_chat_id,
                OneshotEvent.message_id == message_id,
            ),
        )
        if existing:
            return {
                "ok": True,
                "duplicate": True,
                "chat_id": chat.id,
                "silence_until": existing.silence_until.isoformat() if existing.silence_until else None,
            }

    silence_until = await set_silence(
        session,
        chat.id,
        published_at=published_at,
        minutes=minutes if minutes is not None else await settings_store.queue_silence_minutes(),
    )
    # Ждущие посты подписчиков и белого списка не отклоняются, а переносятся
    await defer_chat_jobs(session, chat.id, until=silence_until)

    session.add(
        OneshotEvent(
            network=network,
            telegram_chat_id=resolved_chat_id,
            message_id=message_id,
            chat_id=chat.id,
            published_at=published_at,
            silence_until=silence_until,
        ),
    )
    await log_event(
        session,
        "silence_set",
        chat_id=chat.id,
        detail=f"oneshot network={network} message_id={message_id} until={silence_until.isoformat()}",
    )
    await session.commit()

    logger.info(
        "Oneshot post registered chat=%s (%s) network=%s silence_until=%s",
        chat.id,
        chat.telegram_username,
        network,
        silence_until.isoformat(),
    )
    return {
        "ok": True,
        "duplicate": False,
        "chat_id": chat.id,
        "chat_title": chat.title,
        "silence_until": silence_until.isoformat(),
    }
