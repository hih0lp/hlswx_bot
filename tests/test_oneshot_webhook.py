"""Тишина от разового поста из Hammer/W (ТЗ 6.6, критерий приёмки 3)."""

from __future__ import annotations

from datetime import timedelta

from app.db.session import SessionLocal
from app.services.oneshot import register_oneshot_publication
from app.services.publish import process_publish_queue
from tests.conftest import jobs_for_chat, now_utc, slot_for

SILENCE = timedelta(minutes=20)


async def _register(chat_tg_id: int, at, message_id: int | None = 100, **kwargs):
    async with SessionLocal() as session:
        return await register_oneshot_publication(
            session,
            telegram_chat_id=chat_tg_id,
            network="hammer",
            message_id=message_id,
            published_at=at,
            **kwargs,
        )


async def test_oneshot_sets_silence_for_target_chat_only(world):
    """Тишина действует по конкретной группе, а не глобально по всем (ТЗ 6.4)."""
    t0 = now_utc()
    result = await _register(world.chat_tg_ids[0], t0)

    assert result["ok"] is True
    assert (await slot_for(world.chat_ids[0])).locked_until == t0 + SILENCE
    assert await slot_for(world.chat_ids[1]) is None
    assert await slot_for(world.chat_ids[2]) is None


async def test_oneshot_is_idempotent(world):
    """Повторная доставка того же события не двигает окно тишины."""
    t0 = now_utc()
    await _register(world.chat_tg_ids[0], t0, message_id=555)
    first = (await slot_for(world.chat_ids[0])).locked_until

    repeat = await _register(world.chat_tg_ids[0], t0 + timedelta(minutes=7), message_id=555)

    assert repeat["duplicate"] is True
    assert (await slot_for(world.chat_ids[0])).locked_until == first


async def test_new_oneshot_recalculates_window(world):
    """Каждый новый разовый пост считает тишину заново от своего времени."""
    t0 = now_utc()
    await _register(world.chat_tg_ids[0], t0, message_id=1)
    await _register(world.chat_tg_ids[0], t0 + timedelta(minutes=5), message_id=2)

    assert (await slot_for(world.chat_ids[0])).locked_until == t0 + timedelta(minutes=25)


async def test_oneshot_defers_but_does_not_drop_pending_posts(bot, world, enqueue):
    """Посты, поступившие до тишины, не теряются, а переносятся на её окончание."""
    await enqueue("sub1")
    t0 = now_utc()

    await _register(world.chat_tg_ids[0], t0)

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert [j.status for j in chat_jobs] == ["queued"]
    assert chat_jobs[0].scheduled_at == t0 + SILENCE

    await process_publish_queue(bot, now=t0 + SILENCE)
    assert [j.status for j in await jobs_for_chat(world.chat_ids[0])] == ["published"]


async def test_oneshot_accepts_username(world):
    """Группу можно указать username-ом, если Hammer не передал chat_id."""
    t0 = now_utc()
    async with SessionLocal() as session:
        result = await register_oneshot_publication(
            session, telegram_username="@group_b", message_id=7, published_at=t0,
        )
    assert result["ok"] is True
    assert (await slot_for(world.chat_ids[1])).locked_until == t0 + SILENCE


async def test_oneshot_unknown_chat(world):
    async with SessionLocal() as session:
        result = await register_oneshot_publication(
            session, telegram_chat_id=-100_999_999, message_id=1,
        )
    assert result == {"ok": False, "error": "chat_not_found"}


async def test_manual_silence_uses_same_mechanics(world):
    """Ручная тишина администратора ставит то же окно (фолбэк, пока нет вебхука)."""
    t0 = now_utc()
    async with SessionLocal() as session:
        result = await register_oneshot_publication(
            session,
            telegram_chat_id=world.chat_tg_ids[0],
            network="manual",
            published_at=t0,
            minutes=30,
        )
    assert result["ok"] is True
    assert (await slot_for(world.chat_ids[0])).locked_until == t0 + timedelta(minutes=30)
