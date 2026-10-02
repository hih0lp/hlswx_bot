from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from decimal import Decimal

from sqlalchemy import select

from app.config import get_settings
from app.core.texts import PAYMENT_CTA, TOPUP_AMOUNT, TOPUP_METHOD
from app.db.session import SessionLocal
from app.keyboards.main import (
    inline_back_home,
    inline_home_row,
    pay_button,
    topup_method_keyboard,
)
from app.models.entities import Payment, PaymentStatus
from app.services.menu_nav import dispatch_menu_button
from app.services.payments import (
    PaymentCreationError,
    create_package_payment,
    create_subscription_payment,
    create_topup_payment,
    mark_payment_succeeded,
    payment_error_text,
    pay_package_from_balance,
    pay_subscription_from_balance,
)
from app.services.stars import rub_to_stars, send_stars_topup_invoice
from app.services.users import get_or_create_user
from app.services.wallet import InsufficientBalanceError, format_rub, parse_amount_text
from app.states.flows import TopUpFlow
from app.services.tenant import current_partner_id

logger = logging.getLogger("wallet")
wallet_router = Router()


@wallet_router.callback_query(F.data == "profile:topup")
async def topup_start(callback: CallbackQuery, state: FSMContext) -> None:
    settings = get_settings()
    await state.set_state(TopUpFlow.waiting_amount)
    await callback.answer()
    from app.services import banners

    # На экран ввода суммы пришли кликом из профиля — перерисовываем его.
    await banners.show_prompt(
        callback.message,
        banners.TOPUP,
        TOPUP_AMOUNT.format(minimum=settings.min_topup_amount),
        inline_back_home(),
        edit=True,
    )


@wallet_router.message(TopUpFlow.waiting_amount)
async def topup_amount(message: Message, state: FSMContext) -> None:
    if await dispatch_menu_button(message, state):
        return

    from app.services import banners

    settings = get_settings()
    amount = parse_amount_text(message.text)
    if amount is None or amount < Decimal(settings.min_topup_amount):
        await banners.show_new_screen(
            message,
            banners.ATTENTION,
            f"Введите сумму от <b>{settings.min_topup_amount} ₽</b>. Копейки можно указать через запятую, например 100,50.",
            inline_back_home(),
        )
        return

    is_whole_rubles = amount == amount.to_integral_value()
    stars = rub_to_stars(int(amount)) if is_whole_rubles else None
    await state.update_data(topup_amount=amount)
    await state.set_state(TopUpFlow.waiting_method)
    await banners.show_new_screen(
        message,
        banners.TOPUP,
        TOPUP_METHOD.format(amount=format_rub(amount)),
        topup_method_keyboard(amount, stars, allow_stars=current_partner_id() == 0 and stars is not None),
    )


@wallet_router.callback_query(TopUpFlow.waiting_method, F.data.startswith("topup:card:"))
async def topup_card(callback: CallbackQuery, state: FSMContext) -> None:
    from app.services import banners

    amount = Decimal(callback.data.rsplit(":", 1)[-1])
    await callback.answer()
    try:
        async with SessionLocal() as session:
            user = await get_or_create_user(session, callback.from_user)
            _, url = await create_topup_payment(session, user.id, amount)
    except PaymentCreationError as exc:
        await state.clear()
        await banners.show_screen(
            callback.message,
            banners.ATTENTION,
            payment_error_text(
                exc,
                "<b>⚠️ Не удалось создать оплату</b>\n\n"
                "Платёжная система временно недоступна. Попробуйте позже.",
            ),
            inline_home_row(),
            edit=True,
        )
        return

    await state.clear()
    await banners.show_screen(
        callback.message,
        banners.TOPUP,
        f"💰 <b>Пополнение на {format_rub(amount)} ₽</b>\n\n"
        "После подтверждения платежа средства появятся на балансе в профиле.",
        pay_button(url, "💳 Пополнить баланс"),
        edit=True,
    )


