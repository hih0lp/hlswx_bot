"""Inline UI: edit на кликах, archive + новое сообщение на ввод текста."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger("flow_ui")

FLOW_UI_MSG_KEY = "flow_ui_msg_id"


async def archive_flow_step(
    message: Message,
    state: FSMContext,
    archive_text: str,
    *,
    parse_mode: str = "HTML",
) -> None:
    data = await state.get_data()
    raw = data.get(FLOW_UI_MSG_KEY)
    if raw is None:
        return
    msg_id = int(raw)
    try:
        await message.bot.edit_message_text(
            text=archive_text,
            chat_id=message.chat.id,
            message_id=msg_id,
            reply_markup=None,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("archive_flow_step: %s", exc)
    except Exception:
        logger.exception("archive_flow_step failed")
    await state.update_data({FLOW_UI_MSG_KEY: None})


async def show_flow_step(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    force_new: bool = False,
    parse_mode: str = "HTML",
) -> Message:
    """Показать/обновить активный шаг. force_new — после ввода текста пользователем."""
    chat_id = message.chat.id
    msg_id: int | None = None
    if not force_new:
        raw = (await state.get_data()).get(FLOW_UI_MSG_KEY)
        if raw is not None:
            msg_id = int(raw)

    if msg_id is not None and not force_new:
        try:
            await message.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return message
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                try:
                    await message.bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=reply_markup,
                    )
                except Exception:
                    pass
                return message
            logger.debug("show_flow_step edit failed: %s", exc)
        except Exception:
            logger.exception("show_flow_step edit failed")

    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    await state.update_data({FLOW_UI_MSG_KEY: sent.message_id})
    return sent


async def edit_flow_callback(
    callback: CallbackQuery,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    parse_mode: str = "HTML",
) -> None:
    """Обновить сообщение из callback (клик по кнопке)."""
    msg = callback.message
    data = await state.get_data()
    msg_id = data.get(FLOW_UI_MSG_KEY) or msg.message_id
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        await state.update_data({FLOW_UI_MSG_KEY: msg_id})
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("edit_flow_callback: %s", exc)
    await callback.answer()
