"""Inline-меню: маршрутизация callback → те же экраны, что и reply-кнопки.

Клик по inline-кнопке перерисовывает текущий экран (edit=True) — новое
сообщение бот отправляет только там, где перед этим пользователь вводил текст.

Часть callback'ов ниже уже никто не выдаёт: главное меню переехало на
reply-клавиатуру. Обработчики оставлены, потому что кнопки остались висеть
в истории чатов у пользователей.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.keyboards.pagination import NOOP_CB
from app.services.welcome import cancel_inline_flow

menu_router = Router()


@menu_router.callback_query(F.data == NOOP_CB)
async def cb_noop(callback: CallbackQuery) -> None:
    """Счётчик страниц «2 / 5» — кнопка только ради подписи, ничего не делает.

    Telegram требует у кнопки callback_data, а неотвеченный callback крутит
    у пользователя часики, поэтому отвечаем пустым answer().
    """
    await callback.answer()


@menu_router.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery, state: FSMContext) -> None:
    await cancel_inline_flow(callback, state)


@menu_router.callback_query(F.data == "menu:sub")
async def cb_sub(callback: CallbackQuery, state: FSMContext) -> None:
    from app.handlers.subscription import subscription_start

    await callback.answer()
    await subscription_start(callback.message, state, tg_user=callback.from_user)


@menu_router.callback_query(F.data == "menu:pkg")
async def cb_pkg(callback: CallbackQuery, state: FSMContext) -> None:
    from app.handlers.package import package_start

    await callback.answer()
    await package_start(callback.message, state)


@menu_router.callback_query(F.data == "menu:pub")
async def cb_pub(callback: CallbackQuery, state: FSMContext) -> None:
    from app.handlers.publish import publish_start

    await callback.answer()
    await publish_start(callback.message, state, tg_user=callback.from_user, edit=True)


@menu_router.callback_query(F.data.in_({"menu:mysubs", "profile:mysubs"}))
async def cb_mysubs(callback: CallbackQuery) -> None:
    from app.handlers.subscription import my_subscriptions

    await callback.answer()
    await my_subscriptions(callback.message, tg_user=callback.from_user, edit=True)


@menu_router.callback_query(F.data == "menu:rules")
async def cb_rules(callback: CallbackQuery) -> None:
    from app.handlers.start import show_rules

    await callback.answer()
    await show_rules(callback.message, edit=True)


@menu_router.callback_query(F.data == "menu:connect")
async def cb_connect(callback: CallbackQuery) -> None:
    from app.handlers.connect import connect_info

    await callback.answer()
    await connect_info(callback.message, tg_user=callback.from_user, edit=True)


@menu_router.callback_query(F.data == "menu:franchise")
async def cb_franchise(callback: CallbackQuery, state: FSMContext) -> None:
    from app.handlers.franchise import show_franchise

    await state.clear()
    await callback.answer()
    await show_franchise(callback.message, tg_user=callback.from_user, edit=True)


@menu_router.callback_query(F.data == "menu:whitelabel")
async def cb_whitelabel(callback: CallbackQuery, state: FSMContext) -> None:
    from app.handlers.whitelabel_apply import show_whitelabel_info

    await state.clear()
    await callback.answer()
    await show_whitelabel_info(callback.message)


@menu_router.callback_query(F.data == "menu:admin")
async def cb_admin(callback: CallbackQuery, state: FSMContext) -> None:
    from app.handlers.connect import admin_shortcut

    await callback.answer()
    await admin_shortcut(callback.message, state, user_id=callback.from_user.id)


@menu_router.callback_query(F.data == "menu:profile")
async def cb_profile(callback: CallbackQuery) -> None:
    from app.handlers.start import show_profile

    await callback.answer()
    await show_profile(callback.message, tg_user=callback.from_user, edit=True)
