from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.entities import Chat, ChatPublishLock, City, PublishJob, Subscription, SubscriptionChat, SubscriptionStatus, User
from app.services.publish import (
    PRIORITY_PACKAGE,
    PRIORITY_SUBSCRIPTION,
    PRIORITY_WHITELIST,
    _batch_jobs,
    _set_chat_lock,
    enqueue_subscription_post,
    process_publish_queue,
    render_post,
)
from app.services.whitelist import WHITELIST_CATEGORY
from tests.conftest_helpers import as_utc


@pytest.mark.asyncio
async def test_render_post_adds_contact() -> None:
    text = render_post("Текст", "@user")
    assert "Текст" in text
    assert "@user" in text


@pytest.mark.asyncio
async def test_enqueue_subscription_same_time_all_chats(db_session) -> None:
    city = City(key="msk", label="Москва")
    db_session.add(city)
    await db_session.flush()

    chat1 = Chat(city_id=city.id, title="C1", telegram_username="c1", telegram_chat_id=-1001, sort_order=1)
    chat2 = Chat(city_id=city.id, title="C2", telegram_username="c2", telegram_chat_id=-1002, sort_order=2)
    db_session.add_all([chat1, chat2])
    user = User(telegram_id=111, username="u1", full_name="U1")
    db_session.add(user)
    await db_session.flush()

    sub = Subscription(
        user_id=user.id,
        category_code="VACANCY",
        approved_text="x",
        approved_text_hash="h",
        contact="@u1",
        price_per_chat=1000,
        total_price=2000,
        status=SubscriptionStatus.active,
    )
    db_session.add(sub)
    await db_session.flush()
    db_session.add_all(
        [
            SubscriptionChat(subscription_id=sub.id, chat_id=chat1.id),
            SubscriptionChat(subscription_id=sub.id, chat_id=chat2.id),
        ],
    )
    await db_session.commit()

    with patch("app.services.volume_limits.log_publication", new=AsyncMock()):
        count = await enqueue_subscription_post(db_session, sub.id, "post", "@u1")

    assert count == 2
    jobs = list((await db_session.scalars(select(PublishJob))).all())
    assert len(jobs) == 2
    assert jobs[0].scheduled_at == jobs[1].scheduled_at
    assert all(j.priority == PRIORITY_SUBSCRIPTION for j in jobs)


@pytest.mark.asyncio
async def test_enqueue_whitelist_lower_priority_than_paid(db_session) -> None:
    city = City(key="msk", label="Москва")
    db_session.add(city)
    await db_session.flush()
    chat = Chat(city_id=city.id, title="C1", telegram_username="c1", telegram_chat_id=-1001)
    db_session.add(chat)
    user = User(telegram_id=222, username="wl", full_name="WL")
    db_session.add(user)
    await db_session.flush()

    sub = Subscription(
        user_id=user.id,
        category_code=WHITELIST_CATEGORY,
        approved_text="wl",
        approved_text_hash="h",
        contact="@wl",
        price_per_chat=0,
        total_price=0,
        status=SubscriptionStatus.active,
    )
    db_session.add(sub)
    await db_session.flush()
    db_session.add(SubscriptionChat(subscription_id=sub.id, chat_id=chat.id))
    await db_session.commit()

    with patch("app.services.volume_limits.log_publication", new=AsyncMock()):
        await enqueue_subscription_post(db_session, sub.id, "wl post", "@wl")

    job = await db_session.scalar(select(PublishJob))
    assert job is not None
    assert job.priority == PRIORITY_WHITELIST
    assert PRIORITY_WHITELIST > PRIORITY_SUBSCRIPTION > PRIORITY_PACKAGE


@pytest.mark.asyncio
async def test_batch_jobs_groups_by_subscription_text(db_session) -> None:
    now = datetime.now(UTC)
    j1 = PublishJob(subscription_id=1, chat_id=1, text="same", contact="c", status="queued", scheduled_at=now)
    j2 = PublishJob(subscription_id=1, chat_id=2, text="same", contact="c", status="queued", scheduled_at=now)
    j3 = PublishJob(subscription_id=1, chat_id=3, text="other", contact="c", status="queued", scheduled_at=now)
    db_session.add_all([j1, j2, j3])
    await db_session.commit()

    batch = await _batch_jobs(db_session, j1)
    assert len(batch) == 2
    assert {job.text for job in batch} == {"same"}


