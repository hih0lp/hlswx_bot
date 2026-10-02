from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.entities import (
    PackageOrder,
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    WhitelabelPartner,
)
from app.services.fraud import subscription_period
from app.services.wallet import InsufficientBalanceError, get_user_for_update
from app.services.yookassa import YooKassaService
from app.services.tenant import current_partner_id

logger = logging.getLogger("payments")

# Префикс invoice_id — единственный признак, по которому видно, откуда пришли
# деньги: отдельной колонки в payments нет. Нужен для выручки (ТЗ 6.2, 6.4).
INVOICE_SUB = "hwls-sub-"            # подписка картой
INVOICE_PKG = "hwls-pkg-"            # разовый пакет картой
INVOICE_TOPUP = "hwls-topup-"        # пополнение баланса картой
INVOICE_STARS = "hwls-stars-"        # пополнение через Telegram Stars
INVOICE_BALANCE_SUB = "hwls-bal-sub-"  # оплата подписки с баланса
INVOICE_BALANCE_PKG = "hwls-bal-pkg-"  # оплата пакета с баланса
INVOICE_WHITELIST = "hwls-wl-"       # бесплатная активация по белому списку, сумма 0

# Настоящий приход денег. Оплата с баланса — не приход: эти деньги уже
# посчитаны при пополнении, иначе выручка удваивается.
CASH_IN_PREFIXES = (INVOICE_SUB, INVOICE_PKG, INVOICE_TOPUP, INVOICE_STARS)



class PaymentCreationError(Exception):
    """YooKassa or payment setup failed."""


class PartnerPaymentsNotConfigured(PaymentCreationError):
    """Владелец клона не указал реквизиты ЮKassa — принимать оплату некуда."""


def payment_error_text(exc: BaseException, default: str) -> str:
    """Что показать клиенту: «владелец не подключил оплату» — вместо общего
    «платёжная система недоступна», если дело именно в этом."""
    if isinstance(exc, PartnerPaymentsNotConfigured):
        from app.core.texts import ERR_PARTNER_PAYMENTS_OFF
        return ERR_PARTNER_PAYMENTS_OFF
    return default


# partner_id -> когда владельцу в последний раз писали, что оплата не настроена.
_UNCONFIGURED_NOTICE_AT: dict[int, float] = {}
_UNCONFIGURED_NOTICE_EVERY = 6 * 3600


async def _notify_owner_payments_unconfigured(partner_id: int) -> None:
    """Сообщить владельцу клона, что клиент не смог оплатить из-за пустых
    реквизитов. Не чаще раза в несколько часов, чтобы не заспамить."""
    import time

    now = time.monotonic()
    last = _UNCONFIGURED_NOTICE_AT.get(partner_id)
    if last is not None and now - last < _UNCONFIGURED_NOTICE_EVERY:
        return
    _UNCONFIGURED_NOTICE_AT[partner_id] = now
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from app.bot.runtime import _get_main_bot
        from app.db.session import SessionLocal

        async with SessionLocal() as session:
            partner = await session.scalar(
                select(WhitelabelPartner)
                .where(WhitelabelPartner.id == partner_id)
                .execution_options(skip_partner_scope=True)
            )
        if not partner or not partner.owner_telegram_id:
            return
        username = partner.bot_username
        text = (
            "<b>⚠️ Клиент не смог оплатить</b>\n\n"
            + (f"В вашем боте @{username} клиент" if username else "В вашем боте клиент")
            + " попытался оплатить, но платёжные реквизиты не указаны — оплата не прошла.\n\n"
            "Откройте бота → «🏷 Франшиза» → «💳 Платёжные реквизиты» и укажите "
            "shopId и секретный ключ вашего магазина в ЮKassa."
        )
        markup = None
        if username:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"🤖 Открыть @{username}", url=f"https://t.me/{username}"),
            ]])
        await _get_main_bot().send_message(partner.owner_telegram_id, text, reply_markup=markup)
    except Exception:
        # Не доставили — не блокируем следующую попытку на несколько часов.
        _UNCONFIGURED_NOTICE_AT.pop(partner_id, None)
        logger.exception("Could not notify owner of partner_id=%s about missing payment credentials", partner_id)


