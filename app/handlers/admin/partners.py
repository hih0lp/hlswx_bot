"""Раздел «Партнёры»: просмотр подключённых ботов (ТЗ этапа 3)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.core import admin_texts as T
from app.bot.filters import IsPlatformBot
from app.db.session import SessionLocal
from app.handlers.admin.common import (
    LIST_PER_PAGE,
    _is_admin,
)
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import (
    WhitelabelPartner,
)
from app.services import admin_audit, admin_ui
from app.services.textfmt import format_date
from app.states.admin import AdminFlow

router = Router()
router.callback_query.filter(IsPlatformBot())
router.message.filter(IsPlatformBot())

# Префиксы раздела. `adm:wl:` не занимаем — это белый список.
INTEGRATION_CB = "adm:wlbl:integration:"   # + id партнёра
CHECK_CB = "adm:wlbl:check:"               # + id партнёра


@router.callback_query(
    F.data.startswith("adm:wlreq") | F.data.startswith("adm:wlbl:add"),
)
async def retired_partner_manual_actions(callback: CallbackQuery) -> None:
    """Close stale keyboards from the removed manual approval/token workflow."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer(
        "Ручное одобрение и подключение токеном отключены по ТЗ. "
        "Используйте подключение через владельца бота.",
        show_alert=True,
    )


