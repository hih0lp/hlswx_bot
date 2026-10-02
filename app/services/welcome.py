"""Главное меню и вход/выход из пошаговых флоу (макет Figma, ТЗ 6.9)."""

import logging

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, CallbackQuery, ReplyKeyboardMarkup
from sqlalchemy import func, select

from app.core.texts import (
    BOT_COMMANDS,
    BOT_DESCRIPTION,
    BOT_SHORT_DESCRIPTION,
    HOME_ACTIVE,
    HOME_NEW,
    HOME_STATUS_EXPIRED_MANY,
    HOME_STATUS_EXPIRED_ONE,
    HOME_STATUS_NONE,
)
from app.keyboards.main import reply_main_keyboard
from app.services import access, banners
from app.services.textfmt import plural

logger = logging.getLogger("welcome")


async def setup_bot_ui(bot: Bot) -> None:
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    await bot.set_my_description(BOT_DESCRIPTION)
    await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)


async def _subscription_counts(telegram_id: int | None) -> tuple[int, int]:
    """(активных, завершившихся) подписок пользователя."""
    if not telegram_id:
        return 0, 0
    from datetime import UTC, datetime

    from app.db.session import SessionLocal
    from app.models.entities import Subscription, SubscriptionStatus, User

    now = datetime.now(UTC)
    try:
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if not user:
                return 0, 0
            active = await session.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.user_id == user.id,
                    Subscription.status == SubscriptionStatus.active,
                    Subscription.deleted_at.is_(None),
                    Subscription.expires_at > now,
                ),
            )
            finished = await session.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.user_id == user.id,
                    Subscription.status.in_(
                        (SubscriptionStatus.active, SubscriptionStatus.expired),
                    ),
                    Subscription.deleted_at.is_(None),
                    Subscription.expires_at <= now,
                ),
            )
            return int(active or 0), int(finished or 0)
    except Exception:
        logger.exception("Не удалось посчитать подписки пользователя %s", telegram_id)
        return 0, 0


def subscriptions_status_text(active: int, finished: int) -> str:
    """Строка состояния подписок — общая для главного меню и «Мои подписки».

    Начертание по макету: первая строка блока жирная только тогда, когда под
    ней есть вторая, — жирный работает подзаголовком к ней. Одинокий счётчик
    активных подписок остаётся обычным (фреймы 126:691 и 139:61 против
    146:197 и 146:263). `HOME_STATUS_NONE` — всегда две строки, там жирный
    зашит в сам текст.
    """
    if active:
        word = plural(active, "активная подписка", "активные подписки", "активных подписок")
        status = f"{active} {word}"
        if finished:
            status = f"<b>{status}</b>"
    else:
        status = HOME_STATUS_NONE

    if finished == 1:
        status += "\n\n" + HOME_STATUS_EXPIRED_ONE
    elif finished > 1:
        status += "\n\n" + HOME_STATUS_EXPIRED_MANY.format(count=finished)
    return status


def home_screen_text(active: int, finished: int) -> str:
    """Текст главного меню — фреймы «Home» в трёх состояниях."""
    if not active and not finished:
        text = HOME_NEW
    else:
        text = HOME_ACTIVE.format(status=subscriptions_status_text(active, finished))
    from app.services.tenant import current_partner_id, current_partner_tier
    if current_partner_id() and current_partner_tier() == "basic":
        text += "\n\nБот создан на платформе @HLSWX"
    return text


def menu_reply_keyboard(user_id: int | None = None, *, has_subscription: bool = False) -> ReplyKeyboardMarkup:
    """Старая reply-клавиатура. Меню на неё больше не опирается, но тексты её
    кнопок бот по-прежнему понимает — они остались висеть у ранних пользователей."""
    from app.services.tenant import current_partner_owner_id
    is_admin = access.is_staff(user_id)
    owner_id = current_partner_owner_id()
    return reply_main_keyboard(
        is_admin=is_admin,
        has_subscription=has_subscription,
        is_partner_owner=bool(owner_id and user_id == owner_id),
    )


# Состав reply-клавиатуры, поставленной в чат: (админ, есть подписка, владелец
# клона). Ключ — (бот, чат): у одного человека разные клавиатуры в основном
# боте и в клоне.
#
# Telegram накладывает на такие сообщения два ограничения сразу: сообщение с
# reply-клавиатурой нельзя редактировать, а если его удалить — клавиатура из
# чата пропадает. Поэтому её носитель — экран меню при первом заходе: он
# остаётся в чате как есть, а перерисовывается уже отдельный экран под ним.
_KEYBOARD_CARRIER: dict[tuple[int, int], tuple[bool, bool, bool]] = {}


async def show_main_menu(message, *, user_id: int | None = None, edit: bool = False) -> None:
    """Главное меню: баннер, текст по состоянию подписок и reply-клавиатура."""
    # Состав клавиатуры зависит от роли — прогреваем кэш доступов до сборки.
    await access.ensure_loaded()
    uid = user_id or (message.from_user.id if message.from_user else None)
    active, finished = await _subscription_counts(uid)
    text = home_screen_text(active, finished)
    chat_id = message.chat.id
    from app.services.tenant import current_partner_id, current_partner_owner_id
    owner_id = current_partner_owner_id()
    signature = (
        access.is_staff(uid),
        bool(active or finished),
        bool(owner_id and uid == owner_id),
    )
    carrier_key = (current_partner_id(), chat_id)

    if _KEYBOARD_CARRIER.get(carrier_key) != signature:
        # Клавиатуры в чате ещё нет или её состав изменился — ставим вместе с
        # экраном меню. Это сообщение потом не редактируется: следующий раздел
        # уйдёт новым, а дальше всё уже перерисовывается на месте.
        await banners.show_new_screen(
            message,
            banners.MAIN_MENU,
            text,
            reply_main_keyboard(
                is_admin=signature[0], has_subscription=signature[1], is_partner_owner=signature[2],
            ),
        )
        banners.forget_screen(chat_id)
        _KEYBOARD_CARRIER[carrier_key] = signature
        return

    await banners.show_screen(message, banners.MAIN_MENU, text, None, edit=edit)


async def send_welcome(message, *, user_id: int | None = None) -> None:
    await show_main_menu(message, user_id=user_id)


async def send_menu_text(message, *, user_id: int | None = None) -> None:
    await show_main_menu(message, user_id=user_id)


async def cancel_inline_flow(callback: CallbackQuery, state: FSMContext) -> None:
    """Выход в главное меню по кнопке «🏠 Главное меню» — на месте покинутого экрана."""
    await state.clear()
    await callback.answer()
    msg = callback.message
    if msg is None:
        return
    await show_main_menu(msg, user_id=callback.from_user.id, edit=True)