async def payment_provider_for_partner(settings, partner_id: int) -> YooKassaService:
    """Build an isolated YooKassa client for a tenant (ТЗ 6.6).

    Партнёр вводит свои реквизиты сам через бота («Франшиза → Платёжные
    реквизиты»); они хранятся в `whitelabel_partners.yookassa_shop_id` /
    `yookassa_secret_key`. Переменные окружения `YOOKASSA_PARTNER_<id>_*`
    остаются запасным путём — для партнёров, подключённых вручную до того,
    как это поле появилось в боте.
    """
    if not partner_id:
        return YooKassaService(settings)
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        partner = await session.scalar(
            select(WhitelabelPartner)
            .where(WhitelabelPartner.id == partner_id)
            .execution_options(skip_partner_scope=True)
        )
    shop_id = ((partner.yookassa_shop_id if partner else None) or "").strip()
    secret_key = ((partner.yookassa_secret_key if partner else None) or "").strip()
    if not shop_id or not secret_key:
        import os
        prefix = f"YOOKASSA_PARTNER_{partner_id}_"
        shop_id = os.getenv(prefix + "SHOP_ID", "").strip()
        secret_key = os.getenv(prefix + "SECRET_KEY", "").strip()
    if not shop_id or not secret_key:
        raise PartnerPaymentsNotConfigured("Partner YooKassa shop credentials are not configured")
    update = {"yookassa_shop_id": shop_id, "yookassa_secret_key": secret_key}
    username = (partner.bot_username if partner else None) or ""
    if username:
        # Страница «оплата обработана» должна отправлять клиента в бота клона,
        # а не в основной.
        base = settings.yookassa_return_url
        update["yookassa_return_url"] = f"{base}{'&' if '?' in base else '?'}bot={username}"
    return YooKassaService(settings.model_copy(update=update))


async def _tenant_payment_provider(settings) -> YooKassaService:
    partner_id = current_partner_id()
    try:
        return await payment_provider_for_partner(settings, partner_id)
    except PartnerPaymentsNotConfigured:
        await _notify_owner_payments_unconfigured(partner_id)
        raise


def _next_period(sub: Subscription) -> tuple[datetime, datetime]:
    """Период, на который активируется (или продлевается) подписка.

    Новая и завершённая подписка идут от сегодняшнего дня. Продление действующей
    («🔄 Продлить» в напоминании за сутки до конца) — от даты окончания
    текущего периода: оставшиеся дни не сгорают.
    """
    starts, ends = subscription_period()
    if sub.status == SubscriptionStatus.active and sub.expires_at and sub.expires_at > starts:
        return sub.starts_at or starts, sub.expires_at + (ends - starts)
    return starts, ends


async def activate_subscription(session: AsyncSession, subscription_id: int) -> bool:
    sub = await session.scalar(select(Subscription).where(Subscription.id == subscription_id))
    if not sub:
        return False
    starts, ends = _next_period(sub)
    sub.status = SubscriptionStatus.active
    sub.starts_at = starts
    sub.expires_at = ends
    # Продление завершённой подписки идёт через эту же активацию: у нового
    # периода должны сработать и напоминание за сутки, и уведомление об окончании.
    sub.reminded_at = None
    sub.expired_notified_at = None
    return True


async def activate_package(session: AsyncSession, order_id: int) -> bool:
    order = await session.scalar(select(PackageOrder).where(PackageOrder.id == order_id))
    if not order:
        return False
    order.status = "paid"
    return True


async def create_subscription_payment(
    session: AsyncSession,
    user_id: int,
    subscription_id: int,
    amount: int,
    description: str,
) -> tuple[Payment, str]:
    yk = await _tenant_payment_provider(get_settings())
    invoice_id = f"hwls-sub-{subscription_id}-{uuid.uuid4().hex[:8]}"
    payment_row = Payment(
        user_id=user_id,
        amount=Decimal(amount),
        purpose="subscription",
        purpose_id=subscription_id,
        invoice_id=invoice_id,
        status=PaymentStatus.pending,
    )
    session.add(payment_row)
    await session.flush()

    try:
        data = await yk.create_payment(
            Decimal(amount),
            description,
            {"purpose": "subscription", "purpose_id": str(subscription_id), "invoice_id": invoice_id},
            idempotence_key=invoice_id,
        )
    except Exception:
        await session.rollback()
        logger.exception("YooKassa payment creation failed for subscription %s", subscription_id)
        raise PaymentCreationError from None

    payment_row.provider_payment_id = data.get("id")
    payment_row.confirmation_url = yk.extract_confirmation_url(data)
    sub = await session.scalar(select(Subscription).where(Subscription.id == subscription_id))
    if sub:
        sub.payment_id = payment_row.id
    await session.commit()
    return payment_row, payment_row.confirmation_url or ""


