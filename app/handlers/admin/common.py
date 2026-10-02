"""Общее для разделов панели: доступ, размеры страниц, мелкие форматтеры."""

from __future__ import annotations

import logging


from app.services import access

logger = logging.getLogger("admin")


WL_PER_PAGE = 8


LIST_PER_PAGE = 8


QUEUE_PER_PAGE = 5


def _is_admin(user_id: int) -> bool:
    """Доступ к панели: администратор или менеджер (ТЗ 7).

    Разделы «Управления» проверяют роль отдельно — см. services.access.
    """
    return access.is_staff(user_id)


ADMIN_EXIT_TEXTS = frozenset({"⬅️ В админку", "◀️ В админку", "← В админку"})


async def leave_flow_if_escaped(message, state) -> bool:
    """Выпустить админа из шага ввода, если он вместо ответа ушёл в другой раздел.

    Шаги панели принимают произвольный текст (название города, комментарий,
    сумма), поэтому команда `/admin` или нажатие кнопки меню превращались во
    ввод: админ вводил «/admin» и заводил город с таким названием. Возвращает
    True, если состояние сброшено и обрабатывать сообщение дальше не нужно.
    """
    from app.services.menu_nav import dispatch_menu_button

    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        if text.startswith("/admin"):
            from app.handlers.admin.home import show_panel

            await show_panel(message)
        return True
    if text in ADMIN_EXIT_TEXTS:
        await state.clear()
        from app.handlers.admin.home import show_panel

        await show_panel(message)
        return True
    return await dispatch_menu_button(message, state)
