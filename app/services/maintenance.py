"""Регулярная уборка: неудачные публикации старше срока хранения."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func

from app.db.session import SessionLocal
from app.models.entities import PublishJob
from app.services.admin_stats import FAILED_STATUSES

logger = logging.getLogger("maintenance")

FAILED_RETENTION_DAYS = 30


async def purge_old_failed_publications(days: int = FAILED_RETENTION_DAYS) -> int:
    """Удалить неудачные (failed / expired) публикации старше `days` дней.

    Опубликованные и ждущие в очереди не трогаются. Возвращает число строк.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    async with SessionLocal() as session:
        result = await session.execute(
            delete(PublishJob).where(
                PublishJob.status.in_(FAILED_STATUSES),
                func.coalesce(PublishJob.failed_at, PublishJob.scheduled_at) < cutoff,
            ),
        )
        await session.commit()
    return int(result.rowcount or 0)
