"""«Управление → Доступы» — выдача роли в панели (ТЗ этапа 2, 6.11).

Кадры макета: `349:446` — ввод username или ID, `349:457` — выбор роли,
`349:473` — подтверждение, `349:487` — доступ выдан.

Раздел «Управление» целиком доступен только администратору (ТЗ 7), поэтому
фильтр висит на роутере, а не на каждом обработчике.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.bot.filters import IsAdmin
from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import AdminRole, User
from app.services import access, admin_audit, admin_ui
from app.states.admin import AdminFlow

router = Router()
router.callback_query.filter(IsAdmin())
router.message.filter(IsAdmin())

ACCESS_CB = "adm:access"
ADD_CB = "adm:access:add"
LIST_CB = "adm:access:list"
ROLE_CB = "adm:access:role:"      # + <роль>:<telegram_id>
GRANT_CB = "adm:access:grant:"    # + <роль>:<telegram_id>
REVOKE_CB = "adm:access:revoke:"  # + <telegram_id>

MANAGE_CB = "adm:manage"

_ROLE_LABELS = {AdminRole.admin: T.ROLE_ADMIN, AdminRole.manager: T.ROLE_MANAGER}


def _who(username: str | None, telegram_id: int) -> str:
    """Для текста экрана — с разметкой."""
    return f"@{username}" if username else f"<code>{telegram_id}</code>"


def _who_plain(username: str | None, telegram_id: int) -> str:
    """Для подписи кнопки: разметка в ней не работает."""
    return f"@{username}" if username else str(telegram_id)


@router.callback_query(F.data == ACCESS_CB)
async def admin_access_open(callback: CallbackQuery, state: FSMContext) -> None:
    """«Доступы» — сразу ввод username / Telegram ID, как в кадре 349:446."""
    await state.set_state(AdminFlow.access_user)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.ACCESS_ASK,
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(MANAGE_CB, T.BTN_BACK_TO_MANAGE)]]),
    )


# Список выданных доступов остался, но кнопки на него в макете нет.
@router.callback_query(F.data == LIST_CB)
async def admin_access_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    rows = await access.list_access()
    text = T.ACCESS_LIST.format(count=len(rows))
    if not rows:
        text += T.ACCESS_LIST_EMPTY

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{_ROLE_LABELS[row.role]} · {_who_plain(row.username, row.telegram_id)}",
                callback_data=f"{REVOKE_CB}{row.telegram_id}",
                style=STYLE_PLAIN,
            ),
        ]
        for row in rows
    ]
    buttons.append([InlineKeyboardButton(text=T.BTN_ACCESS_ADD, callback_data=ADD_CB, style=STYLE_MAIN)])
    buttons.append([admin_ui.back_button(MANAGE_CB, "← Управление")])
    await admin_ui.show(callback, text, InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == ADD_CB)
async def admin_access_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.access_user)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.ACCESS_ASK,
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(ACCESS_CB)]]),
    )


@router.message(AdminFlow.access_user)
async def admin_access_user(message: Message, state: FSMContext) -> None:
    """Ищем пользователя среди тех, кто уже писал боту.

    Выдать доступ по username человеку, которого бот не знает, нельзя:
    роль привязана к telegram_id, а Telegram его по username не отдаёт.
    """
    raw = (message.text or "").strip().lstrip("@")
    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(ACCESS_CB)]])
    async with SessionLocal() as session:
        if raw.isdigit():
            user = await session.scalar(select(User).where(User.telegram_id == int(raw)))
        else:
            user = await session.scalar(select(User).where(func.lower(User.username) == raw.lower()))

    if user is None:
        await admin_ui.show(message, T.ACCESS_NOT_FOUND.format(who=f"@{raw}"), back, edit=False)
        return

    await state.clear()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.ROLE_ADMIN,
                    callback_data=f"{ROLE_CB}{AdminRole.admin.value}:{user.telegram_id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=T.ROLE_MANAGER,
                    callback_data=f"{ROLE_CB}{AdminRole.manager.value}:{user.telegram_id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [admin_ui.back_button(ACCESS_CB)],
        ],
    )
    await admin_ui.show(
        message,
        T.ACCESS_FOUND.format(who=_who(user.username, user.telegram_id), telegram_id=user.telegram_id),
        markup,
        edit=False,
    )


@router.callback_query(F.data.startswith(ROLE_CB))
async def admin_access_confirm(callback: CallbackQuery) -> None:
    _, role_value, raw_id = callback.data.rsplit(":", 2)
    role = AdminRole(role_value)
    telegram_id = int(raw_id)
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))

    await callback.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_ACCESS_GRANT,
                    callback_data=f"{GRANT_CB}{role_value}:{telegram_id}",
                    style=STYLE_MAIN,
                ),
            ],
            [admin_ui.back_button(ADD_CB, "← Изменить")],
            [admin_ui.panel_button()],
        ],
    )
    await admin_ui.show(
        callback,
        T.ACCESS_CONFIRM.format(
            who=_who(user.username if user else None, telegram_id),
            telegram_id=telegram_id,
            role=_ROLE_LABELS[role],
        ),
        markup,
    )


@router.callback_query(F.data.startswith(GRANT_CB))
async def admin_access_grant(callback: CallbackQuery) -> None:
    _, role_value, raw_id = callback.data.rsplit(":", 2)
    role = AdminRole(role_value)
    telegram_id = int(raw_id)
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))

    await access.grant(
        telegram_id,
        role,
        by_telegram_id=callback.from_user.id,
        username=user.username if user else None,
    )
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.ACCESS_GRANT, target=str(telegram_id), detail=role.value,
    )
    await _notify_user(telegram_id, role)
    await callback.answer("Доступ выдан")
    await admin_ui.show(
        callback,
        T.ACCESS_GRANTED.format(
            who=_who(user.username if user else None, telegram_id), role=_ROLE_LABELS[role],
        ),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(ACCESS_CB, "🔐 Доступы")]]),
    )


@router.callback_query(F.data.startswith(REVOKE_CB))
async def admin_access_revoke(callback: CallbackQuery) -> None:
    telegram_id = int(callback.data.rsplit(":", 1)[-1])
    if not await access.revoke(telegram_id):
        await callback.answer("Доступ уже отозван", show_alert=True)
        return
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.ACCESS_REVOKE, target=str(telegram_id),
    )
    await callback.answer("Доступ отозван")
    await admin_ui.show(
        callback,
        T.ACCESS_REVOKED.format(who=f"<code>{telegram_id}</code>"),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(ACCESS_CB, "🔐 Доступы")]]),
    )


async def _notify_user(telegram_id: int, role: AdminRole) -> None:
    """Сообщаем человеку, что доступ появился — иначе он о нём не узнает."""
    from app.bot.runtime import get_bot

    try:
        await get_bot().send_message(
            telegram_id,
            f"<b>🔐 Вам выдан доступ к админ-панели</b>\n\nРоль: {_ROLE_LABELS[role]}\n\n"
            "Откройте панель командой /admin.",
        )
    except Exception:
        pass
