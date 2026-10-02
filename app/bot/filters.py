"""Фильтры доступа к админ-панели (ТЗ этапа 2, раздел 7).

Вешаются на роутер целиком, а не на каждый обработчик: до этапа 2 строка
`if not _is_admin(...): return` была скопирована шестьдесят с лишним раз
в одном файле.

Фильтр возвращает словарь, поэтому обработчик может объявить параметр
`role` и получить роль, не перечитывая её.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from app.models.entities import AdminRole
from app.services import access
from app.services.tenant import current_partner_id


class IsStaff(BaseFilter):
    """Доступ к панели: администратор или менеджер."""

    async def __call__(self, event: Message | CallbackQuery) -> bool | dict:
        role = await access.get_role(event.from_user.id if event.from_user else None)
        return {"role": role} if role is not None else False


class IsAdmin(BaseFilter):
    """Только администратор — раздел «Управление» (ТЗ 6.11)."""

    async def __call__(self, event: Message | CallbackQuery) -> bool | dict:
        role = await access.get_role(event.from_user.id if event.from_user else None)
        return {"role": role} if role is AdminRole.admin else False


class IsPlatformBot(BaseFilter):
    """Block platform-only features when an update came from a franchise bot."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return current_partner_id() == 0
