"""Общее для разделов панели: доступ, размеры страниц, мелкие форматтеры."""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.services.textfmt import to_local
from app.db.session import SessionLocal
from app.models.entities import WhitelabelApplication, WhitelabelApplicationStatus
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


async def _wl_pending_count() -> int:
    async with SessionLocal() as session:
        return await session.scalar(
            select(func.count())
            .select_from(WhitelabelApplication)
            .where(WhitelabelApplication.status == WhitelabelApplicationStatus.pending),
        ) or 0


def _format_wl_application(app: WhitelabelApplication) -> str:
    status_map = {
        WhitelabelApplicationStatus.pending: "⏳ ожидает",
        WhitelabelApplicationStatus.approved: "✅ одобрена",
        WhitelabelApplicationStatus.rejected: "❌ отклонена",
    }
    uname = f"@{app.username}" if app.username else "—"
    bot_line = f"\nБот: @{app.planned_bot_username}" if app.planned_bot_username else ""
    comment = f"\n\n💬 {app.comment}" if app.comment else ""
    note = f"\n\n<i>Админ: {app.admin_note}</i>" if app.admin_note else ""
    created = f"{to_local(app.created_at):%d.%m.%Y %H:%M}" if app.created_at else "—"
    return (
        f"<b>📝 Заявка #{app.id}</b> — {status_map.get(app.status, app.status.value)}\n"
        f"От: {uname}\nID: <code>{app.telegram_id}</code>\n"
        f"Бренд: <b>{app.brand_title}</b>{bot_line}{comment}{note}\n\n"
        f"Создана: {created}"
    )
