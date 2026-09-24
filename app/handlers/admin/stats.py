"""Раздел «Статистика» и подраздел «Выручка» (ТЗ этапа 2, 6.2).

Кадры макета: `337:336` — показатели с переключателем периода,
`337:364` и `337:410` — выручка. Активный период подсвечивается синим:
в макете он помечен заливкой, а заливкой там помечено только выбранное
состояние (см. палитру в `app/keyboards/style.py`).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.core import admin_texts as T
from app.handlers.admin.common import _is_admin
from app.keyboards.style import STYLE_ACTIVE, STYLE_PLAIN
from app.services import admin_stats, admin_ui

router = Router()

STATS_CB = "adm:stats"
REVENUE_CB = "adm:stats:rev:"

# Периоды показателей — по макету их два; у выручки четыре.
STATS_PERIODS = ((admin_stats.PERIOD_7, T.BTN_PERIOD_7), (admin_stats.PERIOD_30, T.BTN_PERIOD_30))
REVENUE_PERIODS = (
    (admin_stats.PERIOD_TODAY, T.BTN_PERIOD_TODAY),
    (admin_stats.PERIOD_7, T.BTN_PERIOD_7),
    (admin_stats.PERIOD_30, T.BTN_PERIOD_30),
    (admin_stats.PERIOD_ALL, T.BTN_PERIOD_ALL),
)


def _period_row(periods, current: str, prefix: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=label,
            callback_data=f"{prefix}{period}",
            style=STYLE_ACTIVE if period == current else STYLE_PLAIN,
        )
        for period, label in periods
    ]


def _money(value) -> str:
    return f"{int(value):,}".replace(",", " ")


@router.callback_query(F.data == STATS_CB)
@router.callback_query(F.data.startswith("adm:stats:p:"))
async def admin_stats_screen(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    period = (
        callback.data.rsplit(":", 1)[-1]
        if callback.data.startswith("adm:stats:p:")
        else admin_stats.PERIOD_7
    )
    data = await admin_stats.stats_overview(period)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            _period_row(STATS_PERIODS, period, "adm:stats:p:"),
            [InlineKeyboardButton(text=T.BTN_REVENUE, callback_data=f"{REVENUE_CB}{period}", style=STYLE_PLAIN)],
            [admin_ui.panel_button()],
        ],
    )
    await admin_ui.show(callback, T.STATS.format(**data), markup)


@router.callback_query(F.data.startswith(REVENUE_CB))
async def admin_revenue(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    period = callback.data.rsplit(":", 1)[-1]
    if period not in admin_stats.PERIOD_LABELS:
        period = admin_stats.PERIOD_TODAY
    data = await admin_stats.revenue(period)

    versus = admin_stats.PERIOD_PREV_LABELS.get(period, "")
    percent = data.change_percent
    if percent is None or not versus:
        change = T.REVENUE_CHANGE_NONE
    elif percent > 0:
        change = T.REVENUE_CHANGE_UP.format(percent=percent, versus=versus)
    elif percent < 0:
        change = T.REVENUE_CHANGE_DOWN.format(percent=abs(percent), versus=versus)
    else:
        change = T.REVENUE_CHANGE_FLAT.format(versus=versus)

    text = T.REVENUE.format(
        period=admin_stats.PERIOD_LABELS[period],
        amount=_money(data.amount),
        change=change,
        payments=data.payments,
        new_subscriptions=data.new_subscriptions,
        renewals=data.renewals,
    )
    # «Назад» сохраняет выбранный период: у показателей их два, и возврат с
    # «30 дней» на экран «7 дней» читался как сброс фильтра.
    back_period = period if period in dict(STATS_PERIODS) else admin_stats.PERIOD_7
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            _period_row(REVENUE_PERIODS[:2], period, REVENUE_CB),
            _period_row(REVENUE_PERIODS[2:], period, REVENUE_CB),
            [admin_ui.back_button(f"adm:stats:p:{back_period}", T.BTN_BACK_TO_STATS)],
        ],
    )
    await admin_ui.show(callback, text, markup)
