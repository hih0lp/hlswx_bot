from __future__ import annotations

import re
from decimal import Decimal

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.texts import PROFILE_TEXT
from app.models.entities import User


class InsufficientBalanceError(Exception):
    pass


def format_rub(amount: Decimal | int | float) -> str:
    value = int(Decimal(str(amount)))
    return f"{value:,}".replace(",", " ")


def parse_amount_text(raw: str | None) -> int | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d]", "", raw.strip())
    if not cleaned:
        return None
    try:
        value = int(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


async def get_user_for_update(session: AsyncSession, user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.id == user_id).with_for_update())


def build_profile_text(tg_user: TgUser, user: User) -> str:
    """Фрейм «Профиль» из макета."""
    return PROFILE_TEXT.format(
        name=tg_user.full_name or "—",
        telegram_id=user.telegram_id,
        username=f"@{user.username}" if user.username else "не указан",
        balance=format_rub(user.balance or Decimal("0")),
    )
