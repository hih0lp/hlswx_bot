from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.core.texts import (
    BTN_HOME,
    PACKAGE_TEXT_PENDING,
    PAYMENT_CTA,
    PKG_STEP1,
    PKG_STEP2,
)
from app.db.session import SessionLocal
from app.keyboards.helpers import PACKAGE_BTN
from app.keyboards.main import (
    inline_back_cancel,
    inline_home_row,
    pay_button,
    payment_choice_keyboard,
)
from app.ml.categories import PACKAGE_CITY_KEYS, PACKAGE_TARIFFS
from app.models.entities import Chat, City, PackageOrder
from app.services.flow_ui import edit_flow_callback, show_flow_step
from app.services.menu_nav import dispatch_menu_button
from app.services.payments import PaymentCreationError, create_package_payment
from app.services.users import get_or_create_user
from app.states.flows import PackageFlow

package_router = Router()


def _placeholder_contact(from_user) -> str:
    if from_user.username:
        return f"@{from_user.username}"
    return "—"


def _city_kb(cities: list[City]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗺 {c.label}", callback_data=f"pkg:city:{c.id}:{c.key}")]
        for c in cities
    ]
    rows.append([InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tariff_kb(city_key: str) -> InlineKeyboardMarkup:
    tariffs = PACKAGE_TARIFFS.get(city_key, PACKAGE_TARIFFS["nn"])
    rows = []
    for price, pinned, label in tariffs:
        pin = 1 if pinned else 0
        icon = "📌" if pinned else "📢"
        promo = " (выгодно!)" if pinned else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {label}{promo} — {price:,} ₽",
                callback_data=f"pkg:tariff:{price}:{pin}",
            ),
        ])
    rows.append([InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _finalize_package(message: Message, state: FSMContext, data: dict) -> None:
    try:
        async with SessionLocal() as session:
            user = await get_or_create_user(session, message.from_user)
            order = PackageOrder(
                user_id=user.id,
                city_id=data["city_id"],
                text=PACKAGE_TEXT_PENDING,
                contact=_placeholder_contact(message.from_user),
                price=int(data["price"]),
                is_pinned=bool(data["is_pinned"]),
                status="pending_payment",
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            await session.refresh(user)
            order_id = order.id
            price = order.price
            balance = user.balance or Decimal("0")

            pay_header = f"<b>Шаг 3 из 3: Оплата</b>\n\nПакет <b>#{order_id}</b> · 💰 <b>{price:,} ₽</b>"

            if balance >= price and price > 0:
                await state.clear()
                await show_flow_step(
                    message,
                    state,
                    pay_header + "\n\nВыберите способ оплаты:",
                    payment_choice_keyboard("pkg", order_id, price, balance),
                )
                return

            _, url = await create_package_payment(
                session,
                user.id,
                order_id,
                price,
                f"HWLS пакет #{order_id}",
            )
    except PaymentCreationError:
        await show_flow_step(
            message,
            state,
            "<b>⚠️ Не удалось создать оплату</b>\n\nПопробуйте через пару минут.",
            inline_home_row(),
        )
        await state.clear()
        return

    await state.clear()
    await show_flow_step(
        message,
        state,
        pay_header + PAYMENT_CTA,
        pay_button(url, "💳 Перейти к оплате"),
    )


@package_router.message(Command("package"))
@package_router.message(F.text.in_(PACKAGE_BTN))
async def package_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with SessionLocal() as session:
        cities = [
            c for c in (await session.scalars(select(City).where(City.active.is_(True)))).all()
            if c.key in PACKAGE_CITY_KEYS
        ]
    await state.set_state(PackageFlow.selecting_city)
    await show_flow_step(message, state, PKG_STEP1, _city_kb(cities))


@package_router.callback_query(PackageFlow.selecting_city, F.data.startswith("pkg:city:"))
async def package_city(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, city_id, city_key = callback.data.split(":", 3)
    await state.update_data(city_id=int(city_id), city_key=city_key)
    async with SessionLocal() as session:
        city = await session.scalar(select(City).where(City.id == int(city_id)))
        chats = (
            await session.scalars(
                select(Chat).where(Chat.city_id == int(city_id), Chat.active.is_(True)).order_by(Chat.sort_order),
            )
        ).all()
    chat_lines = "\n".join(f"  • @{c.telegram_username}" for c in chats) or "  —"
    city_label = city.label if city else city_key
    await state.set_state(PackageFlow.selecting_tariff)
    await edit_flow_callback(
        callback,
        state,
        PKG_STEP2.format(city=city_label, catalog=chat_lines),
        _tariff_kb(city_key),
    )


@package_router.callback_query(PackageFlow.selecting_tariff, F.data.startswith("pkg:tariff:"))
async def package_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, price, pin = callback.data.split(":")
    data = await state.get_data()
    data.update(price=int(price), is_pinned=bool(int(pin)))
    await callback.answer()
    await _finalize_package(callback.message, state, data)


@package_router.callback_query(F.data.startswith("pkg:text:"))
async def package_text_start(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split(":")[-1])
    await state.set_state(PackageFlow.waiting_post_payment_text)
    await state.update_data(package_order_id=order_id)
    await callback.answer()
    await show_flow_step(
        callback.message,
        state,
        f"<b>📝 Текст для пакета #{order_id}</b>\n\n"
        "Пришлите полный текст объявления:",
        inline_back_cancel(),
        force_new=True,
    )


@package_router.message(PackageFlow.waiting_post_payment_text)
async def package_post_payment_text(message: Message, state: FSMContext) -> None:
    if await dispatch_menu_button(message, state):
        return

    text = (message.text or "").strip()
    if len(text) < 10:
        await message.answer("Текст слишком короткий. Допишите объявление.", reply_markup=inline_back_cancel())
        return

    data = await state.get_data()
    order_id = int(data["package_order_id"])
    async with SessionLocal() as session:
        order = await session.scalar(select(PackageOrder).where(PackageOrder.id == order_id))
        if not order or order.status != "paid":
            await message.answer("Пакет не найден или ещё не оплачен.", reply_markup=inline_home_row())
            await state.clear()
            return
        order.text = text
        await session.commit()

    from app.services.publish import enqueue_package_order

    count = await enqueue_package_order(order_id)
    await state.clear()
    await message.answer(
        f"<b>✅ Пакет #{order_id} в очереди</b>\n📬 Чатов: <b>{count}</b>",
    )
