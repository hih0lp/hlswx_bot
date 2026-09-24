"""Журнал очереди публикаций: события в БД + структурный лог (ТЗ 6.1)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import PublishEvent

logger = logging.getLogger("publish.events")

# события: queued | published | retry | failed | expired | silence_set | paused | edited | closed | cancelled


async def log_event(
    session: AsyncSession,
    event: str,
    *,
    publication_id: int | None = None,
    job_id: int | None = None,
    chat_id: int | None = None,
    detail: str = "",
) -> None:
    session.add(
        PublishEvent(
            publication_id=publication_id,
            job_id=job_id,
            chat_id=chat_id,
            event=event,
            detail=(detail or "")[:2000] or None,
        ),
    )
    logger.info(
        "%s publication=%s job=%s chat=%s %s",
        event,
        publication_id,
        job_id,
        chat_id,
        detail,
    )
