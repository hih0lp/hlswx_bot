"""Действия администратора над публикациями и очередью (ТЗ этапа 2, 6.9).

До этапа 2 сервисного слоя для этого не было вовсе: отмена существовала
только как побочный эффект «закрыть объявление», а массовый повтор был
написан прямо в обработчике и даже не писал событие в журнал очереди.

Каждое действие пишет и `PublishEvent` (журнал очереди этапа 1), и
`admin_audit_log` (журнал действий администратора, ТЗ 7).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.entities import PublishJob
from app.services.publish_log import log_event
from app.services.queue_slots import PRIORITY_ONESHOT

logger = logging.getLogger("queue_admin")

# Статусы, из которых задачу ещё можно вернуть в очередь.
RETRYABLE = ("failed", "expired", "cancelled")


class JobActionError(Exception):
    """Действие неприменимо к задаче в её текущем состоянии."""


async def publish_now(job_id: int, *, actor_telegram_id: int) -> PublishJob:
    """«Опубликовать сейчас» — вне очереди, минуя тишину и паузу.

    Механика та же, что у разового платного поста: приоритет 1 в
    `queue_slots.pick_next_job` обходит и тишину группы, и паузу между
    авторами. Сама отправка произойдёт на ближайшем тике воркера.
    """
    async with SessionLocal() as session:
        job = await _get(session, job_id)
        if job.status not in ("queued", *RETRYABLE):
            raise JobActionError(f"публикация уже в статусе «{job.status}»")
        job.status = "queued"
        job.priority = PRIORITY_ONESHOT
        job.scheduled_at = datetime.now(UTC)
        job.error = None
        await log_event(
            session,
            "queued",
            publication_id=job.publication_id,
            job_id=job.id,
            chat_id=job.chat_id,
            detail=f"admin={actor_telegram_id} publish_now",
        )
        await session.commit()
        await session.refresh(job)
        return job


async def retry_job(job_id: int, *, actor_telegram_id: int) -> PublishJob:
    """«Повторить» для публикации с ошибкой: счётчик попыток обнуляется."""
    async with SessionLocal() as session:
        job = await _get(session, job_id)
        if job.status not in RETRYABLE:
            raise JobActionError(f"повторять нечего: статус «{job.status}»")
        job.status = "queued"
        job.attempts = 0
        job.error = None
        job.scheduled_at = datetime.now(UTC)
        await log_event(
            session,
            "retry",
            publication_id=job.publication_id,
            job_id=job.id,
            chat_id=job.chat_id,
            detail=f"admin={actor_telegram_id}",
        )
        await session.commit()
        await session.refresh(job)
        return job


async def cancel_job(job_id: int, *, actor_telegram_id: int) -> PublishJob:
    """«Отменить» / «Удалить из очереди» — задача больше не поедет.

    Строку не удаляем: на неё ссылается публикация, и в истории должно
    остаться видно, что именно отменили.
    """
    async with SessionLocal() as session:
        job = await _get(session, job_id)
        if job.status == "published":
            raise JobActionError("публикация уже вышла — её можно только закрыть")
        job.status = "cancelled"
        job.error = f"cancelled_by_admin:{actor_telegram_id}"
        await log_event(
            session,
            "cancelled",
            publication_id=job.publication_id,
            job_id=job.id,
            chat_id=job.chat_id,
            detail=f"admin={actor_telegram_id}",
        )
        await session.commit()
        await session.refresh(job)
        return job


async def retry_all_failed(*, actor_telegram_id: int) -> int:
    """Массовый повтор с экрана очереди. Возвращает число задач."""
    async with SessionLocal() as session:
        jobs = list(
            (await session.scalars(select(PublishJob).where(PublishJob.status == "failed"))).all(),
        )
        now = datetime.now(UTC)
        for job in jobs:
            job.status = "queued"
            job.attempts = 0
            job.error = None
            job.scheduled_at = now
            await log_event(
                session,
                "retry",
                publication_id=job.publication_id,
                job_id=job.id,
                chat_id=job.chat_id,
                detail=f"admin={actor_telegram_id} bulk",
            )
        await session.commit()
        return len(jobs)


async def _get(session, job_id: int) -> PublishJob:
    job = await session.scalar(select(PublishJob).where(PublishJob.id == job_id))
    if job is None:
        raise JobActionError("публикация не найдена")
    return job
