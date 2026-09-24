"""Раздел «Тарифы» (ТЗ этапа 2, 6.6).

Собран по кадрам макета 375:454 → 375:510 → 375:618 → 375:674: список
категорий со стоимостью стоит в тексте экрана, кнопка на нём одна —
«Изменить тариф», за ней экран выбора категории и ввод новой стоимости.

Переименования тарифа в макете нет, и по решению заказчика от 14.09.2026 оно
убрано: код категории и так неизменен — им размечена обучающая выборка и на
него ссылаются уже оплаченные подписки, а название теперь правится только
через справочник в коде и разовую сверку сида.
"""

from __future__ import annotations

from aiogram import F, Router
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
from app.ml.categories import category_icon
from app.services import admin_audit, admin_ui, tariffs
from app.states.admin import AdminFlow

router = Router()

TARIFFS_CB = "adm:tariffs"
PICK_CB = "adm:tariffs:pick"
PRICE_CB = "adm:tariffs:price:"


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _tariff_label(code: str, label: str, price: int) -> str:
    return f"{category_icon(code)} {label} · {_money(price)} ₽"


@router.callback_query(F.data == TARIFFS_CB)
async def admin_tariffs(callback: CallbackQuery, state: FSMContext) -> None:
    """Кадр 375:454 — список стоимостей и одна кнопка действия."""
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    await tariffs.ensure_loaded()
    # Тарифы идут вместе, пустая строка только после названия экрана.
    lines = "\n".join(
        _tariff_label(t.code, t.label, t.price_per_chat) for t in tariffs.all_tariffs()
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_TARIFF_PRICE, callback_data=PICK_CB, style=STYLE_PLAIN)],
            [admin_ui.panel_button()],
        ],
    )
    await admin_ui.show(callback, T.TARIFFS.format(lines=lines), markup)


@router.callback_query(F.data == PICK_CB)
async def admin_tariff_pick(callback: CallbackQuery, state: FSMContext) -> None:
    """Кадр 375:510 — выбор категории."""
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    await tariffs.ensure_loaded()
    # Все категории — одной колонкой, по кнопке в ряд (заказчик 21.09.2026):
    # в половину ряда «Купля / продажа» и «Драгметаллы» обрезались.
    rows = [
        [
            InlineKeyboardButton(
                text=_tariff_label(t.code, t.label, t.price_per_chat),
                callback_data=f"{PRICE_CB}{t.code}",
                style=STYLE_PLAIN,
            ),
        ]
        for t in tariffs.all_tariffs()
    ]
    rows.append([admin_ui.back_button(TARIFFS_CB)])
    await admin_ui.show(callback, T.TARIFF_PICK, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith(PRICE_CB))
async def admin_tariff_price_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Кадр 375:618 — ввод новой стоимости."""
    if not _is_admin(callback.from_user.id):
        return
    code = callback.data.rsplit(":", 1)[-1]
    tariff = tariffs.get(code)
    await state.set_state(AdminFlow.tariff_price)
    await state.update_data(tariff_code=code)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.TARIFF_PRICE_ASK.format(
            label=f"{category_icon(code)} {tariff.label}",
            price=_money(tariff.price_per_chat),
        ),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(PICK_CB)]]),
    )


@router.message(AdminFlow.tariff_price)
async def admin_tariff_price_save(message: Message, state: FSMContext) -> None:
    """Кадр 375:674 — итог со старой и новой стоимостью."""
    if not _is_admin(message.from_user.id):
        return
    code = (await state.get_data()).get("tariff_code")
    raw = (message.text or "").strip().replace(" ", "")
    if not raw.isdigit():
        await admin_ui.show(
            message,
            T.TARIFF_PRICE_BAD,
            InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(PICK_CB)]]),
            edit=False,
        )
        return

    before = tariffs.get(code)
    await state.clear()
    tariff = await tariffs.set_price(code, int(raw), by_telegram_id=message.from_user.id)
    await admin_audit.log_action(
        message.from_user.id,
        admin_audit.TARIFF_PRICE,
        target=code,
        detail=f"{before.price_per_chat} → {tariff.price_per_chat}",
    )
    await admin_ui.show(
        message,
        T.TARIFF_SAVED_PRICE.format(
            label=f"{category_icon(code)} {tariff.label}",
            old=_money(before.price_per_chat),
            new=_money(tariff.price_per_chat),
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[[admin_ui.back_button(TARIFFS_CB, T.BTN_BACK_TO_TARIFFS)]],
        ),
        edit=False,
    )
