from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.filters import IsPlatformBot
from app.core.texts import WHITELABEL_BTN, WHITELABEL_TEXT
from app.db.session import SessionLocal
from app.keyboards.helpers import MENU_BACK_TEXTS
from app.keyboards.main import inline_back_home, inline_main_menu
from app.keyboards.whitelabel import whitelabel_info_keyboard
from app.models.entities import WhitelabelApplication, WhitelabelApplicationStatus
from app.services.users import get_or_create_user
from app.services.welcome import send_menu_text
from app.states.flows import WhitelabelApplyFlow

whitelabel_apply_router = Router()
whitelabel_apply_router.message.filter(IsPlatformBot())
whitelabel_apply_router.callback_query.filter(IsPlatformBot())

_STATUS_LABELS = {
    WhitelabelApplicationStatus.pending: "⏳ на рассмотрении",
    WhitelabelApplicationStatus.approved: "✅ одобрена",
    WhitelabelApplicationStatus.rejected: "❌ отклонена",
}


async def show_whitelabel_info(message: Message) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user)
        pending = await session.scalar(
            select(WhitelabelApplication).where(
                WhitelabelApplication.user_id == user.id,
                WhitelabelApplication.status == WhitelabelApplicationStatus.pending,
            ).order_by(WhitelabelApplication.id.desc()),
        )
    text = WHITELABEL_TEXT
    show_apply = True
    if pending:
        show_apply = False
        bot_line = f"\nБот: @{pending.planned_bot_username}" if pending.planned_bot_username else ""
        text += (
            f"\n\n<b>Ваша заявка #{pending.id}</b> — {_STATUS_LABELS[pending.status]}\n"
            f"Бренд: <b>{pending.brand_title}</b>{bot_line}\n"
            "Мы свяжемся с вами после проверки."
        )
    await message.answer(text, reply_markup=whitelabel_info_keyboard(show_apply=show_apply))


@whitelabel_apply_router.message(F.text == WHITELABEL_BTN)
async def whitelabel_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_whitelabel_info(message)


@whitelabel_apply_router.callback_query(F.data == "wl:apply")
async def wl_apply_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    async with SessionLocal() as session:
        user = await get_or_create_user(session, callback.from_user)
        pending = await session.scalar(
            select(WhitelabelApplication).where(
                WhitelabelApplication.user_id == user.id,
                WhitelabelApplication.status == WhitelabelApplicationStatus.pending,
            ),
        )
    if pending:
        await callback.message.answer(
            f"У вас уже есть заявка <b>#{pending.id}</b> на рассмотрении.\n"
            "Дождитесь ответа администратора.",
            reply_markup=inline_main_menu(),
        )
        return
    await state.set_state(WhitelabelApplyFlow.brand_title)
    await callback.message.answer(
        "<b>📝 Заявка на WhiteLabel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Шаг 1/3. Как будет называться ваш бренд / сервис?\n"
        "<i>Например: «JobBot MSK» или «Работа Плюс»</i>",
        reply_markup=inline_back_home(),
    )


@whitelabel_apply_router.message(WhitelabelApplyFlow.brand_title)
async def wl_apply_brand(message: Message, state: FSMContext) -> None:
    if message.text in MENU_BACK_TEXTS:
        await state.clear()
        await send_menu_text(message)
        return
    brand = (message.text or "").strip()
    if len(brand) < 2:
        await message.answer("Укажите название бренда (минимум 2 символа).", reply_markup=inline_back_home())
        return
    await state.update_data(wl_brand=brand[:128])
    await state.set_state(WhitelabelApplyFlow.bot_username)
    await message.answer(
        "<b>Шаг 2/3.</b> @username вашего бота в BotFather\n"
        "<i>Если бота ещё нет — отправьте <code>-</code></i>",
        reply_markup=inline_back_home(),
    )


@whitelabel_apply_router.message(WhitelabelApplyFlow.bot_username)
async def wl_apply_bot(message: Message, state: FSMContext) -> None:
    if message.text in MENU_BACK_TEXTS:
        await state.clear()
        await send_menu_text(message)
        return
    raw = (message.text or "").strip().lstrip("@")
    bot_username = None if raw in {"", "-"} else raw[:64]
    await state.update_data(wl_bot=bot_username)
    await state.set_state(WhitelabelApplyFlow.comment)
    await message.answer(
        "<b>Шаг 3/3.</b> Комментарий для нас\n"
        "<i>Города, формат работы, контакт — или <code>-</code></i>",
        reply_markup=inline_back_home(),
    )


@whitelabel_apply_router.message(WhitelabelApplyFlow.comment)
async def wl_apply_finish(message: Message, state: FSMContext) -> None:
    if message.text in MENU_BACK_TEXTS:
        await state.clear()
        await send_menu_text(message)
        return
    comment = (message.text or "").strip()
    if comment == "-":
        comment = None
    data = await state.get_data()
    brand = data["wl_brand"]
    bot_username = data.get("wl_bot")

    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user)
        app = WhitelabelApplication(
            user_id=user.id,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            brand_title=brand,
            planned_bot_username=bot_username,
            comment=comment,
            status=WhitelabelApplicationStatus.pending,
        )
        session.add(app)
        await session.commit()
        await session.refresh(app)
        app_id = app.id

    await state.clear()
    bot_line = f"\nБот: @{bot_username}" if bot_username else ""
    await message.answer(
        f"<b>✅ Заявка #{app_id} отправлена</b>\n"
        "╭──────────────────────╮\n"
        f"Бренд: <b>{brand}</b>{bot_line}\n\n"
        "Мы свяжемся с вами после рассмотрения заявки.\n"
        "Для подключения бота используйте раздел «Франшиза».",
        reply_markup=inline_main_menu(),
    )
