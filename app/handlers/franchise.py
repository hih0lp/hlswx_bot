from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.db.session import SessionLocal
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import WhitelabelPartner
from app.services.b2b import build_wl_publish_url

franchise_router = Router()


async def _partner_for_user(telegram_id: int) -> WhitelabelPartner | None:
    async with SessionLocal() as session:
        return await session.scalar(
            select(WhitelabelPartner).where(
                WhitelabelPartner.active.is_(True),
                WhitelabelPartner.owner_telegram_id == telegram_id,
            ).order_by(WhitelabelPartner.id.desc()),
        )


async def show_franchise(message: Message, *, tg_user=None, edit: bool = False) -> None:
    """Фрейм «Франшиза».

    Владельцу подключённого бота показываем его панель, остальным — описание
    из макета. Самостоятельная покупка франшизы (оплата → токен → автозапуск)
    в макете есть, но требует новой механики и в этот этап не входит; цена
    в макете тоже не заполнена («Стоимость: X ₽»).
    """
    from app.core.texts import BTN_FRANCHISE_START, BTN_HOME, FRANCHISE_TEXT
    from app.services import banners

    user_obj = tg_user or message.from_user
    partner = await _partner_for_user(user_obj.id)
    if not partner:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=BTN_FRANCHISE_START,
                        callback_data="wl:apply",
                        style=STYLE_MAIN,
                    ),
                ],
                [InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home", style=STYLE_PLAIN)],
            ],
        )
        await banners.show_screen(message, banners.FRANCHISE, FRANCHISE_TEXT, kb, edit=edit)
        return
    await _franchise_panel(message, partner)


@franchise_router.message(Command("franchise"))
@franchise_router.message(F.text == "🏷 Франшиза")
async def franchise_entry(message: Message) -> None:
    await show_franchise(message)


async def _franchise_panel(message: Message, partner: WhitelabelPartner) -> None:
    from app.core.texts import BTN_HOME

    token_status = "✅ токен подключён, бот в polling" if partner.bot_token else "⏳ добавьте bot token в админке"
    brand = partner.brand_title or partner.bot_username
    lines = [
        f"<b>🏷 Франшиза — {brand}</b>",
        "",
        f"Бот: <b>@{partner.bot_username}</b>",
        f"Статус: {token_status}",
        "",
        "<b>API-ключ</b>",
        f"<code>{partner.api_key}</code>",
        "",
        "<b>ML классификация</b>",
        "<code>POST /api/ml/classify</code>",
        "Header: <code>X-API-Key</code>",
        "",
        "<b>Публикация (webhook партнёра)</b>",
        f"<code>{build_wl_publish_url()}</code>",
        "Header: <code>X-API-Key</code>",
        'Body: <code>{"text": "...", "contact": "@user", "photo_url": "https://..."}</code>',
    ]
    if partner.note:
        lines.append(f"\n<i>{partner.note}</i>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home", style=STYLE_PLAIN)],
        ],
    )
    await message.answer("\n".join(lines), reply_markup=kb)
