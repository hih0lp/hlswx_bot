"""Действия администратора над публикациями (ТЗ этапа 2, 6.9).

«Опубликовать сейчас» (вне очереди), «Отменить», «Повторить», «Удалить из
очереди». До этапа 2 сервисного слоя для этого не было.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.entities import PublishEvent, PublishJob
from app.services import queue_admin
from app.services.oneshot import register_oneshot_publication
from app.services.publish import process_publish_queue
from app.services.queue_slots import PRIORITY_ONESHOT
from tests.conftest import jobs_for_chat, now_utc

ADMIN = 777


async def _jobs():
    async with SessionLocal() as session:
        return list((await session.scalars(select(PublishJob).order_by(PublishJob.id))).all())


async def _events(event: str):
    async with SessionLocal() as session:
        return list((await session.scalars(select(PublishEvent).where(PublishEvent.event == event))).all())


async def test_publish_now_bypasses_group_silence(bot, world, enqueue):
    """Группа в тишине, но администратор публикует вне очереди — пост выходит."""
    await enqueue("sub1")
    t0 = now_utc()
    async with SessionLocal() as session:
        await register_oneshot_publication(
            session, telegram_chat_id=world.chat_tg_ids[0], network="hammer",
            message_id=1, published_at=t0,
        )

    # На обычном приоритете пост ждал бы 20 минут.
    await process_publish_queue(bot, now=t0 + timedelta(minutes=1))
    assert [j.status for j in await jobs_for_chat(world.chat_ids[0])] == ["queued"]

    job = (await jobs_for_chat(world.chat_ids[0]))[0]
    await queue_admin.publish_now(job.id, actor_telegram_id=ADMIN)

    await process_publish_queue(bot, now=t0 + timedelta(minutes=2))
    assert [j.status for j in await jobs_for_chat(world.chat_ids[0])] == ["published"]


async def test_publish_now_raises_priority_and_logs(bot, world, enqueue):
    await enqueue("sub1")
    job = (await _jobs())[0]

    updated = await queue_admin.publish_now(job.id, actor_telegram_id=ADMIN)
    assert updated.priority == PRIORITY_ONESHOT
    assert updated.status == "queued"
    assert any(f"admin={ADMIN}" in (e.detail or "") for e in await _events("queued"))


async def test_cancel_job(bot, world, enqueue):
    await enqueue("sub1")
    job = (await _jobs())[0]

    updated = await queue_admin.cancel_job(job.id, actor_telegram_id=ADMIN)
    assert updated.status == "cancelled"
    assert str(ADMIN) in updated.error
    assert await _events("cancelled")

    # Отменённая задача воркером не берётся.
    await process_publish_queue(bot, now=now_utc())
    assert (await _jobs())[0].status == "cancelled"


async def test_cannot_cancel_published(bot, world, enqueue):
    await enqueue("sub1")
    t0 = now_utc()
    await process_publish_queue(bot, now=t0)
    job = (await _jobs())[0]
    assert job.status == "published"

    with pytest.raises(queue_admin.JobActionError):
        await queue_admin.cancel_job(job.id, actor_telegram_id=ADMIN)


async def test_retry_failed_job_resets_attempts(bot, world, enqueue):
    """Повтор возвращает задачу в очередь и обнуляет счётчик попыток."""
    bot.fail_chats[world.chat_tg_ids[0]] = "Forbidden: bot was kicked from the supergroup chat"
    await enqueue("sub1")
    await process_publish_queue(bot, now=now_utc())

    job = (await jobs_for_chat(world.chat_ids[0]))[0]
    assert job.status == "failed"
    assert job.attempts > 0

    bot.fail_chats.clear()
    updated = await queue_admin.retry_job(job.id, actor_telegram_id=ADMIN)
    assert updated.status == "queued"
    assert updated.attempts == 0
    assert updated.error is None

    await process_publish_queue(bot, now=now_utc())
    assert (await jobs_for_chat(world.chat_ids[0]))[0].status == "published"


async def test_retry_rejects_queued_job(bot, world, enqueue):
    await enqueue("sub1")
    job = (await _jobs())[0]
    with pytest.raises(queue_admin.JobActionError):
        await queue_admin.retry_job(job.id, actor_telegram_id=ADMIN)


async def test_retry_all_failed(bot, world, enqueue):
    for chat_tg_id in world.chat_tg_ids:
        bot.fail_chats[chat_tg_id] = "Forbidden: bot was kicked from the supergroup chat"
    await enqueue("sub1")
    await process_publish_queue(bot, now=now_utc())
    assert all(j.status == "failed" for j in await _jobs())

    bot.fail_chats.clear()
    assert await queue_admin.retry_all_failed(actor_telegram_id=ADMIN) == 3
    assert all(j.status == "queued" for j in await _jobs())


async def test_unknown_job():
    with pytest.raises(queue_admin.JobActionError):
        await queue_admin.cancel_job(999999, actor_telegram_id=ADMIN)
