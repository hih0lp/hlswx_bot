from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.entities import Payment, PaymentStatus, Subscription, SubscriptionStatus, User
from app.services.fraud import subscription_period
from app.services.payments import activate_subscription, activate_subscription_whitelist, mark_payment_succeeded
from tests.conftest_helpers import as_utc


@pytest.mark.asyncio
async def test_subscription_period_respects_days() -> None:
    start, end = subscription_period(14)
    assert (end - start).days == 14
    start, end = subscription_period(0)
    assert (end - start).days == 1


@pytest.mark.asyncio
async def test_activate_subscription_sets_active(db_session) -> None:
    user = User(telegram_id=666, username="p", full_name="P")
    db_session.add(user)
    await db_session.flush()
    sub = Subscription(
        user_id=user.id,
        category_code="VACANCY",
        approved_text="t",
        approved_text_hash="h",
        contact="@p",
        price_per_chat=1000,
        total_price=1000,
        status=SubscriptionStatus.pending_payment,
    )
    db_session.add(sub)
    await db_session.commit()

    ok = await activate_subscription(db_session, sub.id)
    assert ok is True
    assert sub.status == SubscriptionStatus.active
    assert sub.starts_at is not None
    assert sub.expires_at is not None
    assert as_utc(sub.expires_at) > datetime.now(UTC)


@pytest.mark.asyncio
async def test_activate_whitelist_custom_days(db_session) -> None:
    user = User(telegram_id=777, username="wl", full_name="WL")
    db_session.add(user)
    await db_session.flush()
    sub = Subscription(
        user_id=user.id,
        category_code="WHITELIST",
        approved_text="wl",
        approved_text_hash="h",
        contact="@wl",
        price_per_chat=0,
        total_price=0,
        status=SubscriptionStatus.pending_payment,
    )
    db_session.add(sub)
    await db_session.commit()

    await activate_subscription_whitelist(db_session, user.id, sub.id, days=5)
    await db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
    assert sub.total_price == 0
    delta = as_utc(sub.expires_at) - datetime.now(UTC)
    assert 4 <= delta.days <= 5


@pytest.mark.asyncio
async def test_mark_payment_succeeded_subscription(db_session) -> None:
    user = User(telegram_id=888, username="pay", full_name="Pay")
    db_session.add(user)
    await db_session.flush()
    sub = Subscription(
        user_id=user.id,
        category_code="VACANCY",
        approved_text="t",
        approved_text_hash="h",
        contact="@pay",
        price_per_chat=500,
        total_price=500,
        status=SubscriptionStatus.pending_payment,
    )
    db_session.add(sub)
    await db_session.flush()
    payment = Payment(
        user_id=user.id,
        amount=Decimal("500"),
        purpose="subscription",
        purpose_id=sub.id,
        invoice_id="inv-test-1",
        status=PaymentStatus.pending,
    )
    db_session.add(payment)
    await db_session.commit()

    with patch("app.services.b2b.on_corp_subscription_activated", new=AsyncMock()):
        result = await mark_payment_succeeded(db_session, payment)

    assert result == "subscription_activated"
    await db_session.refresh(sub)
    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.succeeded
    assert sub.status == SubscriptionStatus.active


@pytest.mark.asyncio
async def test_mysubs_excludes_pending_payment(db_session) -> None:
    user = User(telegram_id=999, username="list", full_name="List")
    db_session.add(user)
    await db_session.flush()
    active = Subscription(
        user_id=user.id,
        category_code="VACANCY",
        approved_text="a",
        approved_text_hash="h1",
        contact="@list",
        price_per_chat=500,
        total_price=500,
        status=SubscriptionStatus.active,
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    pending = Subscription(
        user_id=user.id,
        category_code="VACANCY",
        approved_text="p",
        approved_text_hash="h2",
        contact="@list",
        price_per_chat=500,
        total_price=500,
        status=SubscriptionStatus.pending_payment,
    )
    db_session.add_all([active, pending])
    await db_session.commit()

    visible = list(
        (
            await db_session.scalars(
                select(Subscription)
                .where(
                    Subscription.user_id == user.id,
                    Subscription.status != SubscriptionStatus.pending_payment,
                )
                .order_by(Subscription.id.desc()),
            )
        ).all(),
    )
    assert len(visible) == 1
    assert visible[0].status == SubscriptionStatus.active


@pytest.mark.asyncio
async def test_publish_active_subs_query(db_session) -> None:
    from sqlalchemy import or_

    user = User(telegram_id=1001, username="pub", full_name="Pub")
    db_session.add(user)
    await db_session.flush()
    now = datetime.now(UTC)
    active = Subscription(
        user_id=user.id,
        category_code="VACANCY",
        approved_text="a",
        approved_text_hash="h",
        contact="@pub",
        price_per_chat=500,
        total_price=500,
        status=SubscriptionStatus.active,
        expires_at=now + timedelta(days=1),
    )
    expired = Subscription(
        user_id=user.id,
        category_code="VACANCY",
        approved_text="e",
        approved_text_hash="h2",
        contact="@pub",
        price_per_chat=500,
        total_price=500,
        status=SubscriptionStatus.active,
        expires_at=now - timedelta(days=1),
    )
    db_session.add_all([active, expired])
    await db_session.commit()

    subs = list(
        (
            await db_session.scalars(
                select(Subscription)
                .where(
                    Subscription.user_id == user.id,
                    Subscription.status == SubscriptionStatus.active,
                    or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
                ),
            )
        ).all(),
    )
    assert len(subs) == 1
    assert subs[0].approved_text == "a"
