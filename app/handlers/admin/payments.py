"""Раздел «Оплаты» (ТЗ этапа 2, 6.4).

Кадры макета: `340:320` — сводка, `340:353` — в обработке, `340:385` —
ожидают оплаты, `340:567` — все оплаты с пагинацией.

До этапа 2 это был один экран, где каждый из трёх списков обрезался
`limit(8)` без возможности долистать.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import desc, select

from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.handlers.admin.common import LIST_PER_PAGE, _is_admin
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_PLAIN
from app.models.entities import Payment, PaymentStatus, Subscription, SubscriptionStatus
from app.services import admin_stats, admin_ui

router = Router()

PAYMENTS_CB = "adm:payments"
PROCESSING_CB = "adm:payments:proc:"
AWAITING_CB = "adm:payments:await:"
ALL_CB = "adm:payments:all:"

_STATUS_MARK = {
    PaymentStatus.succeeded: "✅",
    PaymentStatus.pending: "⏳",
    PaymentStatus.failed: "❌",
    PaymentStatus.canceled: "🚫",
}


def _money(value) -> str:
    return f"{int(value):,}".replace(",", " ")


def _payment_line(payment: Payment) -> str:
    mark = _STATUS_MARK.get(payment.status, "•")
    return f"{mark} #{payment.id} {payment.purpose} #{payment.purpose_id} — {_money(payment.amount)} ₽"


@router.callback_query(F.data == PAYMENTS_CB)
async def admin_payments(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    summary = await admin_stats.payments_summary()
    text = T.PAYMENTS.format(
        revenue_today=_money(summary["revenue_today"]),
        paid=summary["paid"],
        processing=summary["processing"],
        failed=summary["failed"],
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_PAYMENTS_PROCESSING, callback_data=f"{PROCESSING_CB}0", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_PAYMENTS_AWAITING, callback_data=f"{AWAITING_CB}0", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_PAYMENTS_ALL, callback_data=f"{ALL_CB}0", style=STYLE_PLAIN)],
            [admin_ui.panel_button()],
        ],
    )
    await admin_ui.show(callback, text, markup)


def _list_markup(prefix: str, page: int, pages: int) -> InlineKeyboardMarkup:
    rows = []
    pager = pager_row(prefix, page, pages)
    if pager:
        rows.append(pager)
    rows.append([admin_ui.back_button(PAYMENTS_CB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_list(callback: CallbackQuery, prefix: str, template: str, rows: list, render) -> None:
    page = int(callback.data.rsplit(":", 1)[-1])
    shown, page, pages = page_slice(rows, page, LIST_PER_PAGE)
    lines = "\n".join(render(item) for item in shown) if shown else T.PAYMENTS_EMPTY
    await admin_ui.show(
        callback,
        template.format(count=len(rows), lines=lines),
        _list_markup(prefix, page, pages),
    )


@router.callback_query(F.data.startswith(PROCESSING_CB))
async def admin_payments_processing(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(Payment)
                    .where(Payment.status == PaymentStatus.pending)
                    .order_by(desc(Payment.id)),
                )
            ).all(),
        )
    await _show_list(callback, PROCESSING_CB, T.PAYMENTS_PROCESSING, rows, _payment_line)


@router.callback_query(F.data.startswith(AWAITING_CB))
async def admin_payments_awaiting(callback: CallbackQuery) -> None:
    """Выставленные, но не оплаченные подписки — кадр 340:385."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(Subscription)
                    .where(Subscription.status == SubscriptionStatus.pending_payment)
                    .order_by(desc(Subscription.id)),
                )
            ).all(),
        )
    await _show_list(
        callback,
        AWAITING_CB,
        T.PAYMENTS_AWAITING,
        rows,
        lambda sub: f"⏳ #{sub.id} — {_money(sub.total_price)} ₽",
    )


@router.callback_query(F.data.startswith(ALL_CB))
async def admin_payments_all(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        day_start, _ = admin_stats.period_bounds(admin_stats.PERIOD_TODAY)
        rows = list(
            (
                await session.scalars(
                    select(Payment).where(Payment.created_at >= day_start).order_by(desc(Payment.id)),
                )
            ).all(),
        )
    await _show_list(callback, ALL_CB, T.PAYMENTS_ALL, rows, _payment_line)
