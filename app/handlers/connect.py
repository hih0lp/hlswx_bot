from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core.texts import ADMIN_BTN, CONNECT_BTN
from app.db.session import SessionLocal
from app.handlers.admin import _show_panel
from app.keyboards.main import inline_main_menu
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.services import access
from app.services.b2b import (
    build_webhook_url,
    ensure_integration_for_subscription,
    get_active_corp_subscription,
    get_active_integration,
)
from app.services.users import get_or_create_user

connect_router = Router()


def _is_admin(telegram_id: int) -> bool:
    return access.is_staff(telegram_id)


@connect_router.message(F.text.in_({CONNECT_BTN, "🔌 Подключить"}))
async def connect_entry(message: Message, state: FSMContext) -> None:
    await state.clear()
    await connect_info(message)


async def connect_info(message: Message, *, tg_user=None, edit: bool = False) -> None:
    """Фрейм «Интеграция бота». С активной корп. подпиской показываем выданный API."""
    from app.core.texts import (
        BTN_HOME,
        BTN_INTEGRATION_START,
        INTEGRATION_API,
        INTEGRATION_TEXT,
    )
    from app.services import banners

    user_obj = tg_user or message.from_user
    async with SessionLocal() as session:
        user = await get_or_create_user(session, user_obj)
        sub = await get_active_corp_subscription(session, user.id)
        integration = await get_active_integration(session, user.id) if sub else None
        if sub and not integration:
            integration = await ensure_integration_for_subscription(session, user.id, sub.id)

    if sub and integration:
        text = INTEGRATION_API.format(url=build_webhook_url(integration.token))
        kb_rows = []
    else:
        text = INTEGRATION_TEXT
        kb_rows = [
            [
                InlineKeyboardButton(
                    text=BTN_INTEGRATION_START,
                    callback_data="connect:corp",
                    style=STYLE_MAIN,
                ),
            ],
        ]

    kb_rows.append([InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home", style=STYLE_PLAIN)])
    await banners.show_screen(
        message,
        banners.INTEGRATION,
        text,
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
        edit=edit,
    )


@connect_router.callback_query(F.data == "connect:corp")
async def connect_corp(callback: CallbackQuery, state: FSMContext) -> None:
    from app.handlers.subscription import corp_subscription_start

    await callback.answer()
    await corp_subscription_start(callback.message, state, tg_user=callback.from_user)


@connect_router.message(F.text == ADMIN_BTN)
async def admin_shortcut(message: Message, state: FSMContext, *, user_id: int | None = None) -> None:
    uid = user_id if user_id is not None else (message.from_user.id if message.from_user else 0)
    if not _is_admin(uid):
        await message.answer(
            "Раздел только для администратора.",
            reply_markup=inline_main_menu(is_admin=False),
        )
        return
    await state.clear()
    await _show_panel(message)
