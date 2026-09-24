"""Рассылка сообщения всем пользователям — «Управление → Сообщения» (ТЗ 6.11).

Раздел живёт с этапа 1 и до этапа 2 работал по старым правилам: каждый экран
уходил новым сообщением, «Назад» вело на главный экран панели, а на
подтверждении стояла «Отмена», которую ТЗ 6.12 велит заменить кнопкой
«Назад» на один шаг. Теперь экраны рисуются на баннере АДМИНКА, как весь
остальной раздел, и возвращают в «Сообщения», а не в корень панели.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core import admin_texts as T
from app.keyboards.style import STYLE_MAIN
from app.services import access, admin_audit, admin_ui
from app.services.admin_broadcast import broadcast_to_users, count_users
from app.states.admin import AdminFlow

logger = logging.getLogger("admin_broadcast")
admin_broadcast_router = Router()

MESSAGES_CB = "adm:msg"
BROADCAST_CB = "adm:broadcast"
SEND_CB = "adm:broadcast:send"

TEXT_LIMIT = 4000
# Экран уходит подписью к баннеру (1024 символа), поэтому предпросмотр текста
# рассылки обрезаем — сам текст уйдёт получателям целиком.
PREVIEW_LIMIT = 600


def _is_admin(user_id: int) -> bool:
    """Рассылка живёт в «Управлении» — только администратор (ТЗ 6.11)."""
    return access.is_admin(user_id)


def _back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MESSAGES_CB)]])


def _confirm_keyboard(count: int) -> InlineKeyboardMarkup:
    """Подтверждение рассылки.

    «Отмена» заменена на «Назад» — по ТЗ 6.12 на многошаговых сценариях
    кнопка ведёт на один шаг назад, а не в начало.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_BROADCAST_SEND.format(count=count),
                    callback_data=SEND_CB,
                    style=STYLE_MAIN,
                ),
            ],
            [admin_ui.back_button(BROADCAST_CB)],
        ],
    )


@admin_broadcast_router.callback_query(F.data == BROADCAST_CB)
async def adm_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    count = await count_users()
    me = await callback.message.bot.get_me()
    await state.set_state(AdminFlow.broadcast_text)
    await admin_ui.prompt(
        callback,
        T.BROADCAST_ASK.format(bot=f"@{me.username}" if me.username else "бот", count=count),
        _back(),
    )


@admin_broadcast_router.message(AdminFlow.broadcast_text)
async def adm_broadcast_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        await admin_ui.show(message, T.BROADCAST_EMPTY, _back(), edit=False)
        return
    if len(text) > TEXT_LIMIT:
        await admin_ui.show(
            message,
            T.BROADCAST_TOO_LONG.format(limit=TEXT_LIMIT),
            _back(),
            edit=False,
        )
        return

    count = await count_users()
    preview = text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT] + "\n\n…"
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminFlow.broadcast_confirm)
    await admin_ui.show(
        message,
        T.BROADCAST_CONFIRM.format(count=count, text=preview),
        _confirm_keyboard(count),
        edit=False,
    )


@admin_broadcast_router.callback_query(AdminFlow.broadcast_confirm, F.data == SEND_CB)
async def adm_broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    text = (data.get("broadcast_text") or "").strip()
    if not text:
        await callback.answer("Нет текста", show_alert=True)
        await state.clear()
        return

    await callback.answer()
    await state.clear()
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.BROADCAST, detail=text[:200],
    )
    await admin_ui.show(callback, T.BROADCAST_RUNNING, _back())

    bot = callback.message.bot
    chat_id = callback.message.chat.id

    async def _run() -> None:
        try:
            stats = await broadcast_to_users(bot, text)
        except Exception:
            logger.exception("HWLS admin broadcast failed")
            await bot.send_message(chat_id, T.BROADCAST_BROKEN, reply_markup=_back())
            return
        # Экран панели мог быть перезаписан самой рассылкой, если админ есть
        # среди получателей, — поэтому итог уходит новым сообщением.
        await bot.send_message(chat_id, T.BROADCAST_DONE.format(**stats), reply_markup=_back())

    asyncio.create_task(_run())