@pytest.mark.asyncio
async def test_process_queue_waits_on_chat_lock(db_session, session_local_factory) -> None:
    now = datetime.now(UTC)
    lock_until = now + timedelta(minutes=5)
    city = City(key="msk", label="Москва")
    db_session.add(city)
    await db_session.flush()
    chat = Chat(city_id=city.id, title="C", telegram_username="c", telegram_chat_id=-1001)
    db_session.add(chat)
    await db_session.flush()
    job = PublishJob(
        subscription_id=1,
        chat_id=chat.id,
        text="t",
        contact="c",
        priority=PRIORITY_SUBSCRIPTION,
        status="queued",
        scheduled_at=now,
    )
    db_session.add(job)
    await db_session.commit()

    bot = MagicMock()
    relay = AsyncMock()

    with (
        patch("app.services.publish._chat_lock_until", AsyncMock(return_value=lock_until)),
        patch("app.services.hammer_relay.relay_publish", relay),
    ):
        await process_publish_queue(bot)

    await db_session.refresh(job)
    assert job.status == "queued"
    assert as_utc(job.scheduled_at) == lock_until
    relay.assert_not_called()


@pytest.mark.asyncio
async def test_process_queue_subscription_sets_short_lock(db_session, session_local_factory) -> None:
    now = datetime.now(UTC)
    city = City(key="msk", label="Москва")
    db_session.add(city)
    await db_session.flush()
    chat = Chat(city_id=city.id, title="C", telegram_username="c", telegram_chat_id=-1001)
    db_session.add(chat)
    await db_session.flush()
    job = PublishJob(
        subscription_id=1,
        chat_id=chat.id,
        text="t",
        contact="c",
        priority=PRIORITY_SUBSCRIPTION,
        status="queued",
        scheduled_at=now,
    )
    db_session.add(job)
    await db_session.commit()

    bot = MagicMock()

    class RelayOk:
        ok = True
        message_id = 99

    with (
        patch("app.services.hammer_relay.relay_publish", AsyncMock(return_value=RelayOk())),
        patch("app.services.publish._notify_publish_done", new=AsyncMock()),
    ):
        await process_publish_queue(bot)

    lock = await db_session.scalar(select(ChatPublishLock).where(ChatPublishLock.chat_id == chat.id))
    assert lock is not None
    assert as_utc(lock.locked_until) > now
    assert as_utc(lock.locked_until) <= now + timedelta(minutes=3)


@pytest.mark.asyncio
async def test_process_queue_package_sets_longer_lock(db_session, session_local_factory) -> None:
    now = datetime.now(UTC)
    city = City(key="msk", label="Москва")
    db_session.add(city)
    await db_session.flush()
    chat = Chat(city_id=city.id, title="C", telegram_username="c", telegram_chat_id=-1001)
    db_session.add(chat)
    await db_session.flush()
    job = PublishJob(
        package_order_id=9,
        chat_id=chat.id,
        text="pkg",
        contact="c",
        priority=PRIORITY_PACKAGE,
        status="queued",
        scheduled_at=now,
    )
    db_session.add(job)
    await db_session.commit()

    bot = MagicMock()

    class RelayOk:
        ok = True
        message_id = 1

    with (
        patch("app.services.hammer_relay.relay_publish", AsyncMock(return_value=RelayOk())),
        patch("app.services.publish._notify_publish_done", new=AsyncMock()),
    ):
        await process_publish_queue(bot)

    lock = await db_session.scalar(select(ChatPublishLock).where(ChatPublishLock.chat_id == chat.id))
    assert lock is not None
    assert as_utc(lock.locked_until) >= now + timedelta(minutes=19)


@pytest.mark.asyncio
async def test_set_chat_lock_extends_existing(db_session) -> None:
    city = City(key="msk", label="Москва")
    db_session.add(city)
    await db_session.flush()
    chat = Chat(city_id=city.id, title="C", telegram_username="c", telegram_chat_id=-1001)
    db_session.add(chat)
    await db_session.flush()

    first = datetime.now(UTC) + timedelta(minutes=1)
    db_session.add(ChatPublishLock(chat_id=chat.id, locked_until=first))
    await db_session.commit()

    await _set_chat_lock(db_session, chat.id, 10)
    lock = await db_session.scalar(select(ChatPublishLock).where(ChatPublishLock.chat_id == chat.id))
    assert lock is not None
    assert as_utc(lock.locked_until) > as_utc(first)
