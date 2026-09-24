from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.entities import Subscription, SubscriptionStatus, User, WhitelistEntry
from app.services.whitelist import WHITELIST_CATEGORY, provision_whitelist_subscription, revoke_whitelist_subscription
from tests.conftest_helpers import as_utc


@pytest.mark.asyncio
async def test_provision_whitelist_uses_custom_days(db_session) -> None:
    from app.models.entities import Chat, City

    city = City(key="msk", label="Москва")
    db_session.add(city)
    await db_session.flush()
    chat = Chat(city_id=city.id, title="C", telegram_username="c", telegram_chat_id=-1001)
    db_session.add(chat)
    user = User(telegram_id=333, username="wluser", full_name="WL")
    db_session.add(user)
    await db_session.commit()

    entry = WhitelistEntry(telegram_id=333, username="wluser", city_keys='["msk"]', chat_ids="[]", access_days=7)
    db_session.add(entry)
    await db_session.commit()

    ok, reason, sub_id = await provision_whitelist_subscription(db_session, entry, days=7)
    assert ok is True
    assert sub_id is not None
    assert reason in {"created", "activated_pending", "synced"}

    sub = await db_session.scalar(select(Subscription).where(Subscription.id == sub_id))
    assert sub is not None
    assert sub.status == SubscriptionStatus.active
    assert sub.category_code == WHITELIST_CATEGORY
    assert sub.expires_at is not None
    delta = as_utc(sub.expires_at) - datetime.now(UTC)
    assert 6 <= delta.days <= 7


@pytest.mark.asyncio
async def test_revoke_whitelist_expires_only_wl_active(db_session) -> None:
    user = User(telegram_id=444, username="mix", full_name="Mix")
    db_session.add(user)
    await db_session.flush()

    wl_sub = Subscription(
        user_id=user.id,
        category_code=WHITELIST_CATEGORY,
        approved_text="wl",
        approved_text_hash="h1",
        contact="@mix",
        price_per_chat=0,
        total_price=0,
        status=SubscriptionStatus.active,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    paid_sub = Subscription(
        user_id=user.id,
        category_code="VACANCY",
        approved_text="paid",
        approved_text_hash="h2",
        contact="@mix",
        price_per_chat=1000,
        total_price=1000,
        status=SubscriptionStatus.active,
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add_all([wl_sub, paid_sub])
    await db_session.commit()

    entry = WhitelistEntry(telegram_id=444, username="mix", access_days=7)
    count = await revoke_whitelist_subscription(db_session, entry)
    assert count == 1

    await db_session.refresh(wl_sub)
    await db_session.refresh(paid_sub)
    assert wl_sub.status == SubscriptionStatus.expired
    assert paid_sub.status == SubscriptionStatus.active


@pytest.mark.asyncio
async def test_revoke_by_username(db_session) -> None:
    user = User(telegram_id=555, username="byname", full_name="N")
    db_session.add(user)
    await db_session.flush()
    sub = Subscription(
        user_id=user.id,
        category_code=WHITELIST_CATEGORY,
        approved_text="wl",
        approved_text_hash="h",
        contact="@byname",
        price_per_chat=0,
        total_price=0,
        status=SubscriptionStatus.active,
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    db_session.add(sub)
    await db_session.commit()

    entry = WhitelistEntry(telegram_id=None, username="byname", access_days=3)
    count = await revoke_whitelist_subscription(db_session, entry)
    assert count == 1
    await db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.expired
