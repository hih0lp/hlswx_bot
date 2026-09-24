"""Перехват reply-кнопок меню во время FSM — переключение раздела вместо текста объявления."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.core.texts import ADMIN_BTN, CONNECT_BTN, PUBLICATION_BTN, WHITELABEL_BTN
from app.keyboards.helpers import (
    MENU_BACK_TEXTS,
    MENU_HOME_TEXTS,
    PACKAGE_BTN,
    PUBLICATION_BTNS,
    SUBSCRIPTION_BTN,
)

MY_SUBS_BTN = frozenset({"💳 Мои подписки", "📋 Мои подписки"})
RULES_BTNS = frozenset({"📄 Правила", "📌 Правила"})
PROFILE_BTNS = frozenset({"👤 Профиль"})
FRANCHISE_BTNS = frozenset({"🏷 Франшиза", "Франшиза"})
CONNECT_BTNS = frozenset({CONNECT_BTN, "🔌 Подключить"})

# Алиасы, которые пользователи иногда видят / вводят вместо PUBLICATION_BTN
ALL_PUBLICATION_BTNS = PUBLICATION_BTNS | {PUBLICATION_BTN, "📩 Публикация"}


def menu_button_kind(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    if t in MENU_BACK_TEXTS or t in MENU_HOME_TEXTS:
        return "home"
    if t in SUBSCRIPTION_BTN:
        return "subscription"
    if t in PACKAGE_BTN:
        return "package"
    if t in ALL_PUBLICATION_BTNS:
        return "publish"
    if t in MY_SUBS_BTN:
        return "mysubs"
    if t in RULES_BTNS:
        return "rules"
    if t in PROFILE_BTNS:
        return "profile"
    if t in FRANCHISE_BTNS:
        return "franchise"
    if t in CONNECT_BTNS:
        return "connect"
    if t == WHITELABEL_BTN:
        return "whitelabel"
    if t == ADMIN_BTN:
        return "admin"
    return None


async def dispatch_menu_button(message: Message, state: FSMContext) -> bool:
    """Если текст — кнопка меню, сбросить FSM и открыть нужный раздел.

    Сюда попадают нажатия reply-кнопок во время флоу. Кнопка меню — это всегда
    новый раздел, поэтому экран уходит новым сообщением, а у покинутого
    пропадают кнопки; решение принимает `banners.show_screen` по тому, что
    действие пришло сообщением пользователя.
    """
    kind = menu_button_kind(message.text)
    if kind is None:
        return False

    await state.clear()

    if kind == "home":
        from app.services.welcome import show_main_menu

        await show_main_menu(message)
    elif kind == "subscription":
        from app.handlers.subscription import subscription_start

        await subscription_start(message, state)
    elif kind == "package":
        from app.handlers.package import package_start

        await package_start(message, state)
    elif kind == "publish":
        from app.handlers.publish import publish_start

        await publish_start(message, state)
    elif kind == "mysubs":
        from app.handlers.subscription import my_subscriptions

        await my_subscriptions(message)
    elif kind == "rules":
        from app.handlers.start import show_rules

        await show_rules(message)
    elif kind == "profile":
        from app.handlers.start import show_profile

        await show_profile(message)
    elif kind == "franchise":
        from app.handlers.franchise import show_franchise

        await show_franchise(message)
    elif kind == "connect":
        from app.handlers.connect import connect_info

        await connect_info(message)
    elif kind == "whitelabel":
        from app.handlers.whitelabel_apply import show_whitelabel_info

        await show_whitelabel_info(message)
    elif kind == "admin":
        from app.handlers.connect import admin_shortcut

        await admin_shortcut(message, state)

    return True
