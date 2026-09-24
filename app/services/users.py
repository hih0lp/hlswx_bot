from __future__ import annotations

from datetime import UTC, datetime

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import User
from app.services.whitelist import ensure_whitelist_subscription


async def get_or_create_user(session: AsyncSession, tg_user: TgUser) -> User:
    """Пользователь по данным Telegram; попутно отмечает момент обращения.

    Отметка живёт здесь, а не в отдельном middleware: эту функцию зовут на
    каждом действии пользователя, так что другого такого же общего места нет.
    """
    row = await session.scalar(select(User).where(User.telegram_id == tg_user.id))
    if row:
        row.username = tg_user.username
        row.full_name = tg_user.full_name or ""
        row.last_seen_at = datetime.now(UTC)
        await session.commit()
        await ensure_whitelist_subscription(session, tg_user.id, tg_user.username)
        return row
    row = User(
        telegram_id=tg_user.id,
        username=tg_user.username,
        full_name=tg_user.full_name or "",
        last_seen_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await ensure_whitelist_subscription(session, tg_user.id, tg_user.username)
    return row