@router.callback_query(F.data == "adm:wlbl")
async def admin_wlbl_menu(callback: CallbackQuery) -> None:
    """Экран списка партнёров — кадр макета 344:1333."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        rows = list((await session.scalars(
            select(WhitelabelPartner)
            .where(WhitelabelPartner.bot_username.isnot(None))
            .order_by(WhitelabelPartner.id)
        )).all())
        # Партнёр с закончившейся подпиской в списке — «Требует внимания», как в макете.
        healthy = {r.id: (await _partner_state(session, r))[0] == "ok" for r in rows}
    working = sum(1 for r in rows if healthy[r.id])
    text = T.PARTNERS.format(total=len(rows), working=working, attention=len(rows) - working)

    # Кадр 344:1333: в записи списка стоит не только значок, но и состояние
    # словами — по одному кружку непонятно, чем «красный» партнёр болеет.
    kb_rows = [
        [
            InlineKeyboardButton(
                text=f"@{row.bot_username} · "
                     f"{T.PARTNER_STATE_OK if healthy[row.id] else T.PARTNER_STATE_OFF}",
                callback_data=f"adm:wlbl:card:{row.id}",
                style=STYLE_PLAIN,
            ),
        ]
        for row in rows[:LIST_PER_PAGE]
    ]
    if len(rows) > LIST_PER_PAGE:
        kb_rows.append([
            InlineKeyboardButton(text=T.BTN_PARTNERS_ALL, callback_data="adm:wlbl:list", style=STYLE_PLAIN),
        ])
    kb_rows.append([admin_ui.panel_button()])
    await admin_ui.show(callback, text, InlineKeyboardMarkup(inline_keyboard=kb_rows))


async def _partner_state(session, row: WhitelabelPartner) -> tuple[str, int | None]:
    """Состояние партнёра: ("ok" | "off" | "expired", id пользователя-владельца).

    «expired» — подключение включено, но у владельца нет активной подписки:
    ровно тогда публикация по API отвечает `subscription_inactive`.
    """
    from app.models.entities import Subscription, SubscriptionStatus, User
    from app.services.b2b import get_active_corp_subscription

    if not row.active:
        return "off", None
    user = None
    if row.linked_user_id:
        user = await session.scalar(select(User).where(User.id == row.linked_user_id))
    elif row.owner_telegram_id:
        user = await session.scalar(select(User).where(User.telegram_id == row.owner_telegram_id))
    if user is None:
        return "ok", None
    sub = await get_active_corp_subscription(session, user.id)
    if not sub:
        sub = await session.scalar(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.status == SubscriptionStatus.active,
            ),
        )
    return ("ok" if sub else "expired"), user.id


async def _partner_card_for(row: WhitelabelPartner) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        state, user_id = await _partner_state(session, row)
    return _partner_card(row, state, user_id)


def _partner_card(
    row: WhitelabelPartner, state: str = "ok", user_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка партнёра — кадры макета: подключение неактивно / подписка закончилась.

    Счётчиков подписчиков и публикаций в карточке нет — в кадрах их нет, а в базе
    нет отметки, через какого бота пришёл пользователь.
    """
    if state == "off":
        status = T.PARTNER_STATE_INACTIVE
        tail = "\n\n" + T.PARTNER_REASON.format(reason=T.PARTNER_REASON_MOCK)
    elif state == "expired":
        status = T.PARTNER_STATE_EXPIRED
        tail = "\n\n" + T.PARTNER_API_PAUSED
    else:
        status = T.PARTNER_STATE_OK
        tail = ""
    if state == "ok":
        text = T.PARTNER_CARD_OK.format(
            bot_username=row.bot_username,
            status=T.PARTNER_STATUS_OK,
            users=T.PARTNER_USERS_MOCK,
            publications=T.PARTNER_PUBLICATIONS_MOCK,
            chats=T.PARTNER_CHATS_MOCK,
            connected=format_date(row.created_at),
            last_seen=T.PARTNER_LAST_SEEN_OK_MOCK,
        )
    else:
        text = T.PARTNER_CARD.format(
            bot_username=row.bot_username,
            status=status,
            connected=format_date(row.created_at),
            last_seen=T.PARTNER_LAST_SEEN_MOCK,
        ) + tail

    import os
    creds_prefix = f"YOOKASSA_PARTNER_{row.id}_"
    payment_shop = "подключён" if (
        os.getenv(creds_prefix + "SHOP_ID", "").strip()
        and os.getenv(creds_prefix + "SECRET_KEY", "").strip()
    ) else "не подключён — платежи клиентов заблокированы"
    text += f"\n\nОтдельный магазин ЮKassa: <b>{payment_shop}</b>"
    toggle = (
        InlineKeyboardButton(
            text=T.BTN_PARTNER_DISABLE,
            callback_data=f"adm:wlbl:toggle:{row.id}",
            style=STYLE_DANGER,
        )
        if row.active
        else InlineKeyboardButton(
            text=T.BTN_PARTNER_ENABLE,
            callback_data=f"adm:wlbl:toggle:{row.id}",
            style=STYLE_MAIN,
        )
    )
    rows: list[list[InlineKeyboardButton]] = []
    if state == "expired":
        if user_id:
            # К карточке пользователя-владельца — там видно его подписку.
            rows.append([
                InlineKeyboardButton(
                    text=T.BTN_PARTNER_SUBSCRIPTION,
                    callback_data=f"adm:users:card:{user_id}",
                    style=STYLE_PLAIN,
                ),
            ])
    else:
        rows.append([
            InlineKeyboardButton(
                text=T.BTN_PARTNER_CHECK, callback_data=f"{CHECK_CB}{row.id}", style=STYLE_PLAIN,
            ),
        ])
        rows.append([
            InlineKeyboardButton(
                text=T.BTN_PARTNER_SETTINGS, callback_data=f"{INTEGRATION_CB}{row.id}", style=STYLE_PLAIN,
            ),
        ])
    # «Отключить партнёра» — действие карточки по ТЗ 6.7, в кадрах его нет.
    rows.append([toggle])
    rows.append([admin_ui.back_button("adm:wlbl")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)



def _integration_screen(row: WhitelabelPartner) -> tuple[str, InlineKeyboardMarkup]:
    """Настройка интеграции партнёра — кадр макета 344:1478 (ТЗ 6.7)."""
    from app.bot.runtime import partner_bot_is_running

    if row.bot_token:
        mode = T.PARTNER_MODE_POLLING
        if partner_bot_is_running(row.id):
            mode += " · запущен"
    else:
        mode = T.PARTNER_MODE_API

    text = T.PARTNER_INTEGRATION.format(
        brand=row.brand_title or row.bot_username,
        bot_username=row.bot_username,
        status=T.PARTNER_STATUS_OK if row.active else T.PARTNER_STATUS_OFF,
        api=T.PARTNER_API_ON if row.active else T.PARTNER_API_OFF,
        mode=mode,
        last_seen=T.PARTNER_NEVER_SEEN,
        last_request=T.PARTNER_NEVER_SEEN,
        api_key=row.api_key,
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_PARTNER_CHECK,
                    callback_data=f"{CHECK_CB}{row.id}",
                    style=STYLE_MAIN,
                ),
            ],
            [admin_ui.back_button(f"adm:wlbl:card:{row.id}")],
        ],
    )
    return text, markup


async def _partner(partner_id: int) -> WhitelabelPartner | None:
    async with SessionLocal() as session:
        return await session.scalar(
            select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id),
        )


