"""Состояние слота группы и выбор следующей задачи (ТЗ 6.2–6.5).

Очередь строится по группам: каждая группа независимо хранит своё состояние
(свободна / тишина / пауза) и разбирает собственную очередь.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import ChatSlotState, PublishJob

logger = logging.getLogger("queue_slots")

# Уровни приоритета по ТЗ 6.3
PRIORITY_ONESHOT = 1  # разовый платный пост из Hammer/W (и пакет, купленный у нас)
PRIORITY_SUBSCRIBER = 2  # ежемесячная подписка
PRIORITY_WHITELIST = 3  # белый список

# Совместимость со старыми именами
PRIORITY_PACKAGE = PRIORITY_ONESHOT
PRIORITY_SUBSCRIPTION = PRIORITY_SUBSCRIBER

SLOT_FREE = "free"
SLOT_SILENCE = "silence"
SLOT_PAUSE = "pause"


@dataclass
class SlotDecision:
    """Что делать с группой прямо сейчас."""

    job: PublishJob | None = None
    state: str = SLOT_FREE
    ready_at: datetime | None = None  # когда группа освободится, если сейчас занята


async def get_slot(session: AsyncSession, chat_id: int) -> ChatSlotState | None:
    return await session.scalar(select(ChatSlotState).where(ChatSlotState.chat_id == chat_id))


async def _get_or_create_slot(session: AsyncSession, chat_id: int) -> ChatSlotState:
    slot = await get_slot(session, chat_id)
    if slot is None:
        slot = ChatSlotState(chat_id=chat_id, locked_until=datetime.now(UTC) - timedelta(days=1))
        session.add(slot)
        await session.flush()
    return slot


async def set_silence(
    session: AsyncSession,
    chat_id: int,
    *,
    published_at: datetime,
    minutes: int,
) -> datetime:
    """Тишина после разового поста: всегда 20 минут вперёд от момента ЭТОГО поста.

    Каждый новый разовый пост пересчитывает окно заново, даже если группа уже
    в тишине (ТЗ 6.4, сценарий 2).
    """
    slot = await _get_or_create_slot(session, chat_id)
    slot.locked_until = published_at + timedelta(minutes=minutes)
    return slot.locked_until


async def mark_published(
    session: AsyncSession,
    chat_id: int,
    *,
    published_at: datetime,
    author_user_id: int | None,
) -> None:
    """Запомнить последнюю публикацию в группе — от этого считается пауза."""
    slot = await _get_or_create_slot(session, chat_id)
    slot.last_published_at = published_at
    slot.last_author_user_id = author_user_id


def slot_status(slot: ChatSlotState | None, now: datetime, pause_sec: int) -> tuple[str, datetime | None]:
    """Текущее состояние группы: свободна / тишина / пауза."""
    if slot is None:
        return SLOT_FREE, None
    if slot.locked_until and slot.locked_until > now:
        return SLOT_SILENCE, slot.locked_until
    if slot.last_published_at:
        ready_at = slot.last_published_at + timedelta(seconds=pause_sec)
        if ready_at > now:
            return SLOT_PAUSE, ready_at
    return SLOT_FREE, None


async def pick_next_job(
    session: AsyncSession,
    chat_id: int,
    *,
    now: datetime,
    pause_sec: int,
    lock_rows: bool = True,
) -> SlotDecision:
    """Выбрать задачу, которую можно опубликовать в эту группу прямо сейчас."""
    query = (
        select(PublishJob)
        .where(
            PublishJob.chat_id == chat_id,
            PublishJob.status == "queued",
            PublishJob.scheduled_at <= now,
        )
        # Приоритет 2 обгоняет 3 на освободившемся слоте, внутри приоритета —
        # по времени постановки в очередь (ТЗ 6.3). Порядок задаётся id, а не
        # scheduled_at: последний лишь гейт «не раньше чем» и сдвигается вперёд
        # при тишине, паузе и ретраях, поэтому очерёдность по нему теряется.
        .order_by(PublishJob.priority, PublishJob.id)
        .limit(1)
    )
    if lock_rows:
        query = query.with_for_update(skip_locked=True)
    job = await session.scalar(query)
    if job is None:
        return SlotDecision()

    # Разовый пост публикуется немедленно и вне общей очереди — тишина и пауза
    # на него не действуют (ТЗ 6.3, сценарии 1–2).
    if job.priority <= PRIORITY_ONESHOT:
        return SlotDecision(job=job, state=SLOT_FREE)

    slot = await get_slot(session, chat_id)

    if slot and slot.locked_until and slot.locked_until > now:
        return SlotDecision(state=SLOT_SILENCE, ready_at=slot.locked_until)

    # Пауза 2 минуты — только между публикациями РАЗНЫХ пользователей.
    # Посты одного автора уходят во все его группы одновременно (ТЗ 6.4).
    if slot and slot.last_published_at:
        same_author = (
            job.author_user_id is not None
            and slot.last_author_user_id is not None
            and job.author_user_id == slot.last_author_user_id
        )
        if not same_author:
            ready_at = slot.last_published_at + timedelta(seconds=pause_sec)
            if ready_at > now:
                return SlotDecision(state=SLOT_PAUSE, ready_at=ready_at)

    return SlotDecision(job=job, state=SLOT_FREE)


async def defer_chat_jobs(
    session: AsyncSession,
    chat_id: int,
    *,
    until: datetime,
    exclude_oneshot: bool = True,
) -> None:
    """Отложить ждущие задачи группы до освобождения слота.

    Задачи остаются в очереди и не теряются — так требует ТЗ 6.4.
    """
    query = select(PublishJob).where(
        PublishJob.chat_id == chat_id,
        PublishJob.status == "queued",
        PublishJob.scheduled_at < until,
    )
    if exclude_oneshot:
        query = query.where(PublishJob.priority > PRIORITY_ONESHOT)
    for job in (await session.scalars(query)).all():
        job.scheduled_at = until
