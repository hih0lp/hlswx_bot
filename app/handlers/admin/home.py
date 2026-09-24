"""Главный экран админ-панели (ТЗ этапа 2, 6.1).

Кадры макета: `337:208` — «всё работает нормально», `337:233` — «требует
внимания» с расшифровкой. Экран перерисовывается на месте, как и остальные
разделы (ТЗ 6.13), и он единственный в панели идёт на баннере АДМИНКА:
картинка нарисована только на `337:208`.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core import admin_texts as T
from app.handlers.admin.common import _is_admin
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.services import access, admin_ui
from app.services.admin_stats import gather_dashboard_stats

router = Router()


def _menu(*, is_admin: bool) -> InlineKeyboardMarkup:
    """Меню разделов. «Управление» видит только администратор (ТЗ 6.11, 7).

    Первые семь пунктов — как на кадре `337:208`; «Города и чаты» и
    «Тарифы» добавлены по ТЗ 6.1, кадров для них в макете нет.
    """
    rows = [
        [InlineKeyboardButton(text=T.BTN_STATS, callback_data="adm:stats", style=STYLE_PLAIN)],
        [
            InlineKeyboardButton(text=T.BTN_WHITELIST, callback_data="adm:wl", style=STYLE_PLAIN),
            InlineKeyboardButton(text=T.BTN_PUBLICATIONS, callback_data="adm:pubs", style=STYLE_PLAIN),
        ],
        [
            InlineKeyboardButton(text=T.BTN_PAYMENTS, callback_data="adm:payments", style=STYLE_PLAIN),
            InlineKeyboardButton(text=T.BTN_QUEUE, callback_data="adm:queue", style=STYLE_PLAIN),
        ],
        [
            InlineKeyboardButton(text=T.BTN_PLACES, callback_data="adm:places", style=STYLE_PLAIN),
            InlineKeyboardButton(text=T.BTN_TARIFFS, callback_data="adm:tariffs", style=STYLE_PLAIN),
        ],
        # «🤝 Партнёры» стоят в кадре собственным рядом. Рядом с ними до
        # 20.09.2026 была кнопка «👤 Пользователи» — наша, и ошибочная: мы
        # приняли ветку поиска пользователя за самостоятельный раздел, потому
        # что читали в макете только кадры и не читали стрелки между ними.
        # Стрелки (153 коннектора на странице) говорят обратное: `349:564` и
        # соседние кадры висят на «Управление → Белый список → Найти
        # пользователя», а в меню панели их нет — как нет их и в перечне
        # разделов ТЗ 6.1. Вход туда теперь там же, где в макете.
        [InlineKeyboardButton(text=T.BTN_PARTNERS, callback_data="adm:wlbl", style=STYLE_PLAIN)],
    ]
    if is_admin:
        rows.append(
            [InlineKeyboardButton(text=T.BTN_MANAGE, callback_data="adm:manage", style=STYLE_PLAIN)],
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:home", style=STYLE_MAIN)],
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _status_block(stats: dict) -> str:
    """Индикатор состояния системы с расшифровкой (ТЗ 6.1).

    Ошибки и платежи считаются за текущие сутки (`admin_stats`), поэтому блок
    отражает сегодняшнее состояние, а не всю историю. Очередь сюда больше не
    попадает — она стала показателем сводки.
    """
    lines = []
    if stats["queue_failed"]:
        lines.append(T.PANEL_ATTENTION_ERRORS.format(count=stats["queue_failed"]))
    if stats["pending_payments"]:
        lines.append(T.PANEL_ATTENTION_PAYMENTS.format(count=stats["pending_payments"]))
    if not lines:
        return T.PANEL_OK
    return T.PANEL_ATTENTION.format(lines="\n".join(lines))


async def panel_text() -> str:
    stats = await gather_dashboard_stats()
    return T.PANEL.format(
        users=stats["users"],
        active_subs=stats["active_subs"],
        publications_today=stats["publications_today"],
        queued=stats["queue_queued"],
        status=_status_block(stats),
    )


async def show_panel(target: Message | CallbackQuery, *, edit: bool | None = None) -> None:
    """Единственный экран панели с баннером — так он нарисован на `337:208`."""
    user = target.from_user
    markup = _menu(is_admin=access.is_admin(user.id if user else None))
    await admin_ui.show(target, await panel_text(), markup, edit=edit, banner=True)


# Имя оставлено ради внешнего импорта из app/handlers/connect.py.
_show_panel = show_panel


@router.message(Command("admin"))
async def admin_menu(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await show_panel(message)


@router.callback_query(F.data == "adm:home")
async def admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    await show_panel(callback)