async def create_package_payment(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    amount: int,
    description: str,
) -> tuple[Payment, str]:
    yk = await _tenant_payment_provider(get_settings())
    invoice_id = f"hwls-pkg-{order_id}-{uuid.uuid4().hex[:8]}"
    payment_row = Payment(
        user_id=user_id,
        amount=Decimal(amount),
        purpose="package",
        purpose_id=order_id,
        invoice_id=invoice_id,
        status=PaymentStatus.pending,
    )
    session.add(payment_row)
    await session.flush()

    try:
        data = await yk.create_payment(
            Decimal(amount),
            description,
            {"purpose": "package", "purpose_id": str(order_id), "invoice_id": invoice_id},
            idempotence_key=invoice_id,
        )
    except Exception:
        await session.rollback()
        logger.exception("YooKassa payment creation failed for package %s", order_id)
        raise PaymentCreationError from None

    payment_row.provider_payment_id = data.get("id")
    payment_row.confirmation_url = yk.extract_confirmation_url(data)
    order = await session.scalar(select(PackageOrder).where(PackageOrder.id == order_id))
    if order:
        order.payment_id = payment_row.id
    await session.commit()
    return payment_row, payment_row.confirmation_url or ""


async def create_topup_payment(
    session: AsyncSession,
    user_id: int,
    amount: Decimal | int,
) -> tuple[Payment, str]:
    amount = Decimal(amount).quantize(Decimal("0.01"))
    yk = await _tenant_payment_provider(get_settings())
    invoice_id = f"hwls-topup-{user_id}-{uuid.uuid4().hex[:8]}"
    payment_row = Payment(
        user_id=user_id,
        amount=amount,
        purpose="topup",
        purpose_id=user_id,
        invoice_id=invoice_id,
        status=PaymentStatus.pending,
    )
    session.add(payment_row)
    await session.flush()

    try:
        data = await yk.create_payment(
            amount,
            f"HWLS пополнение баланса {amount:.2f} ₽",
            {"purpose": "topup", "purpose_id": str(user_id), "invoice_id": invoice_id},
            idempotence_key=invoice_id,
        )
    except Exception:
        await session.rollback()
        logger.exception("YooKassa topup failed for user %s", user_id)
        raise PaymentCreationError from None

    payment_row.provider_payment_id = data.get("id")
    payment_row.confirmation_url = yk.extract_confirmation_url(data)
    await session.commit()
    return payment_row, payment_row.confirmation_url or ""


async def pay_subscription_from_balance(
    session: AsyncSession,
    user_id: int,
    subscription_id: int,
    amount: int,
) -> str:
    user = await get_user_for_update(session, user_id)
    if not user:
        raise PaymentCreationError("User not found")

    sub = await session.scalar(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.user_id == user_id,
            # `expired` / `active` — продление подписки («🔄 Продлить»).
            Subscription.status.in_(
                (SubscriptionStatus.pending_payment, SubscriptionStatus.expired, SubscriptionStatus.active),
            ),
            Subscription.deleted_at.is_(None),
        ),
    )
    if not sub:
        raise PaymentCreationError("Subscription not found")

    balance = user.balance or Decimal("0")
    price = Decimal(amount)
    if balance < price:
        raise InsufficientBalanceError()

    user.balance = balance - price
    payment_row = Payment(
        user_id=user_id,
        amount=price,
        purpose="subscription",
        purpose_id=subscription_id,
        invoice_id=f"hwls-bal-sub-{subscription_id}-{uuid.uuid4().hex[:8]}",
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(UTC),
    )
    session.add(payment_row)
    await session.flush()
    sub.payment_id = payment_row.id
    await activate_subscription(session, subscription_id)
    from app.services.b2b import on_corp_subscription_activated

    await on_corp_subscription_activated(session, user_id, subscription_id)
    await session.commit()
    return "subscription_activated"