@wallet_router.callback_query(TopUpFlow.waiting_method, F.data.startswith("topup:stars:"))
async def topup_stars(callback: CallbackQuery, state: FSMContext) -> None:
    from app.services import banners

    amount_value = Decimal(callback.data.rsplit(":", 1)[-1])
    if amount_value != amount_value.to_integral_value():
        await callback.answer("Telegram Stars доступны для целой суммы в рублях", show_alert=True)
        return
    amount = int(amount_value)
    if current_partner_id():
        await callback.answer("В боте партнёра пополнение доступно банковской картой", show_alert=True)
        return
    await callback.answer()
    try:
        from app.bot.runtime import get_bot

        async with SessionLocal() as session:
            user = await get_or_create_user(session, callback.from_user)
            await send_stars_topup_invoice(
                get_bot(),
                session,
                chat_id=callback.message.chat.id,
                user_id=user.id,
                amount_rub=amount,
            )
    except Exception:
        logger.exception("Stars invoice failed")
        await state.clear()
        await banners.show_screen(
            callback.message,
            banners.ATTENTION,
            "<b>⚠️ Не удалось выставить счёт Stars</b>\n\nПопробуйте позже или оплатите картой.",
            inline_home_row(),
            edit=True,
        )
        return

    await state.clear()
    stars = rub_to_stars(amount)
    # Счёт Telegram Stars приходит отдельным сообщением от самого Telegram,
    # а экран выбора способа превращаем в пояснение к нему.
    await banners.show_screen(
        callback.message,
        banners.TOPUP,
        f"⭐ <b>Счёт на {stars} Telegram Stars</b>\n\n"
        f"После оплаты на баланс зачислится <b>{format_rub(amount)} ₽</b>.",
        None,
        edit=True,
    )


@wallet_router.pre_checkout_query(F.invoice_payload.startswith("topup_stars:"))
async def stars_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=current_partner_id() == 0, error_message="В этом боте оплата доступна банковской картой.")


@wallet_router.message(F.successful_payment)
async def stars_successful_payment(message: Message) -> None:
    sp = message.successful_payment
    if not sp or not sp.invoice_payload.startswith("topup_stars:"):
        return

    parts = sp.invoice_payload.split(":")
    if len(parts) < 3:
        return
    try:
        payment_id = int(parts[1])
    except ValueError:
        return

    if current_partner_id():
        logger.error("Unexpected Telegram Stars payment in partner tenant; payload=%s", sp.invoice_payload)
        await message.answer("Оплата Stars в этом боте не поддерживается. Обратитесь к владельцу бота.")
        return

    async with SessionLocal() as session:
        payment = await session.scalar(select(Payment).where(Payment.id == payment_id))
        if not payment or payment.status == PaymentStatus.succeeded:
            return
        payment.provider = "telegram_stars"
        payment.provider_payment_id = sp.telegram_payment_charge_id
        result = await mark_payment_succeeded(session, payment)
        user_id = payment.user_id
        amount = payment.amount

    from app.services.notifications import notify_payment_success

    await notify_payment_success(user_id, result, user_id, amount=amount)
    await message.answer(
        f"✅ <b>Баланс пополнен</b> на <b>{format_rub(amount)} ₽</b>\n"
        f"Оплачено: <b>{sp.total_amount} ⭐</b>",
    )


@wallet_router.callback_query(F.data.startswith("pay:card:sub:"))
async def pay_sub_card(callback: CallbackQuery) -> None:
    from app.services import banners

    sub_id = int(callback.data.rsplit(":", 1)[-1])
    try:
        async with SessionLocal() as session:
            user = await get_or_create_user(session, callback.from_user)
            from app.models.entities import Subscription, SubscriptionStatus

            sub = await session.scalar(
                select(Subscription).where(
                    Subscription.id == sub_id,
                    Subscription.user_id == user.id,
                    # `expired` / `active` — продление подписки («🔄 Продлить»).
                    Subscription.status.in_(
                        (SubscriptionStatus.pending_payment, SubscriptionStatus.expired, SubscriptionStatus.active),
                    ),
                    Subscription.deleted_at.is_(None),
                ),
            )
            if not sub:
                await callback.answer("Подписка не найдена или уже оплачена", show_alert=True)
                return
            await callback.answer()
            total_price = sub.total_price
            _, url = await create_subscription_payment(
                session,
                user.id,
                sub_id,
                total_price,
                f"HWLS подписка #{sub_id}",
            )
    except PaymentCreationError as exc:
        await banners.show_screen(
            callback.message,
            banners.ATTENTION,
            payment_error_text(
                exc, "<b>⚠️ Не удалось создать оплату</b>\n\nПопробуйте через пару минут.",
            ),
            inline_home_row(),
            edit=True,
        )
        return

    await banners.show_screen(
        callback.message,
        banners.AD,
        f"<b>💳 Оплата подписки #{sub_id}</b>\n"
        f"Сумма: <b>{total_price:,} ₽</b>"
        f"{PAYMENT_CTA}",
        pay_button(url, "💳 Перейти к оплате"),
        edit=True,
    )


