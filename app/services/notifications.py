from __future__ import annotations

import logging
from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.bot.runtime import get_bot
from app.core.texts import INTEGRATION_API, SUB_ACTIVATED
from app.db.session import SessionLocal
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import PackageOrder, Subscription, User
from app.services import access, banners
from app.services.pricing import PLAN_CORP_B2B
from app.services.wallet import format_rub

logger = logging.getLogger("notifications")


async def _subscription_activated_text(session, user_id: int, sub: Subscription | None) -> str:
    """Фрейм «🎉 Подписка активирована».

    Персональный API выдаём только по корпоративной подписке — так в макете
    («API выдаётся только после оплаты активной корпоративной подписки»).
    """
    from app.services.subscription_info import subscription_summary

    if not sub:
        return SUB_ACTIVATED.format(city="—", chats="—", volume="—")

    if (sub.plan_type or "") == PLAN_CORP_B2B:
        from app.services.b2b import (
            build_webhook_url,
            ensure_integration_for_subscription,
        )

        integration = await ensure_integration_for_subscription(session, user_id, sub.id)
        return INTEGRATION_API.format(url=build_webhook_url(integration.token))

    return SUB_ACTIVATED.format(**await subscription_summary(session, sub))


async def notify_payment_success(
    user_id: int,
    result: str,
    purpose_id: int,
    amount: Decimal | int | None = None,
) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        if not user:
            return

        extra_kb = None
        # Баннер раздела, к которому относится уведомление: экраны оплаты
        # в макете живут внутри своих разделов и баннер наследуют (ТЗ 6.8).
        banner_key = banners.SUBSCRIPTION
        if result in ("subscription_activated", "subscription_whitelist"):
            sub = await session.scalar(select(Subscription).where(Subscription.id == purpose_id))
            text = await _subscription_activated_text(session, user_id, sub)
            if sub and (sub.plan_type or "") == PLAN_CORP_B2B:
                # Корпоративная подписка отдаёт экран «Интеграция бота» с API.
                banner_key = banners.INTEGRATION
        elif result == "package_queued":
            order = await session.scalar(select(PackageOrder).where(PackageOrder.id == purpose_id))
            text = (
                "<b>✅ Оплата получена</b>\n\n"
                f"📦 Пакет <b>#{purpose_id}</b> оплачен\n"
                f"💰 Сумма: <b>{order.price:,} ₽</b>\n\n"
                "Публикация во все чаты города уже в очереди."
                if order
                else f"📦 Пакет <b>#{purpose_id}</b> оплачен. Публикация в очереди."
            )
            banner_key = banners.PUBLICATION
        elif result == "package_needs_text":
            banner_key = banners.PUBLICATION
            text = (
                "<b>✅ Оплата получена</b>\n\n"
                f"📦 Пакет <b>#{purpose_id}</b> оплачен.\n\n"
                "Отправьте текст объявления для публикации во все чаты:"
            )
            extra_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📝 Прислать текст",
                            callback_data=f"pkg:text:{purpose_id}",
                            style=STYLE_MAIN,
                        ),
                    ],
                ],
            )
        elif result == "balance_topped_up":
            banner_key = banners.TOPUP
            balance = user.balance or 0
            credited = format_rub(amount) if amount is not None else "—"
            text = (
                "<b>✅ Баланс пополнен</b>\n\n"
                f"💰 Зачислено: <b>{credited} ₽</b>\n"
                f"💳 Текущий баланс: <b>{format_rub(balance)} ₽</b>\n\n"
                "Средства можно использовать при оплате подписки."
            )
        else:
            return

    bot = get_bot()
    try:
        await banners.send_banner_to(bot, user.telegram_id, banner_key, text, extra_kb)
    except Exception:
        logger.exception("Failed to notify user %s about payment", user_id)
    finally:
        # Уведомление — не экран раздела: обновлять его на месте нельзя,
        # следующий раздел уйдёт новым сообщением.
        banners.forget_screen(user.telegram_id)


async def notify_admins_publish_failed(publication_id: int, chat_id: int, error: str) -> None:
    """Попытки публикации исчерпаны — зовём администратора (ТЗ 6.7).

    Оборванный ответ разбирается отдельно: раньше про живой пост в группе
    админу уходило «публикация отменена», и он шёл чинить то, что работает.
    """
    from app.models.entities import Chat
    from app.services.publish_errors import human_reason, is_uncertain

    async with SessionLocal() as session:
        chat = await session.scalar(select(Chat).where(Chat.id == chat_id))
        chat_title = chat.title if chat else f"чат #{chat_id}"
        chat_username = f" (@{chat.telegram_username})" if chat and chat.telegram_username else ""

    if is_uncertain(error):
        text = (
            "<b>❓ Публикация без подтверждения</b>\n"
            "╭──────────────────────╮\n"
            f"📬 Группа: <b>{chat_title}</b>{chat_username}\n"
            f"📋 Публикация: <b>#{publication_id}</b>\n"
            f"⏱ Причина: {human_reason(error)}\n\n"
            "Повтор не отправлен, чтобы не задвоить пост. "
            "Загляните в группу: если объявления там нет, опубликуйте вручную."
        )
    else:
        text = (
            "<b>⚠️ Публикация не удалась</b>\n"
            "╭──────────────────────╮\n"
            f"📬 Группа: <b>{chat_title}</b>{chat_username}\n"
            f"📋 Публикация: <b>#{publication_id}</b>\n"
            f"❌ Причина: {human_reason(error)}\n\n"
            "Публикация в эту группу отменена, остальные группы не затронуты."
        )

    bot = get_bot()
    for admin_id in await access.staff_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Failed to notify admin %s about publish failure", admin_id)