async def pay_package_from_balance(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    amount: int,
) -> str:
    user = await get_user_for_update(session, user_id)
    if not user:
        raise PaymentCreationError("User not found")

    order = await session.scalar(
        select(PackageOrder).where(
            PackageOrder.id == order_id,
            PackageOrder.user_id == user_id,
            PackageOrder.status == "pending_payment",
        ),
    )
    if not order:
        raise PaymentCreationError("Package order not found")

    balance = user.balance or Decimal("0")
    price = Decimal(amount)
    if balance < price:
        raise InsufficientBalanceError()

    user.balance = balance - price
    payment_row = Payment(
        user_id=user_id,
        amount=price,
        purpose="package",
        purpose_id=order_id,
        invoice_id=f"hwls-bal-pkg-{order_id}-{uuid.uuid4().hex[:8]}",
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(UTC),
    )
    session.add(payment_row)
    await session.flush()
    order.payment_id = payment_row.id
    await activate_package(session, order_id)
    await session.commit()
    from app.core.texts import PACKAGE_TEXT_PENDING
    from app.services.publish import enqueue_package_order

    if order.text and order.text != PACKAGE_TEXT_PENDING:
        await enqueue_package_order(order_id)
        return "package_queued"
    return "package_needs_text"


async def mark_payment_succeeded(session: AsyncSession, payment: Payment) -> str:
    payment.status = PaymentStatus.succeeded
    payment.paid_at = datetime.now(UTC)

    if payment.purpose == "topup":
        user = await get_user_for_update(session, payment.user_id)
        if user:
            user.balance = (user.balance or Decimal("0")) + payment.amount
        await session.commit()
        return "balance_topped_up"

    if payment.purpose == "subscription":
        await activate_subscription(session, payment.purpose_id)
        from app.services.b2b import on_corp_subscription_activated

        await on_corp_subscription_activated(session, payment.user_id, payment.purpose_id)
        await session.commit()
        return "subscription_activated"

    if payment.purpose == "franchise":
        partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == payment.purpose_id))
        if not partner:
            await session.commit()
            return "franchise_partner_missing"
        tier = payment.invoice_id.split("-")[-2]
        if tier not in {"basic", "standard", "premium"}:
            tier = "basic"
        from app.services.franchise_billing import _next_month
        partner.franchise_tier = tier
        if payment.payment_method_id and payment.save_payment_method:
            partner.franchise_payment_method_id = payment.payment_method_id
        partner.franchise_next_charge_at = _next_month(datetime.now(UTC))
        partner.franchise_grace_until = None
        partner.franchise_last_notice_at = None
        if partner.bot_username is None:
            # First-ever payment for this partner: the clone bot doesn't exist
            # yet (ТЗ «Оплата -> Создание бота») — nothing to launch, the owner
            # still has to send a @BotFather token.
            partner.franchise_billing_status = "awaiting_token"
            await session.commit()
            return "franchise_awaiting_token"
        partner.active = True
        partner.franchise_billing_status = "active" if partner.franchise_payment_method_id else "manual"
        await session.commit()
        return "franchise_activated"

    if payment.purpose == "package":
        activated = await activate_package(session, payment.purpose_id)
        await session.commit()
        if activated:
            from app.core.texts import PACKAGE_TEXT_PENDING
            from app.services.publish import enqueue_package_order

            order = await session.scalar(select(PackageOrder).where(PackageOrder.id == payment.purpose_id))
            if order and order.text and order.text != PACKAGE_TEXT_PENDING:
                await enqueue_package_order(payment.purpose_id)
                return "package_queued"
            return "package_needs_text"
        return "package_order_missing"

    await session.commit()
    return "unknown"


async def activate_subscription_whitelist(
    session: AsyncSession,
    user_id: int,
    subscription_id: int,
) -> None:
    """Активация подписки без оплаты (whitelist)."""
    sub = await session.scalar(select(Subscription).where(Subscription.id == subscription_id))
    if not sub:
        return
    starts, ends = _next_period(sub)
    sub.status = SubscriptionStatus.active
    sub.starts_at = starts
    sub.expires_at = ends
    sub.total_price = 0
    # Бесплатное продление идёт через эту же активацию — у нового периода
    # должны сработать напоминание за сутки и уведомление об окончании.
    sub.reminded_at = None
    sub.expired_notified_at = None

    payment_row = Payment(
        user_id=user_id,
        amount=Decimal(0),
        purpose="subscription",
        purpose_id=subscription_id,
        invoice_id=f"hwls-wl-{subscription_id}-{uuid.uuid4().hex[:8]}",
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(UTC),
    )
    session.add(payment_row)
    await session.flush()
    sub.payment_id = payment_row.id
    await session.commit()
