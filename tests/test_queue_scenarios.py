"""Сценарии очереди из таблицы ТЗ 6.5 — критерий приёмки 2.

Номера тестов соответствуют номерам сценариев в ТЗ.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.session import SessionLocal
from app.services.oneshot import register_oneshot_publication
from app.services.publish import MAX_ATTEMPTS, RETRY_DELAYS_SEC, process_publish_queue
from tests.conftest import all_jobs, jobs_for_chat, now_utc, slot_for

SILENCE = timedelta(minutes=20)
PAUSE = timedelta(seconds=120)


async def _set_silence(chat_tg_id: int, at, message_id: int = 1):
    async with SessionLocal() as session:
        return await register_oneshot_publication(
            session,
            telegram_chat_id=chat_tg_id,
            network="hammer",
            message_id=message_id,
            published_at=at,
        )


# ---------------------------------------------------------------- сценарий 1

async def test_scenario_1_oneshot_free_chat(bot, world, enqueue_oneshot):
    """Разовый пост в свободную группу: выходит сразу, группа уходит в тишину на 20 минут."""
    await enqueue_oneshot()
    t0 = now_utc()

    await process_publish_queue(bot, now=t0)

    jobs = await all_jobs()
    assert {j.status for j in jobs} == {"published"}
    for chat_id in world.chat_ids:
        slot = await slot_for(chat_id)
        assert slot.locked_until == t0 + SILENCE


# ---------------------------------------------------------------- сценарий 2

async def test_scenario_2_oneshot_during_silence_extends_window(bot, world, enqueue_oneshot):
    """Разовый пост в группу, уже находящуюся в тишине: выходит немедленно, окно считается заново."""
    t0 = now_utc()
    await _set_silence(world.chat_tg_ids[0], t0)
    assert (await slot_for(world.chat_ids[0])).locked_until == t0 + SILENCE

    await enqueue_oneshot()
    t1 = t0 + timedelta(minutes=5)
    await process_publish_queue(bot, now=t1)

    jobs = await jobs_for_chat(world.chat_ids[0])
    assert [j.status for j in jobs] == ["published"]
    # 12:05 + 20 минут, а не остаток от предыдущего окна
    assert (await slot_for(world.chat_ids[0])).locked_until == t1 + SILENCE


# ---------------------------------------------------------------- сценарий 3

async def test_scenario_3_subscriber_free_chat(bot, world, enqueue):
    """Пост подписчика в свободную группу: публикуется немедленно."""
    await enqueue("sub1")
    t0 = now_utc()

    await process_publish_queue(bot, now=t0)

    jobs = await all_jobs()
    assert {j.status for j in jobs} == {"published"}
    assert len(bot.messages_to(world.chat_tg_ids[0])) == 1


# ---------------------------------------------------------------- сценарий 4

async def test_scenario_4_subscriber_waits_for_pause(bot, world, enqueue):
    """Пауза 2 минуты ещё не истекла: пост встаёт в очередь и выходит по её истечении."""
    await enqueue("sub1")
    t0 = now_utc()
    await process_publish_queue(bot, now=t0)

    await enqueue("sub2")
    t1 = t0 + timedelta(seconds=30)
    await process_publish_queue(bot, now=t1)

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    pending = [j for j in chat_jobs if j.status == "queued"]
    assert len(pending) == 1, "пост второго пользователя должен ждать паузу"
    assert pending[0].scheduled_at == t0 + PAUSE

    await process_publish_queue(bot, now=t0 + PAUSE)
    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert [j.status for j in chat_jobs] == ["published", "published"]


# ---------------------------------------------------------------- сценарий 5

async def test_scenario_5_subscriber_waits_for_silence(bot, world, enqueue):
    """Группа в тишине: пост подписчика не отклоняется, а ждёт её окончания."""
    t0 = now_utc()
    await _set_silence(world.chat_tg_ids[0], t0)

    await enqueue("sub1")
    await process_publish_queue(bot, now=t0 + timedelta(minutes=1))

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert [j.status for j in chat_jobs] == ["queued"]
    assert chat_jobs[0].scheduled_at == t0 + SILENCE
    assert not bot.messages_to(world.chat_tg_ids[0])

    await process_publish_queue(bot, now=t0 + SILENCE)
    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert [j.status for j in chat_jobs] == ["published"]


# ---------------------------------------------------------------- сценарий 6

async def test_scenario_6_whitelist_free_chat(bot, world, enqueue):
    """Белый список: группа свободна и постов подписчиков нет — публикуется немедленно."""
    await enqueue("wl")
    t0 = now_utc()

    await process_publish_queue(bot, now=t0)

    jobs = await all_jobs()
    assert {j.status for j in jobs} == {"published"}
    assert {j.priority for j in jobs} == {3}


# ---------------------------------------------------------------- сценарий 7

async def test_scenario_7_subscriber_beats_whitelist(bot, world, enqueue):
    """В очереди есть посты подписчиков — белый список идёт после них."""
    await enqueue("wl")
    await enqueue("sub1")
    t0 = now_utc()

    await process_publish_queue(bot, now=t0)

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    published = [j for j in chat_jobs if j.status == "published"]
    queued = [j for j in chat_jobs if j.status == "queued"]
    assert len(published) == 1 and published[0].priority == 2, "подписчик обгоняет белый список"
    assert len(queued) == 1 and queued[0].priority == 3

    # белый список выходит после паузы, тишину и паузу он не отменяет
    await process_publish_queue(bot, now=t0 + PAUSE)
    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert {j.status for j in chat_jobs} == {"published"}


# ---------------------------------------------------------------- сценарий 8

async def test_scenario_8_partial_publication(bot, world, enqueue):
    """Пост адресован группам в разном состоянии: выходит независимо, статус — частично."""
    t0 = now_utc()
    await _set_silence(world.chat_tg_ids[1], t0)  # группа B в тишине

    await enqueue("sub1")
    await process_publish_queue(bot, now=t0 + timedelta(seconds=1))

    statuses = {j.chat_id: j.status for j in await all_jobs()}
    assert statuses[world.chat_ids[0]] == "published"
    assert statuses[world.chat_ids[2]] == "published"
    assert statuses[world.chat_ids[1]] == "queued", "в группе B пост ждёт окончания тишины"

    from tests.conftest import publication

    assert (await publication(1)).status == "partial"

    await process_publish_queue(bot, now=t0 + SILENCE)
    statuses = {j.chat_id: j.status for j in await all_jobs()}
    assert set(statuses.values()) == {"published"}
    assert (await publication(1)).status == "published"


# ---------------------------------------------------------------- сценарий 9

async def test_scenario_9_send_error_retries_then_notifies_admin(bot, world, enqueue, monkeypatch):
    """Ошибка отправки: повтор по политике ретраев, при исчерпании — уведомление админа."""
    notices: list[tuple[int, int, str]] = []

    async def fake_notify(publication_id, chat_id, error):
        notices.append((publication_id, chat_id, error))

    monkeypatch.setattr(
        "app.services.notifications.notify_admins_publish_failed", fake_notify, raising=True,
    )
    # временная ошибка — сеть, лечится повтором
    bot.fail_chats[world.chat_tg_ids[0]] = "Connection reset by peer"

    await enqueue("sub1")
    t0 = now_utc()

    await process_publish_queue(bot, now=t0)
    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert chat_jobs[0].status == "queued" and chat_jobs[0].attempts == 1
    assert not notices

    # Повторы идут по политике `MAX_ATTEMPTS` и `RETRY_DELAYS_SEC`: 20.09.2026
    # попыток стало пять вместо трёх — сбой сети до Telegram держится по
    # несколько минут, и трёх не хватало. Тест следует политике, а не
    # переписывает её числами: проверяем исход, а не длину пауз. Шаг заведомо
    # длиннее и самой долгой паузы ретрая, и паузы между постами автора.
    step = timedelta(seconds=max(RETRY_DELAYS_SEC)) + PAUSE
    moment = t0
    for _ in range(MAX_ATTEMPTS - 1):
        moment += step
        await process_publish_queue(bot, now=moment)

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert chat_jobs[0].status == "failed"
    assert chat_jobs[0].attempts == MAX_ATTEMPTS
    assert notices, "администратор должен быть уведомлён при исчерпании попыток"

    # ошибка в одной группе не блокирует остальные (ТЗ 6.7, критерий приёмки 6)
    others = {j.status for j in await all_jobs() if j.chat_id != world.chat_ids[0]}
    assert others == {"published"}


async def test_permanent_error_skips_retries(bot, world, enqueue, monkeypatch):
    """Бот удалён из группы — повторять бессмысленно, сразу отмена и админ."""
    notices: list = []

    async def fake_notify(publication_id, chat_id, error):
        notices.append(chat_id)

    monkeypatch.setattr(
        "app.services.notifications.notify_admins_publish_failed", fake_notify, raising=True,
    )
    bot.fail_chats[world.chat_tg_ids[0]] = "Forbidden: bot was kicked from the supergroup chat"

    await enqueue("sub1")
    await process_publish_queue(bot, now=now_utc())

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert chat_jobs[0].status == "failed"
    assert chat_jobs[0].attempts == 1, "безнадёжная ошибка не ретраится"
    assert notices == [world.chat_ids[0]]


# ---------------------------------------------------------------- сценарий 10

async def test_scenario_10_job_expires_in_queue(bot, world, enqueue, monkeypatch):
    """Пост завис в очереди дольше срока: отмена + уведомление пользователя и админа."""
    notices: list[tuple[int, int, str]] = []

    async def fake_notify(publication_id, chat_id, error):
        notices.append((publication_id, chat_id, error))

    monkeypatch.setattr(
        "app.services.notifications.notify_admins_publish_failed", fake_notify, raising=True,
    )

    await enqueue("sub1")
    t0 = now_utc()

    await process_publish_queue(bot, now=t0 + timedelta(minutes=121))

    jobs = await all_jobs()
    assert {j.status for j in jobs} == {"expired"}
    assert {j.error for j in jobs} == {"queue_timeout"}

    # ТЗ 6.5 сценарий 10: уведомляются и пользователь, и администратор
    assert {chat_id for _, chat_id, _ in notices} == set(world.chat_ids)
    assert {error for *_, error in notices} == {"queue_timeout"}

    user_notes = bot.user_notifications(world.user_tg_ids["sub1"])
    assert user_notes and "срок ожидания" in user_notes[-1].lower()


# ------------------------------------------------- дополнительные требования

async def test_one_author_posts_to_all_groups_simultaneously(bot, world, enqueue):
    """Критерий приёмки 5: пост одного пользователя уходит во все его группы сразу."""
    count = await enqueue("sub1")
    assert count == 3
    t0 = now_utc()

    await process_publish_queue(bot, now=t0)

    jobs = await all_jobs()
    assert {j.status for j in jobs} == {"published"}
    assert {j.published_at for j in jobs} == {t0}, "без внутренней задержки между группами"


async def test_pause_is_per_group(bot, world, enqueue):
    """Пауза считается по каждой группе отдельно, а не глобально."""
    await enqueue("sub1")
    t0 = now_utc()
    await process_publish_queue(bot, now=t0)

    for chat_id in world.chat_ids:
        slot = await slot_for(chat_id)
        assert slot.last_author_user_id == world.user_ids["sub1"]
        assert slot.last_published_at == t0


async def test_order_follows_enqueue_time_not_deferral(bot, world, enqueue):
    """Критерий приёмки 4: после тишины посты выходят в порядке постановки в очередь.

    Первый пост уже отложен до конца тишины, второй пришёл позже и ещё не сдвигался —
    по scheduled_at он выглядит «более ранним», но выйти должен всё равно вторым.
    """
    t0 = now_utc()
    await _set_silence(world.chat_tg_ids[0], t0)

    await enqueue("sub1")
    await process_publish_queue(bot, now=t0 + timedelta(minutes=1))
    await enqueue("sub2")

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    first, second = chat_jobs[0], chat_jobs[1]
    assert first.scheduled_at > second.scheduled_at, "предусловие: порядок по scheduled_at перевёрнут"

    await process_publish_queue(bot, now=t0 + SILENCE)

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    published = [j for j in chat_jobs if j.status == "published"]
    assert len(published) == 1
    assert published[0].author_user_id == world.user_ids["sub1"], "первым выходит поставленный раньше"


async def test_queue_survives_restart(bot, world, enqueue):
    """Критерий приёмки 1: состояние очереди живёт в БД и переживает перезапуск."""
    t0 = now_utc()
    await _set_silence(world.chat_tg_ids[0], t0)
    await enqueue("sub1")

    # «перезапуск» — новый проход воркера с новым объектом бота
    from tests.conftest import FakeBot

    fresh_bot = FakeBot()
    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert chat_jobs[0].status == "queued"

    await process_publish_queue(fresh_bot, now=t0 + SILENCE)
    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert chat_jobs[0].status == "published"


@pytest.mark.parametrize("who,expected_priority", [("sub1", 2), ("wl", 3)])
async def test_priority_assigned_by_source(world, enqueue, who, expected_priority):
    """Подписчик — приоритет 2, белый список — 3 (ТЗ 6.3)."""
    await enqueue(who)
    jobs = await all_jobs()
    assert {j.priority for j in jobs} == {expected_priority}
