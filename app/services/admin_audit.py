"""Журнал действий администратора и менеджера (ТЗ этапа 2, раздел 7).

ТЗ требует фиксировать «добавление в белый список, изменение тарифа, выдачу
доступа, отключение партнёра» для последующей диагностики. Существующий
`PublishEvent` для этого не годится: он про очередь и не хранит автора.

Запись в журнал не должна ронять само действие — если лог не записался,
администратор всё равно получает результат, а ошибка уходит в логи.
"""

from __future__ import annotations

import logging

from sqlalchemy import desc, select

from app.db.session import SessionLocal
from app.models.entities import AdminAuditLog
from app.services import access

logger = logging.getLogger("admin_audit")

# Действия. Строки короткие и стабильные — по ним потом фильтруют журнал.
WHITELIST_ADD = "whitelist.add"
WHITELIST_EDIT = "whitelist.edit"
WHITELIST_DELETE = "whitelist.delete"
TARIFF_PRICE = "tariff.price"
TARIFF_RENAME = "tariff.rename"
ACCESS_GRANT = "access.grant"
ACCESS_REVOKE = "access.revoke"
PARTNER_TOGGLE = "partner.toggle"
PARTNER_ADD = "partner.add"
QUEUE_SETTING = "queue.setting"
PUBLICATION_ACTION = "publication.action"
CITY_ADD = "city.add"
CITY_TOGGLE = "city.toggle"
CHAT_ADD = "chat.add"
CHAT_TOGGLE = "chat.toggle"
CITY_DELETE = "city.delete"
CHAT_DELETE = "chat.delete"
BROADCAST = "broadcast.send"
MESSAGE_SEND = "message.send"


async def log_action(
    actor_telegram_id: int,
    action: str,
    *,
    target: str | None = None,
    detail: str | None = None,
) -> None:
    """Записать действие. Ошибка записи не прерывает само действие."""
    role = access.role_of(actor_telegram_id)
    try:
        async with SessionLocal() as session:
            session.add(
                AdminAuditLog(
                    actor_telegram_id=actor_telegram_id,
                    actor_role=role.value if role else None,
                    action=action,
                    target=str(target)[:128] if target else None,
                    detail=detail,
                ),
            )
            await session.commit()
    except Exception:
        logger.exception("Не удалось записать в журнал действие %s", action)


async def recent(limit: int = 50, *, action: str | None = None) -> list[AdminAuditLog]:
    """Последние записи журнала — для диагностики."""
    async with SessionLocal() as session:
        query = select(AdminAuditLog).order_by(desc(AdminAuditLog.id)).limit(limit)
        if action:
            query = query.where(AdminAuditLog.action == action)
        return list((await session.scalars(query)).all())
