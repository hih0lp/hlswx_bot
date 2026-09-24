"""Рассылка сообщений пользователям HWLS (нажимали /start)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models.entities import User

logger = logging.getLogger("admin_broadcast")

BROADCAST_DELAY_SEC = 0.05


async def count_users() -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(User)) or 0)


async def broadcast_to_users(
    bot: Bot,
    text: str,
    *,
    delay_sec: float = BROADCAST_DELAY_SEC,
) -> dict[str, int]:
    """Рассылка всем, кто нажимал /start."""
    async with SessionLocal() as session:
        telegram_ids = list(
            await session.scalars(select(User.telegram_id).order_by(User.id)),
        )
    return await send_to_ids(bot, text, telegram_ids, delay_sec=delay_sec)


async def send_to_ids(
    bot: Bot,
    text: str,
    telegram_ids: Sequence[int],
    *,
    delay_sec: float = BROADCAST_DELAY_SEC,
) -> dict[str, int]:
    """Отправка одного текста списку получателей (ТЗ 6.11).

    Раньше получателей выбирала сама рассылка, и «нескольким пользователям»
    из макета упиралось в «всем». Теперь список приходит снаружи: его строит
    либо экран выбора получателей, либо `broadcast_to_users`.
    """
    from app.services.banners import forget_screen

    sent = 0
    blocked = 0
    failed = 0
    for telegram_id in telegram_ids:
        try:
            await bot.send_message(telegram_id, text)
            sent += 1
            # Наше сообщение стало последним в чате получателя: экран его
            # раздела больше не перерисовать, иначе правка уедет не туда.
            forget_screen(telegram_id)
        except TelegramForbiddenError:
            blocked += 1
        except TelegramBadRequest as exc:
            logger.warning("Broadcast bad request to %s: %s", telegram_id, exc)
            failed += 1
        except Exception:
            logger.exception("Broadcast failed for %s", telegram_id)
            failed += 1
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)

    return {
        "total": len(telegram_ids),
        "sent": sent,
        "blocked": blocked,
        "failed": failed,
    }
