"""Настройки очереди из панели (ТЗ этапа 2, 6.10 и 7).

Главное требование — «немедленно влияет на работу очереди, реализованной в
этапе 1», то есть **без перезапуска бота**. Поэтому здесь не проверка
чтения-записи, а сценарий очереди на том же процессе: меняем значение и
смотрим, что следующая публикация подчиняется новому.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.session import SessionLocal
from app.services import settings_store
from app.services.oneshot import register_oneshot_publication
from app.services.publish import process_publish_queue
from tests.conftest import jobs_for_chat, now_utc, slot_for


@pytest.fixture(autouse=True)
async def _clean_settings():
    await settings_store.refresh()
    yield
    await settings_store.refresh()


async def test_defaults_come_from_env():
    """Пока настройку не трогали, значение то же, что было в этапе 1."""
    assert await settings_store.queue_silence_minutes() == 20
    assert await settings_store.queue_user_pause_sec() == 120


async def test_set_and_read_back():
    await settings_store.set_int(settings_store.QUEUE_SILENCE_MINUTES, 30, by_telegram_id=111)
    assert await settings_store.queue_silence_minutes() == 30


async def test_unknown_key_rejected():
    with pytest.raises(KeyError):
        await settings_store.set_int("queue.nonsense", 5)


async def test_broken_value_falls_back_to_env():
    """Мусор в БД не должен ронять очередь — откатываемся на значение из .env."""
    from app.models.entities import AppSetting

    async with SessionLocal() as session:
        session.add(AppSetting(key=settings_store.QUEUE_SILENCE_MINUTES, value="двадцать"))
        await session.commit()
    await settings_store.refresh()

    assert await settings_store.queue_silence_minutes() == 20


async def test_silence_change_applies_without_restart(bot, world, enqueue):
    """Тихий режим сокращён до 10 минут — пост подписчика выходит на 10-й минуте.

    На дефолтных 20 минутах он бы ещё ждал.
    """
    await settings_store.set_int(settings_store.QUEUE_SILENCE_MINUTES, 10)

    chat_tg_id = world.chat_tg_ids[0]
    t0 = now_utc()
    async with SessionLocal() as session:
        await register_oneshot_publication(
            session, telegram_chat_id=chat_tg_id, network="hammer", message_id=1, published_at=t0,
        )

    slot = await slot_for(world.chat_ids[0])
    assert slot.locked_until == t0 + timedelta(minutes=10)

    await enqueue("sub1")
    await process_publish_queue(bot, now=t0 + timedelta(minutes=5))
    assert [j.status for j in await jobs_for_chat(world.chat_ids[0])] == ["queued"]

    await process_publish_queue(bot, now=t0 + timedelta(minutes=10, seconds=1))
    assert [j.status for j in await jobs_for_chat(world.chat_ids[0])] == ["published"]


async def test_pause_change_applies_without_restart(bot, world, enqueue):
    """Интервал между постами разных пользователей увеличен до 5 минут."""
    await settings_store.set_int(settings_store.QUEUE_USER_PAUSE_SEC, 300)

    # t0 берём после постановки в очередь: scheduled_at пишется по реальным
    # часам, и задача, поставленная «позже» t0, на первом тике не видна.
    await enqueue("sub1")
    t0 = now_utc()
    await process_publish_queue(bot, now=t0)

    await enqueue("sub2")
    # На дефолтных двух минутах второй пост бы уже вышел.
    await process_publish_queue(bot, now=t0 + timedelta(minutes=3))
    statuses = [j.status for j in await jobs_for_chat(world.chat_ids[0])]
    assert statuses == ["published", "queued"]

    await process_publish_queue(bot, now=t0 + timedelta(minutes=5, seconds=1))
    statuses = [j.status for j in await jobs_for_chat(world.chat_ids[0])]
    assert statuses == ["published", "published"]
