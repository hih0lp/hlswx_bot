"""«Управление → Сообщения» (ТЗ этапа 2, 6.11).

Кадры макета: `349:337` — выбор адресата, `349:916` — ввод username или ID,
`349:419` — текст, `349:923` — подтверждение, `349:434` — отправлено.

До этапа 2 была только рассылка всем сразу; отправка одному пользователю
появляется здесь. Массовая рассылка осталась в `admin_broadcast`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.bot.filters import IsStaff
from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import User
from app.services import admin_audit, admin_ui, banners
from app.services.admin_broadcast import send_to_ids
from app.services.textfmt import moment_label
from app.states.admin import AdminFlow

logger = logging.getLogger("admin")

router = Router()
router.callback_query.filter(IsStaff())
router.message.filter(IsStaff())

MESSAGES_CB = "adm:msg"
ONE_CB = "adm:msg:one"
TO_CB = "adm:msg:to:"           # + <telegram_id>: получатель уже выбран
SEND_CB = "adm:msg:send"
MANAGE_CB = "adm:manage"

# Несколько получателей (ТЗ 6.11): список копится в FSM, как отмеченные чаты
# на шаге выбора в белом списке.
MANY_CB = "adm:msg:many"
MANY_LIST_CB = "adm:msg:many:list"
MANY_DROP_CB = "adm:msg:many:drop:"   # + telegram_id
MANY_NEXT_CB = "adm:msg:many:next"
MANY_SEND_CB = "adm:msg:many:send"


@router.callback_query(F.data == MESSAGES_CB)
async def admin_messages(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_MSG_ONE, callback_data=ONE_CB, style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_MSG_MANY, callback_data=MANY_CB, style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_MSG_ALL, callback_data="adm:broadcast", style=STYLE_PLAIN)],
            [admin_ui.back_button(MANAGE_CB, T.BTN_BACK_TO_MANAGE)],
        ],
    )
    await admin_ui.show(callback, T.MESSAGES, markup)


@router.callback_query(F.data == ONE_CB)
async def admin_message_recipient(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.message_recipient)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.MSG_ASK_RECIPIENT,
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MESSAGES_CB)]]),
    )


@router.callback_query(F.data.startswith(TO_CB))
async def admin_message_to(callback: CallbackQuery, state: FSMContext) -> None:
    """«Написать» из карточки пользователя — кадр 349:967.

    Получатель уже известен, поэтому шаг с вводом username пропускаем и сразу
    просим текст: заставлять админа набирать того, на чьей карточке он стоит,
    незачем.
    """
    telegram_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    who = f"@{user.username}" if user.username else str(user.telegram_id)
    await state.set_state(AdminFlow.message_text)
    await state.update_data(msg_to=user.telegram_id, msg_who=who)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.MSG_ASK_TEXT.format(who=who),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MESSAGES_CB)]]),
    )


@router.message(AdminFlow.message_recipient)
async def admin_message_find(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().lstrip("@")
    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MESSAGES_CB)]])
    async with SessionLocal() as session:
        if raw.isdigit():
            user = await session.scalar(select(User).where(User.telegram_id == int(raw)))
        else:
            user = await session.scalar(select(User).where(func.lower(User.username) == raw.lower()))

    if user is None:
        await admin_ui.show(message, T.MSG_USER_NOT_FOUND.format(who=f"@{raw}"), back, edit=False)
        return

    who = f"@{user.username}" if user.username else str(user.telegram_id)
    await state.set_state(AdminFlow.message_text)
    await state.update_data(msg_to=user.telegram_id, msg_who=who)
    await admin_ui.prompt(message, T.MSG_ASK_TEXT.format(who=who), back)


@router.message(AdminFlow.message_text)
async def admin_message_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MESSAGES_CB)]])
    if not text:
        await admin_ui.show(message, "⚠️ Нужен текст сообщения.", back, edit=False)
        return

    # Подтверждения нет: текст введён — сообщение уходит сразу (кадр 349:434).
    to = data.get("msg_to")
    who = data.get("msg_who", "—")
    if not to:
        await admin_ui.show(message, "⚠️ Получатель потерялся, начните заново.", back, edit=False)
        return
    await state.clear()
    done = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_MSG_AGAIN, callback_data=ONE_CB, style=STYLE_PLAIN)],
            [admin_ui.back_button(MESSAGES_CB, "← Сообщения")],
        ],
    )
    try:
        await message.bot.send_message(to, text)
    except Exception:
        logger.exception("Не удалось отправить сообщение пользователю %s", to)
        await admin_ui.show(message, T.MSG_FAILED.format(who=who), done, edit=False)
        return

    # Экран раздела перестал быть последним сообщением в чате получателя.
    banners.forget_screen(to)
    await admin_audit.log_action(
        message.from_user.id, admin_audit.MESSAGE_SEND, target=str(to), detail=text[:200],
    )
    await admin_ui.show(
        message,
        T.MSG_SENT.format(who=who, when=moment_label(datetime.now(UTC))),
        done,
        edit=False,
    )


# ------------------------------------------------ нескольким получателям (6.11)


async def _find_user(raw: str) -> User | None:
    """Получателя ищем среди тех, кто писал боту: иначе нет telegram_id."""
    value = raw.strip().lstrip("@")
    async with SessionLocal() as session:
        if value.isdigit():
            return await session.scalar(select(User).where(User.telegram_id == int(value)))
        return await session.scalar(select(User).where(func.lower(User.username) == value.lower()))


def _who(user: User) -> str:
    return f"@{user.username}" if user.username else str(user.telegram_id)


def _many_screen(recipients: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Накопленный список: кнопка получателя убирает его из списка."""
    lines = "\n".join(f"• {item['who']}" for item in recipients)
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 {item['who']}",
                callback_data=f"{MANY_DROP_CB}{item['id']}",
                style=STYLE_PLAIN,
            ),
        ]
        for item in recipients
    ]
    rows.append([
        InlineKeyboardButton(text=T.BTN_MSG_ADD_MORE, callback_data=MANY_CB, style=STYLE_PLAIN),
    ])
    rows.append([
        InlineKeyboardButton(text=T.BTN_MSG_NEXT, callback_data=MANY_NEXT_CB, style=STYLE_MAIN),
    ])
    rows.append([admin_ui.back_button(MESSAGES_CB)])
    text = T.MANY_LIST.format(count=len(recipients), lines=lines) + T.MANY_LIST_HINT
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == MANY_CB)
async def admin_many_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Ввод очередного получателя. Уже набранный список не теряем."""
    await state.set_state(AdminFlow.many_recipient)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.MANY_ASK,
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MESSAGES_CB)]]),
    )


@router.message(AdminFlow.many_recipient)
async def admin_many_recipient(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    data = await state.get_data()
    recipients = list(data.get("many_recipients") or [])
    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MESSAGES_CB)]])

    user = await _find_user(raw)
    if user is None:
        await admin_ui.show(
            message,
            T.MSG_USER_NOT_FOUND.format(who=f"@{raw.lstrip('@')}"),
            back,
            edit=False,
        )
        return

    who = _who(user)
    if any(item["id"] == user.telegram_id for item in recipients):
        await admin_ui.show(message, T.MANY_ALREADY.format(who=who), back, edit=False)
        return

    recipients.append({"id": user.telegram_id, "who": who})
    await state.update_data(many_recipients=recipients)
    await state.set_state(AdminFlow.many_recipient)
    await admin_ui.show(message, *_many_screen(recipients), edit=False)


@router.callback_query(F.data == MANY_LIST_CB)
async def admin_many_list(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipients = list(data.get("many_recipients") or [])
    if not recipients:
        await callback.answer(T.MANY_EMPTY, show_alert=True)
        return
    await callback.answer()
    await admin_ui.show(callback, *_many_screen(recipients))


@router.callback_query(F.data.startswith(MANY_DROP_CB))
async def admin_many_drop(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = int(callback.data.rsplit(":", 1)[-1])
    data = await state.get_data()
    recipients = [item for item in (data.get("many_recipients") or []) if item["id"] != telegram_id]
    await state.update_data(many_recipients=recipients)
    await callback.answer("Убрали")
    if not recipients:
        await state.set_state(AdminFlow.many_recipient)
        await admin_ui.prompt(
            callback,
            T.MANY_ASK,
            InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MESSAGES_CB)]]),
        )
        return
    await admin_ui.show(callback, *_many_screen(recipients))


@router.callback_query(F.data == MANY_NEXT_CB)
async def admin_many_next(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipients = list(data.get("many_recipients") or [])
    if not recipients:
        await callback.answer(T.MANY_EMPTY, show_alert=True)
        return
    await state.set_state(AdminFlow.many_text)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.MANY_ASK_TEXT.format(count=len(recipients)),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MANY_LIST_CB)]]),
    )


@router.message(AdminFlow.many_text)
async def admin_many_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    recipients = list(data.get("many_recipients") or [])
    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MANY_LIST_CB)]])
    if not text:
        await admin_ui.show(message, "⚠️ Нужен текст сообщения.", back, edit=False)
        return

    await state.set_state(AdminFlow.many_confirm)
    await state.update_data(many_text=text)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_MSG_SEND, callback_data=MANY_SEND_CB, style=STYLE_MAIN)],
            [admin_ui.back_button(MANY_LIST_CB)],
        ],
    )
    await admin_ui.show(
        message,
        T.MANY_CONFIRM.format(count=len(recipients), text=text),
        markup,
        edit=False,
    )


@router.callback_query(AdminFlow.many_confirm, F.data == MANY_SEND_CB)
async def admin_many_send(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    recipients = list(data.get("many_recipients") or [])
    text = data.get("many_text") or ""
    if not recipients or not text:
        await callback.answer(T.MANY_EMPTY, show_alert=True)
        return

    await callback.answer("Отправляю…")
    stats = await send_to_ids(callback.message.bot, text, [item["id"] for item in recipients])
    await admin_audit.log_action(
        callback.from_user.id,
        admin_audit.MESSAGE_SEND,
        target=", ".join(item["who"] for item in recipients)[:200],
        detail=text[:200],
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_MSG_AGAIN, callback_data=MANY_CB, style=STYLE_PLAIN)],
            [admin_ui.back_button(MESSAGES_CB)],
        ],
    )
    await admin_ui.show(callback, T.MANY_SENT.format(**stats), markup)


@router.callback_query(AdminFlow.message_confirm, F.data == SEND_CB)
async def admin_message_send(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    to = data.get("msg_to")
    who = data.get("msg_who", "—")
    text = data.get("msg_text") or ""

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_MSG_AGAIN, callback_data=ONE_CB, style=STYLE_PLAIN)],
            [admin_ui.back_button(MESSAGES_CB)],
        ],
    )
    try:
        await callback.message.bot.send_message(to, text)
    except Exception:
        logger.exception("Не удалось отправить сообщение пользователю %s", to)
        await callback.answer("Не доставлено", show_alert=True)
        await admin_ui.show(callback, T.MSG_FAILED.format(who=who), markup)
        return

    # Экран раздела перестал быть последним сообщением в чате получателя.
    banners.forget_screen(to)
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.MESSAGE_SEND, target=str(to), detail=text[:200],
    )
    await callback.answer("Отправлено")
    await admin_ui.show(
        callback,
        T.MSG_SENT.format(who=who, when=moment_label(datetime.now(UTC))),
        markup,
    )