@router.callback_query(F.data.startswith(INTEGRATION_CB))
async def admin_wlbl_integration(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    row = await _partner(int(callback.data.rsplit(":", 1)[-1]))
    if row is None:
        await callback.answer("Партнёр не найден", show_alert=True)
        return
    await callback.answer()
    await admin_ui.show(callback, *_integration_screen(row))


@router.callback_query(F.data.startswith(CHECK_CB))
async def admin_wlbl_check(callback: CallbackQuery) -> None:
    """Живая проверка подключения: дёргаем getMe токеном партнёра."""
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.rsplit(":", 1)[-1])
    row = await _partner(partner_id)
    if row is None:
        await callback.answer("Партнёр не найден", show_alert=True)
        return

    back = InlineKeyboardMarkup(
        inline_keyboard=[[admin_ui.back_button(f"{INTEGRATION_CB}{partner_id}")]],
    )
    if not row.bot_token:
        await callback.answer()
        await admin_ui.show(callback, T.PARTNER_CHECK_API_ONLY, back)
        return

    from app.bot.runtime import check_partner_token

    await callback.answer("Проверяю…")
    ok, detail = await check_partner_token(row.bot_token)
    template = T.PARTNER_CHECK_OK if ok else T.PARTNER_CHECK_FAIL
    await admin_ui.show(callback, template.format(detail=detail), back)


@router.callback_query(F.data.startswith("adm:wlbl:card:"))
async def admin_wlbl_card(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
    if row is None:
        await callback.answer("Партнёр не найден", show_alert=True)
        return
    await callback.answer()
    text, markup = await _partner_card_for(row)
    await admin_ui.show(callback, text, markup)


@router.callback_query(F.data == "adm:wlbl:find")
async def admin_wlbl_find_start(callback: CallbackQuery, state: FSMContext) -> None:
    """«Найти партнёра» — кадр макета 344:1744."""
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.wlbl_find)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        "<b>🔎 Найти партнёра</b>\n\nВведите username бота",
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wlbl")]]),
    )


@router.message(AdminFlow.wlbl_find)
async def admin_wlbl_find(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    username = (message.text or "").strip().lstrip("@")
    async with SessionLocal() as session:
        row = await session.scalar(
            select(WhitelabelPartner).where(func.lower(WhitelabelPartner.bot_username) == username.lower()),
        )
    if row is None:
        await admin_ui.show(
            message,
            T.PARTNER_NOT_FOUND.format(bot_username=username),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [admin_ui.back_button("adm:wlbl")],
                ],
            ),
            edit=False,
        )
        return
    text, markup = await _partner_card_for(row)
    await admin_ui.show(message, text, markup, edit=False)


async def _render_wlbl_list(page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Полный список партнёров с круговой пагинацией.

    Оплаченные, но ещё без токена бота (owner ещё не прислал @BotFather
    токен — ТЗ «Оплата -> Создание бота») сюда не попадают: показывать
    здесь нечего, владелец продолжает подключение из своего «Франшиза».
    """
    async with SessionLocal() as session:
        rows = list((await session.scalars(
            select(WhitelabelPartner)
            .where(WhitelabelPartner.bot_username.isnot(None))
            .order_by(WhitelabelPartner.id)
        )).all())
    if not rows:
        return None
    shown, page, pages = page_slice(rows, page, LIST_PER_PAGE)
    kb_rows = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if row.active else '🔴'} @{row.bot_username}",
                callback_data=f"adm:wlbl:card:{row.id}",
                style=STYLE_PLAIN,
            ),
        ]
        for row in shown
    ]
    pager = pager_row("adm:wlbl:lpage:", page, pages)
    if pager:
        kb_rows.append(pager)
    kb_rows.append([admin_ui.back_button("adm:wlbl")])
    working = sum(1 for r in rows if r.active)
    text = T.PARTNERS.format(total=len(rows), working=working, attention=len(rows) - working)
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "adm:wlbl:list")
async def admin_wlbl_list(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    rendered = await _render_wlbl_list(0)
    if rendered is None:
        await callback.answer("Партнёров пока нет", show_alert=True)
        return
    text, kb = rendered
    await admin_ui.show(callback, text, kb)


@router.callback_query(F.data.startswith("adm:wlbl:lpage:"))
async def admin_wlbl_list_page(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    rendered = await _render_wlbl_list(int(callback.data.rsplit(":", 1)[-1]))
    if rendered is None:
        return
    text, kb = rendered
    await admin_ui.show(callback, text, kb)


@router.callback_query(F.data.startswith("adm:wlbl:toggle:"))
async def admin_wlbl_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
        if row is None:
            await callback.answer("Не найден", show_alert=True)
            return
        row.active = not row.active
        await session.commit()
        from app.bot.runtime import request_partner_bots_reload

        request_partner_bots_reload()
        status = "активен" if row.active else "отключён"
        await admin_audit.log_action(
            callback.from_user.id,
            admin_audit.PARTNER_TOGGLE,
            target=f"@{row.bot_username}",
            detail=status,
        )
        await callback.answer(status)
        text, markup = _partner_card(row, *await _partner_state(session, row))
    await admin_ui.show(callback, text, markup)
