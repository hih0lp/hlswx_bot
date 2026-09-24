"""Напоминание об окончании подписки и уведомление о её завершении.

Заказчик в постановке от 11.09.2026: «Добавлено напоминание об окончании
подписки за 24 часа, надо реализовать». Экран «⚠️ Подписка закончилась»
(фрейм макета `144:211`, баннер ВНИМАНИЕ) в боте тоже не отправлялся — текст
`SUB_EXPIRED` лежал готовым, но некому было его послать: фоновой проверки
подписок в боте не было.

Обе отправки идемпотентны: у подписки есть отметки `reminded_at` и
`expired_notified_at`, и воркер берёт только те строки, где отметка пуста.
Поэтому перезапуск бота, второй тик или подвисшая отправка не приводят
к повторному сообщению пользователю.

Воркер вызывает `process_subscription_reminders()` раз в несколько минут —
точность до минуты здесь не нужна, речь о суточном горизонте.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.core.texts import (
    BTN_HOME,
    BTN_SUB_INFO,
    BTN_SUB_RENEW,
    SUB_EXPIRED,
    SUB_EXPIRING_SOON,
)
from app.db.session import SessionLocal
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import Subscription, SubscriptionStatus, User
from app.services import banners
from app.services.subscription_info import subscription_chats
from app.services.textfmt import chats_label, format_date

logger = logging.getLogger("subscription_reminders")

# За сколько до конца подписки предупреждаем.
REMIND_BEFORE = timedelta(hours=24)


def _keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """Кнопки фрейма `144:211`.

    В макете вторая кнопка — «🔄 Продлить», но механики продления в боте нет
    (см. «Из макета, но за рамками этапа» в docs/figma_mapping.md), поэтому
    ведём на оформление новой подписки.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_SUB_RENEW, callback_data="menu:sub", style=STYLE_MAIN)],
            [
                InlineKeyboardButton(
                    text=BTN_SUB_INFO,
                    callback_data=f"sub:card:{subscription_id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home", style=STYLE_PLAIN)],
        ],
    )


async def _send(telegram_id: int, text: str, subscription_id: int) -> bool:
    """Отправляет экран на баннере ВНИМАНИЕ. False — пользователь недоступен.

    Заблокировавший бота пользователь всё равно помечается уведомлённым:
    иначе воркер будет пытаться достучаться до него на каждом тике.
    """
    from app.bot.runtime import get_bot

    bot = get_bot()
    try:
        await banners.send_banner_to(bot, telegram_id, banners.ATTENTION, text, _keyboard(subscription_id))
    except TelegramForbiddenError:
        logger.info("Подписка %s: бот заблокирован пользователем %s", subscription_id, telegram_id)
        return False
    except Exception:
        logger.exception("Подписка %s: не удалось отправить уведомление", subscription_id)
        return False
    finally:
        # После постороннего сообщения экран раздела уже не последний в чате,
        # и редактировать его нельзя — следующий раздел уйдёт новым сообщением.
        banners.forget_screen(telegram_id)
    return True


async def process_subscription_reminders(now: datetime | None = None) -> dict[str, int]:
    """Один проход: напоминания за сутки и уведомления об окончании.

    Возвращает счётчики — их удобно проверять в тестах и логах.
    """
    now = now or datetime.now(UTC)
    sent = {"reminded": 0, "expired": 0}

    async with SessionLocal() as session:
        # 1. Заканчивается в ближайшие сутки — напоминаем один раз.
        soon = (
            await session.scalars(
                select(Subscription).where(
                    Subscription.status == SubscriptionStatus.active,
                    Subscription.reminded_at.is_(None),
                    Subscription.expires_at.is_not(None),
                    Subscription.expires_at > now,
                    Subscription.expires_at <= now + REMIND_BEFORE,
                ),
            )
        ).all()
        for sub in soon:
            text = await _format(session, sub, SUB_EXPIRING_SOON)
            telegram_id = await _telegram_id(session, sub.user_id)
            if telegram_id:
                if await _send(telegram_id, text, sub.id):
                    sent["reminded"] += 1
            sub.reminded_at = now
        await session.commit()

        # 2. Срок вышел — переводим в expired и показываем экран из макета.
        over = (
            await session.scalars(
                select(Subscription).where(
                    Subscription.status == SubscriptionStatus.active,
                    Subscription.expires_at.is_not(None),
                    Subscription.expires_at <= now,
                ),
            )
        ).all()
        for sub in over:
            text = await _format(session, sub, SUB_EXPIRED)
            telegram_id = await _telegram_id(session, sub.user_id)
            already_notified = sub.expired_notified_at is not None
            if telegram_id and not already_notified:
                if await _send(telegram_id, text, sub.id):
                    sent["expired"] += 1
            sub.status = SubscriptionStatus.expired
            sub.expired_notified_at = sub.expired_notified_at or now
        await session.commit()

    return sent


async def _format(session, sub: Subscription, template: str) -> str:
    city_label, chats = await subscription_chats(session, sub.id)
    return template.format(
        city=city_label,
        chats=chats_label(len(chats)),
        date=format_date(sub.expires_at),
    )


async def _telegram_id(session, user_id: int) -> int | None:
    user = await session.scalar(select(User).where(User.id == user_id))
    return user.telegram_id if user else None
