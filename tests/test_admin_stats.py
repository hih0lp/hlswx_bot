"""Показатели и выручка админ-панели (ТЗ этапа 2, 6.1, 6.2, 6.4).

Главное здесь — не арифметика, а две ловушки таблицы `payments`: бесплатная
активация по белому списку пишет успешный платёж на 0 ₽, а оплата с баланса
создаёт второй успешный платёж поверх пополнения. Если сложить всё подряд,
выручка удвоится.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.session import SessionLocal
from app.models.entities import Payment, PaymentStatus, Subscription, SubscriptionStatus, User
from app.services import admin_stats
from app.services.payments import (
    INVOICE_BALANCE_SUB,
    INVOICE_STARS,
    INVOICE_SUB,
    INVOICE_TOPUP,
    INVOICE_WHITELIST,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


async def _payment(invoice_prefix: str, amount, *, paid_at, status=PaymentStatus.succeeded, user_id=1):
    async with SessionLocal() as session:
        session.add(
            Payment(
                user_id=user_id,
                amount=Decimal(amount),
                purpose="subscription",
                purpose_id=1,
                invoice_id=f"{invoice_prefix}{paid_at.timestamp()}-{amount}",
                status=status,
                paid_at=paid_at,
            ),
        )
        await session.commit()


@pytest.fixture
async def user():
    async with SessionLocal() as session:
        row = User(telegram_id=9001, username="payer", full_name="payer")
        session.add(row)
        await session.commit()
        return row.id


async def test_card_payments_count_as_revenue(user):
    await _payment(INVOICE_SUB, 1200, paid_at=NOW - timedelta(hours=1), user_id=user)
    await _payment(INVOICE_TOPUP, 500, paid_at=NOW - timedelta(hours=2), user_id=user)
    await _payment(INVOICE_STARS, 300, paid_at=NOW - timedelta(hours=3), user_id=user)

    result = await admin_stats.revenue(admin_stats.PERIOD_TODAY, now=NOW)
    assert result.amount == Decimal(2000)
    assert result.payments == 3


async def test_balance_payment_is_not_counted_twice(user):
    """Пополнили баланс на 1000 и оплатили с него подписку — выручка 1000, не 2000."""
    await _payment(INVOICE_TOPUP, 1000, paid_at=NOW - timedelta(hours=2), user_id=user)
    await _payment(INVOICE_BALANCE_SUB, 1000, paid_at=NOW - timedelta(hours=1), user_id=user)

    result = await admin_stats.revenue(admin_stats.PERIOD_TODAY, now=NOW)
    assert result.amount == Decimal(1000)
    assert result.payments == 1


async def test_whitelist_activation_is_not_revenue(user):
    await _payment(INVOICE_WHITELIST, 0, paid_at=NOW - timedelta(hours=1), user_id=user)

    result = await admin_stats.revenue(admin_stats.PERIOD_TODAY, now=NOW)
    assert result.amount == Decimal(0)
    assert result.payments == 0


async def test_pending_payment_is_not_revenue(user):
    await _payment(INVOICE_SUB, 1200, paid_at=NOW, status=PaymentStatus.pending, user_id=user)

    assert (await admin_stats.revenue(admin_stats.PERIOD_TODAY, now=NOW)).amount == Decimal(0)


async def test_period_bounds_and_previous_period(user):
    await _payment(INVOICE_SUB, 1000, paid_at=NOW - timedelta(days=2), user_id=user)   # в 7 днях
    await _payment(INVOICE_SUB, 400, paid_at=NOW - timedelta(days=9), user_id=user)    # в предыдущих 7

    result = await admin_stats.revenue(admin_stats.PERIOD_7, now=NOW)
    assert result.amount == Decimal(1000)
    assert result.previous == Decimal(400)
    assert result.change_percent == 150


async def test_change_percent_is_none_without_previous(user):
    await _payment(INVOICE_SUB, 1000, paid_at=NOW - timedelta(hours=1), user_id=user)
    assert (await admin_stats.revenue(admin_stats.PERIOD_TODAY, now=NOW)).change_percent is None


async def test_all_time_period_has_no_lower_bound(user):
    await _payment(INVOICE_SUB, 700, paid_at=NOW - timedelta(days=300), user_id=user)
    result = await admin_stats.revenue(admin_stats.PERIOD_ALL, now=NOW)
    assert result.amount == Decimal(700)
    assert result.previous == Decimal(0)


async def test_new_subscriptions_versus_renewals(user):
    """Вторая подписка того же пользователя считается продлением."""
    async with SessionLocal() as session:
        for idx in range(2):
            session.add(
                Subscription(
                    user_id=user,
                    category_code="SHABASHKA",
                    approved_text="текст",
                    approved_text_hash="hash",
                    contact="@payer",
                    price_per_chat=500,
                    total_price=500,
                    status=SubscriptionStatus.active,
                    starts_at=NOW - timedelta(hours=2 - idx),
                    created_at=NOW - timedelta(days=10 - idx),
                ),
            )
        await session.commit()

    result = await admin_stats.revenue(admin_stats.PERIOD_TODAY, now=NOW)
    assert result.new_subscriptions == 1
    assert result.renewals == 1


async def test_dashboard_counts_publications_today(bot, world, enqueue):
    await enqueue("sub1")
    stats = await admin_stats.gather_dashboard_stats()
    assert stats["publications_today"] == 1
    assert stats["users"] == 3


async def test_payments_summary_shape(user):
    await _payment(INVOICE_SUB, 1200, paid_at=NOW - timedelta(hours=1), user_id=user)
    summary = await admin_stats.payments_summary(now=NOW)
    assert set(summary) == {"revenue_today", "paid", "processing", "failed", "awaiting_subs"}
    assert summary["paid"] == 1


async def test_publications_summary_shape(bot, world, enqueue):
    await enqueue("sub1")
    summary = await admin_stats.publications_summary()
    assert summary["queued"] == 3
    assert summary["failed"] == 0
