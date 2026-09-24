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
)
from app.services.fraud import subscription_period
from app.services.wallet import InsufficientBalanceError, get_user_for_update
from app.services.yookassa import YooKassaService

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


async def activate_subscription(session: AsyncSession, subscription_id: int) -> bool:
    sub = await session.scalar(select(Subscription).where(Subscription.id == subscription_id))
    if not sub:
        return False
    starts, ends = subscription_period()
    sub.status = SubscriptionStatus.active
    sub.starts_at = starts
    sub.expires_at = ends
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
    yk = YooKassaService(get_settings())
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
    yk = YooKassaService(get_settings())
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
    amount: int,
) -> tuple[Payment, str]:
    yk = YooKassaService(get_settings())
    invoice_id = f"hwls-topup-{user_id}-{uuid.uuid4().hex[:8]}"
    payment_row = Payment(
        user_id=user_id,
        amount=Decimal(amount),
        purpose="topup",
        purpose_id=user_id,
        invoice_id=invoice_id,
        status=PaymentStatus.pending,
    )
    session.add(payment_row)
    await session.flush()

    try:
        data = await yk.create_payment(
            Decimal(amount),
            f"HWLS пополнение баланса {amount} ₽",
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
            Subscription.status == SubscriptionStatus.pending_payment,
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
    starts, ends = subscription_period()
    sub.status = SubscriptionStatus.active
    sub.starts_at = starts
    sub.expires_at = ends
    sub.total_price = 0

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
