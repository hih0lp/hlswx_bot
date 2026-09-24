"""Админ-панель: сборка роутеров разделов (ТЗ этапа 2).

До этапа 2 вся панель жила одним файлом на 1800 строк. Разделы разнесены по
модулям, но callback-префиксы (`adm:*`) сохранены: старые кнопки висят в
истории чатов у администраторов.

Порядок включения повторяет порядок разделов в прежнем файле — фильтры
разделов не пересекаются, но менять порядок без нужды не стоит.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Router
from aiogram.types import CallbackQuery, TelegramObject

from app.handlers.admin import (
    access_grant,
    home,
    legacy,
    manage,
    messages,
    partners,
    payments,
    places,
    publications,
    queue,
    stats,
    tariffs,
    users,
    whitelist,
)
from app.handlers.admin.home import _show_panel  # noqa: F401 — импортируют извне

logger = logging.getLogger("admin")

admin_router = Router()


@admin_router.callback_query.outer_middleware()
async def log_callback_errors(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: CallbackQuery,
    data: dict[str, Any],
) -> Any:
    """Исключение в кнопке панели должно оставлять след.

    Раньше упавший хендлер не вызывал `callback.answer()`, и админ видел только
    вечно крутящуюся кнопку — ни в чате, ни в логе ничего. Теперь в журнале
    остаётся `callback_data`, по которому видно, какая именно кнопка сломалась.
    """
    try:
        return await handler(event, data)
    except Exception:
        logger.exception("Ошибка в кнопке панели: %s", event.data)
        raise


for _section in (
    home, stats, users, payments, publications, queue, places, tariffs,
    manage, access_grant, messages, whitelist, partners, legacy,
):
    admin_router.include_router(_section.router)

__all__ = ["admin_router", "_show_panel"]
