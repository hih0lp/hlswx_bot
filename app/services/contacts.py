from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import SavedContact


async def list_saved_contacts(session: AsyncSession, user_id: int, *, limit: int = 5) -> list[str]:
    rows = (
        await session.scalars(
            select(SavedContact.contact)
            .where(SavedContact.user_id == user_id)
            .order_by(desc(SavedContact.last_used_at), desc(SavedContact.id))
            .limit(limit),
        )
    ).all()
    return list(rows)


async def remember_contact(session: AsyncSession, user_id: int, contact: str) -> None:
    value = (contact or "").strip()
    if not value:
        return
    row = await session.scalar(
        select(SavedContact).where(SavedContact.user_id == user_id, SavedContact.contact == value),
    )
    if row:
        from datetime import UTC, datetime

        row.last_used_at = datetime.now(UTC)
    else:
        session.add(SavedContact(user_id=user_id, contact=value))
    await session.commit()
