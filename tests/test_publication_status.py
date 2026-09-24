"""Уведомления о статусе поста и кнопки «Редактировать»/«Закрыть» (ТЗ 6.8)."""

from __future__ import annotations

from datetime import timedelta

from app.db.session import SessionLocal
from app.services.oneshot import register_oneshot_publication
from app.services.publication_status import (
    apply_publication_close,
    apply_publication_edit,
)
from app.services.publish import process_publish_queue
from tests.conftest import all_jobs, jobs_for_chat, now_utc, publication

SILENCE = timedelta(minutes=20)


async def _silence(chat_tg_id: int, at, message_id: int = 1):
    async with SessionLocal() as session:
        await register_oneshot_publication(
            session, telegram_chat_id=chat_tg_id, message_id=message_id, published_at=at,
        )


def _notifications(bot, world, who="sub1") -> list[str]:
    return bot.user_notifications(world.user_tg_ids[who])


async def test_notifies_published_with_action_buttons(bot, world, enqueue):
    """Очереди не было: пост опубликован сразу, под ним — «Редактировать» и «Закрыть»."""
    await enqueue("sub1")
    await process_publish_queue(bot, now=now_utc())

    notes = _notifications(bot, world)
    assert len(notes) == 1
    assert "опубликован" in notes[0].lower()

    keyboard = [m for m in bot.sent if m["chat_id"] == world.user_tg_ids["sub1"]][0]["reply_markup"]
    buttons = [b.text for row in keyboard.inline_keyboard for b in row]
    # Подписи кнопок — как во фрейме макета «Уведомление» (140:130).
    assert any("Изменить текст" in b for b in buttons)
    assert any("Закрыть объявление" in b for b in buttons)


async def test_notifies_queued_state(bot, world, enqueue):
    """Пост встал в очередь во всех группах — пользователь получает уведомление об очереди."""
    t0 = now_utc()
    for idx, chat_tg_id in enumerate(world.chat_tg_ids):
        await _silence(chat_tg_id, t0, message_id=idx + 1)

    await enqueue("sub1")
    await process_publish_queue(bot, now=t0 + timedelta(minutes=1))

    notes = _notifications(bot, world)
    assert len(notes) == 1, "уведомление об очереди должно быть отправлено"
    # Фрейм макета «Очередь» (146:86) списка групп не показывает — только
    # сам факт постановки в очередь и обещание сообщить о публикации.
    assert "добавлено в очередь" in notes[0].lower()

    pub = await publication(1)
    assert pub.status == "queued"
    assert pub.notified_state == "queued", "состояние уведомления должно быть зафиксировано"

    # ни одного сообщения в группы не ушло
    for chat_tg_id in world.chat_tg_ids:
        assert not bot.messages_to(chat_tg_id)


async def test_queued_notification_is_sent_once(bot, world, enqueue):
    """Повторные проходы воркера не спамят тем же уведомлением."""
    t0 = now_utc()
    for idx, chat_tg_id in enumerate(world.chat_tg_ids):
        await _silence(chat_tg_id, t0, message_id=idx + 1)

    await enqueue("sub1")
    await process_publish_queue(bot, now=t0 + timedelta(minutes=1))
    await process_publish_queue(bot, now=t0 + timedelta(minutes=2))
    await process_publish_queue(bot, now=t0 + timedelta(minutes=3))

    assert len(_notifications(bot, world)) == 1


async def test_notifies_partial_then_completes(bot, world, enqueue):
    """Частичная публикация: сообщаем, где вышел и где ещё в очереди."""
    t0 = now_utc()
    await _silence(world.chat_tg_ids[1], t0)

    await enqueue("sub1")
    await process_publish_queue(bot, now=t0 + timedelta(seconds=1))

    notes = _notifications(bot, world)
    assert len(notes) == 1
    # Частичная публикация в макете — тот же экран, что и полная; отличается
    # только список чатов с отметками ✅ / ⏳ (см. _render_notification).
    assert "опубликовано" in notes[0].lower()
    assert "⏳ Группа B" in notes[0], "должно быть видно, в какой группе пост ещё ждёт"
    assert "✅ Группа A" in notes[0], "и где он уже вышел"

    await process_publish_queue(bot, now=t0 + SILENCE)
    assert (await publication(1)).status == "published"
    # уведомление обновляется на месте, а не спамит новым
    assert any("опубликован" in (e["text"] or "").lower() for e in bot.edited)


async def test_edit_updates_published_messages(bot, world, enqueue):
    """«Редактировать»: текст меняется во всех группах, где пост уже вышел."""
    await enqueue("sub1", "исходный текст объявления")
    await process_publish_queue(bot, now=now_utc())

    updated, failed, pending = await apply_publication_edit(bot, 1, "новый текст объявления")

    assert (updated, failed, pending) == (3, 0, 0)
    edited_group_msgs = [e for e in bot.edited if e["chat_id"] in world.chat_tg_ids]
    assert len(edited_group_msgs) == 3
    assert all("новый текст объявления" in (e["text"] or "") for e in edited_group_msgs)

    jobs = await all_jobs()
    assert {j.text for j in jobs} == {"новый текст объявления"}


async def test_edit_applies_to_queued_jobs_too(bot, world, enqueue):
    """Ещё не вышедшие задачи получают уже новый текст."""
    t0 = now_utc()
    await _silence(world.chat_tg_ids[1], t0)
    await enqueue("sub1", "исходный текст объявления")
    await process_publish_queue(bot, now=t0 + timedelta(seconds=1))

    updated, failed, pending = await apply_publication_edit(bot, 1, "исправленный текст")

    assert updated == 2 and pending == 1
    queued = [j for j in await jobs_for_chat(world.chat_ids[1])]
    assert queued[0].text == "исправленный текст"


async def test_close_strikes_through_and_cancels_queue(bot, world, enqueue):
    """«Закрыть»: зачёркиваем опубликованное и снимаем оставшееся из очереди."""
    t0 = now_utc()
    await _silence(world.chat_tg_ids[1], t0)
    await enqueue("sub1")
    await process_publish_queue(bot, now=t0 + timedelta(seconds=1))

    closed, failed, cancelled = await apply_publication_close(bot, 1)

    assert closed == 2
    assert cancelled == 1
    assert failed == 0
    struck = [e for e in bot.edited if e["chat_id"] in world.chat_tg_ids]
    assert all("<s>" in (e["text"] or "") for e in struck)
    assert (await publication(1)).closed is True

    statuses = {j.status for j in await all_jobs()}
    assert statuses == {"published", "cancelled"}


async def test_notification_lists_failed_groups(bot, world, enqueue, monkeypatch):
    """Пользователь видит, что в одной группе публикация не удалась."""
    async def fake_notify(publication_id, chat_id, error):
        return None

    monkeypatch.setattr(
        "app.services.notifications.notify_admins_publish_failed", fake_notify, raising=True,
    )
    bot.fail_chats[world.chat_tg_ids[0]] = "Forbidden: bot was kicked from the supergroup chat"

    await enqueue("sub1")
    await process_publish_queue(bot, now=now_utc())

    notes = _notifications(bot, world)
    assert notes and "Группа A" in notes[-1]