@wallet_router.callback_query(F.data.startswith("pay:bal:sub:"))
async def pay_sub_balance(callback: CallbackQuery) -> None:
    sub_id = int(callback.data.rsplit(":", 1)[-1])
    try:
        async with SessionLocal() as session:
            user = await get_or_create_user(session, callback.from_user)
            from app.models.entities import Subscription, SubscriptionStatus

            sub = await session.scalar(
                select(Subscription).where(
                    Subscription.id == sub_id,
                    Subscription.user_id == user.id,
                    # `expired` / `active` — продление подписки («🔄 Продлить»).
                    Subscription.status.in_(
                        (SubscriptionStatus.pending_payment, SubscriptionStatus.expired, SubscriptionStatus.active),
                    ),
                    Subscription.deleted_at.is_(None),
                ),
            )
            if not sub:
                await callback.answer("Подписка не найдена", show_alert=True)
                return
            total_price = sub.total_price
            user_id = user.id
            result = await pay_subscription_from_balance(session, user_id, sub_id, total_price)
            await session.refresh(user)
            new_balance = user.balance
    except InsufficientBalanceError:
        await callback.answer("Недостаточно средств на балансе", show_alert=True)
        return
    except PaymentCreationError:
        await callback.answer("Не удалось списать с баланса", show_alert=True)
        return

    await callback.answer("✅ Оплачено с баланса")
    from app.services import banners
    from app.services.notifications import notify_payment_success

    await banners.show_screen(
        callback.message,
        banners.AD,
        f"💰 Списано <b>{total_price:,} ₽</b> с баланса.\n"
        f"Остаток: <b>{format_rub(new_balance)} ₽</b>",
        None,
        edit=True,
    )
    await notify_payment_success(user_id, result, sub_id)


@wallet_router.callback_query(F.data.startswith("pay:card:pkg:"))
async def pay_pkg_card(callback: CallbackQuery) -> None:
    from app.services import banners

    order_id = int(callback.data.rsplit(":", 1)[-1])
    try:
        async with SessionLocal() as session:
            user = await get_or_create_user(session, callback.from_user)
            from app.models.entities import PackageOrder

            order = await session.scalar(
                select(PackageOrder).where(
                    PackageOrder.id == order_id,
                    PackageOrder.user_id == user.id,
                    PackageOrder.status == "pending_payment",
                ),
            )
            if not order:
                await callback.answer("Пакет не найден или уже оплачен", show_alert=True)
                return
            await callback.answer()
            _, url = await create_package_payment(
                session,
                user.id,
                order_id,
                order.price,
                f"HWLS пакет #{order_id}",
            )
            price = order.price
    except PaymentCreationError as exc:
        await banners.show_screen(
            callback.message,
            banners.ATTENTION,
            payment_error_text(
                exc, "<b>⚠️ Не удалось создать оплату</b>\n\nПопробуйте через пару минут.",
            ),
            inline_home_row(),
            edit=True,
        )
        return

    await banners.show_screen(
        callback.message,
        banners.AD,
        f"<b>💳 Оплата пакета #{order_id}</b>\n"
        f"Сумма: <b>{price:,} ₽</b>"
        f"{PAYMENT_CTA}",
        pay_button(url, "💳 Перейти к оплате"),
        edit=True,
    )


@wallet_router.callback_query(F.data.startswith("pay:bal:pkg:"))
async def pay_pkg_balance(callback: CallbackQuery) -> None:
    order_id = int(callback.data.rsplit(":", 1)[-1])
    try:
        async with SessionLocal() as session:
            user = await get_or_create_user(session, callback.from_user)
            from app.models.entities import PackageOrder

            order = await session.scalar(
                select(PackageOrder).where(
                    PackageOrder.id == order_id,
                    PackageOrder.user_id == user.id,
                    PackageOrder.status == "pending_payment",
                ),
            )
            if not order:
                await callback.answer("Пакет не найден", show_alert=True)
                return
            price = order.price
            user_id = user.id
            result = await pay_package_from_balance(session, user_id, order_id, price)
            await session.refresh(user)
            new_balance = user.balance
    except InsufficientBalanceError:
        await callback.answer("Недостаточно средств на балансе", show_alert=True)
        return
    except PaymentCreationError:
        await callback.answer("Не удалось списать с баланса", show_alert=True)
        return

    await callback.answer("✅ Оплачено с баланса")
    from app.services import banners
    from app.services.notifications import notify_payment_success

    await banners.show_screen(
        callback.message,
        banners.AD,
        f"💰 Списано <b>{price:,} ₽</b> с баланса.\n"
        f"Остаток: <b>{format_rub(new_balance)} ₽</b>",
        None,
        edit=True,
    )
    await notify_payment_success(user_id, result, order_id)
