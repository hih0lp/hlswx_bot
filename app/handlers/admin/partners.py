"""Раздел «Партнёры»: заявки и подключённые боты (ТЗ этапа 2, 6.7)."""

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
from app.db.session import SessionLocal
from app.handlers.admin.common import (
    LIST_PER_PAGE,
    _format_wl_application,
    _is_admin,
    _wl_pending_count,
)
from app.keyboards.admin import admin_wlreq_keyboard, admin_wlreq_view_keyboard
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_DANGER, STYLE_MAIN, STYLE_PLAIN
from app.models.entities import (
    WhitelabelApplication,
    WhitelabelApplicationStatus,
    WhitelabelPartner,
)
from app.services import admin_audit, admin_ui
from app.services.textfmt import format_date
from app.states.admin import AdminFlow

router = Router()

# Префиксы раздела. `adm:wl:` не занимаем — это белый список.
INTEGRATION_CB = "adm:wlbl:integration:"   # + id партнёра
CHECK_CB = "adm:wlbl:check:"               # + id партнёра


def _add_back() -> InlineKeyboardMarkup:
    """Возврат с шагов подключения бота — в список партнёров."""
    return InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wlbl")]])


@router.callback_query(F.data == "adm:wlreq")
async def admin_wlreq_menu(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    pending = await _wl_pending_count()
    await admin_ui.show(callback, T.WLREQ.format(pending=pending), admin_wlreq_keyboard())


async def _render_wlreq_list(mode: str, page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with SessionLocal() as session:
        query = select(WhitelabelApplication).order_by(WhitelabelApplication.id.desc())
        if mode == "pending":
            query = query.where(WhitelabelApplication.status == WhitelabelApplicationStatus.pending)
        rows = (await session.scalars(query)).all()
    if not rows:
        return None
    shown, page, pages = page_slice(list(rows), page, LIST_PER_PAGE)
    lines = [T.WLREQ_LIST.format(count=len(rows)), ""]
    kb_rows = []
    for row in shown:
        mark = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(row.status.value, "•")
        uname = f"@{row.username}" if row.username else str(row.telegram_id)
        lines.append(f"{mark} <b>#{row.id}</b> {row.brand_title} — {uname}")
        kb_rows.append([
            InlineKeyboardButton(
                text=f"#{row.id} {row.brand_title[:20]}",
                callback_data=f"adm:wlreq:view:{row.id}",
                style=STYLE_PLAIN,
            ),
        ])
    pager = pager_row(f"adm:wlreq:lpage:{mode}:", page, pages)
    if pager:
        kb_rows.append(pager)
    kb_rows.append([admin_ui.back_button("adm:wlreq")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.startswith("adm:wlreq:list:"))
async def admin_wlreq_list(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    rendered = await _render_wlreq_list(callback.data.rsplit(":", 1)[-1], 0)
    if rendered is None:
        await admin_ui.show(callback, T.WLREQ_EMPTY, admin_wlreq_keyboard())
        return
    await admin_ui.show(callback, *rendered)


@router.callback_query(F.data.startswith("adm:wlreq:lpage:"))
async def admin_wlreq_list_page(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    _, mode, page = callback.data.rsplit(":", 2)
    rendered = await _render_wlreq_list(mode, int(page))
    if rendered is None:
        return
    await admin_ui.show(callback, *rendered)


@router.callback_query(F.data.startswith("adm:wlreq:view:"))
async def admin_wlreq_view(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        app_row = await session.scalar(select(WhitelabelApplication).where(WhitelabelApplication.id == app_id))
    if not app_row:
        await callback.answer("Не найдена", show_alert=True)
        return
    await callback.answer()
    can_partner = app_row.status != WhitelabelApplicationStatus.rejected
    await admin_ui.show(
        callback,
        _format_wl_application(app_row),
        admin_wlreq_view_keyboard(app_id, can_partner=can_partner),
    )


@router.callback_query(F.data.startswith("adm:wlreq:approve:"))
async def admin_wlreq_approve(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.rsplit(":", 1)[-1])
    from datetime import UTC, datetime

    async with SessionLocal() as session:
        app_row = await session.scalar(select(WhitelabelApplication).where(WhitelabelApplication.id == app_id))
        if not app_row:
            await callback.answer("Не найдена", show_alert=True)
            return
        app_row.status = WhitelabelApplicationStatus.approved
        app_row.processed_at = datetime.now(UTC)
        tg_id = app_row.telegram_id
        await session.commit()
    await callback.answer("Одобрено")
    from app.services.notifications import notify_user_wl_application_status

    await notify_user_wl_application_status(tg_id, app_id, approved=True)
    await admin_ui.show(
        callback,
        T.WLREQ_APPROVED.format(number=app_id),
        admin_wlreq_keyboard(),
    )


@router.callback_query(F.data.startswith("adm:wlreq:reject:"))
async def admin_wlreq_reject(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.rsplit(":", 1)[-1])
    from datetime import UTC, datetime

    async with SessionLocal() as session:
        app_row = await session.scalar(select(WhitelabelApplication).where(WhitelabelApplication.id == app_id))
        if not app_row:
            await callback.answer("Не найдена", show_alert=True)
            return
        app_row.status = WhitelabelApplicationStatus.rejected
        app_row.processed_at = datetime.now(UTC)
        tg_id = app_row.telegram_id
        await session.commit()
    await callback.answer("Отклонено")
    from app.services.notifications import notify_user_wl_application_status

    await notify_user_wl_application_status(tg_id, app_id, approved=False)
    await admin_ui.show(
        callback,
        T.WLREQ_REJECTED.format(number=app_id),
        admin_wlreq_keyboard(),
    )


@router.callback_query(F.data.startswith("adm:wlreq:partner:"))
async def admin_wlreq_create_partner(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.rsplit(":", 1)[-1])
    from datetime import UTC, datetime

    async with SessionLocal() as session:
        app_row = await session.scalar(select(WhitelabelApplication).where(WhitelabelApplication.id == app_id))
        if not app_row:
            await callback.answer("Не найдена", show_alert=True)
            return
        if app_row.status == WhitelabelApplicationStatus.pending:
            app_row.status = WhitelabelApplicationStatus.approved
            app_row.processed_at = datetime.now(UTC)
            await session.commit()
            from app.services.notifications import notify_user_wl_application_status

            await notify_user_wl_application_status(app_row.telegram_id, app_id, approved=True)
        bot_username = app_row.planned_bot_username
        owner_id = app_row.telegram_id
        brand = app_row.brand_title

    await callback.answer()
    await state.update_data(wlbl_owner_id=owner_id, wlbl_brand=brand, wlbl_from_app=app_id)
    if bot_username:
        await state.update_data(wlbl_bot=bot_username)
        await state.set_state(AdminFlow.wlbl_token)
        await admin_ui.prompt(
            callback,
            T.PARTNER_FROM_APP_TOKEN.format(
                number=app_id, bot_username=bot_username, owner=owner_id,
            ),
            InlineKeyboardMarkup(
                inline_keyboard=[[admin_ui.back_button(f"adm:wlreq:view:{app_id}")]],
            ),
        )
    else:
        await state.set_state(AdminFlow.wlbl_bot_username)
        await admin_ui.prompt(
            callback,
            T.PARTNER_FROM_APP_USERNAME.format(number=app_id, owner=owner_id, brand=brand),
            InlineKeyboardMarkup(
                inline_keyboard=[[admin_ui.back_button(f"adm:wlreq:view:{app_id}")]],
            ),
        )


@router.callback_query(F.data == "adm:wlbl")
async def admin_wlbl_menu(callback: CallbackQuery) -> None:
    """Экран списка партнёров — кадр макета 344:1333."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        rows = list((await session.scalars(select(WhitelabelPartner).order_by(WhitelabelPartner.id))).all())
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
    pending = await _wl_pending_count()
    kb_rows.append([
        InlineKeyboardButton(text=T.BTN_PARTNER_ADD, callback_data="adm:wlbl:add", style=STYLE_MAIN),
        InlineKeyboardButton(text=T.BTN_PARTNER_FIND, callback_data="adm:wlbl:find", style=STYLE_PLAIN),
    ])
    kb_rows.append([
        InlineKeyboardButton(
            text=f"📝 Заявки ({pending})" if pending else "📝 Заявки",
            callback_data="adm:wlreq",
            style=STYLE_PLAIN,
        ),
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
                    [InlineKeyboardButton(text=T.BTN_PARTNER_ADD, callback_data="adm:wlbl:add", style=STYLE_MAIN)],
                    [admin_ui.back_button("adm:wlbl")],
                ],
            ),
            edit=False,
        )
        return
    text, markup = await _partner_card_for(row)
    await admin_ui.show(message, text, markup, edit=False)


async def _render_wlbl_list(page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Полный список партнёров с круговой пагинацией."""
    async with SessionLocal() as session:
        rows = list((await session.scalars(select(WhitelabelPartner).order_by(WhitelabelPartner.id))).all())
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
        from app.bot.runtime import init_partner_bots

        await init_partner_bots()
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


@router.callback_query(F.data == "adm:wlbl:add")
async def admin_wlbl_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.wlbl_bot_username)
    # Хвосты прошлого мастера (заявка, токен, бренд) не должны влиять на новый.
    await state.update_data(
        wlbl_bot=None, wlbl_owner_id=None, wlbl_token=None, wlbl_brand=None, wlbl_from_app=None,
    )
    await callback.answer()
    await admin_ui.prompt(callback, T.PARTNER_ADD_USERNAME, _add_back())


@router.message(AdminFlow.wlbl_bot_username)
async def admin_wlbl_bot_username(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip().lstrip("@")
    if not raw:
        await admin_ui.show(message, T.PARTNER_ADD_USERNAME_BAD, _add_back(), edit=False)
        return
    if (await state.get_data()).get("wlbl_from_app"):
        # Подключение по заявке — прежний пошаговый мастер.
        await state.update_data(wlbl_bot=raw)
        await state.set_state(AdminFlow.wlbl_owner)
        await admin_ui.prompt(message, T.PARTNER_ADD_OWNER, _add_back())
        return

    found = await _find_bot(raw)
    if found is None:
        # Состояние не меняем: можно сразу отправить другой @username.
        # «Назад» — на один шаг, к запросу @username.
        await admin_ui.show(
            message,
            T.PARTNER_BOT_NOT_FOUND,
            InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wlbl:add")]]),
            edit=False,
        )
        return
    await state.update_data(wlbl_bot=found)
    await state.set_state(AdminFlow.wlbl_confirm)
    await admin_ui.show(
        message,
        T.PARTNER_BOT_FOUND.format(bot_username=found),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=T.BTN_PARTNER_CONNECT, callback_data="adm:wlbl:add:ok", style=STYLE_MAIN,
                    ),
                ],
                [admin_ui.back_button("adm:wlbl:add")],
            ],
        ),
        edit=False,
    )


async def _find_bot(raw: str) -> str | None:
    """@username бота, если он есть в Telegram; иначе None.

    Ник бота по правилам Telegram заканчивается на «bot», а getChat по нему
    отдаёт личный чат. Всё остальное — «бот не найден».
    """
    from app.bot.runtime import get_bot
    from app.services.telegram_resolve import normalize_username

    username = normalize_username(raw)
    if not username or not username.lower().endswith("bot"):
        return None
    try:
        chat = await get_bot().get_chat(f"@{username}")
    except Exception:
        return None
    if chat.type != "private":
        return None
    return (chat.username or username).lstrip("@")


@router.callback_query(AdminFlow.wlbl_confirm, F.data == "adm:wlbl:add:ok")
async def admin_wlbl_connect(callback: CallbackQuery, state: FSMContext) -> None:
    """«🔗 Подключить» — партнёр создаётся по одному @username."""
    if not _is_admin(callback.from_user.id):
        return
    import secrets

    from app.bot.runtime import init_partner_bots

    bot_username = (await state.get_data()).get("wlbl_bot")
    if not bot_username:
        await callback.answer("Бот потерялся, начните заново", show_alert=True)
        return
    api_key = secrets.token_urlsafe(24)
    has_token = False
    async with SessionLocal() as session:
        exists = await session.scalar(
            select(WhitelabelPartner).where(
                func.lower(WhitelabelPartner.bot_username) == bot_username.lower(),
            ),
        )
        if exists:
            exists.active = True
            api_key = exists.api_key
            has_token = bool(exists.bot_token)
            partner_id = exists.id
        else:
            row = WhitelabelPartner(bot_username=bot_username, api_key=api_key)
            session.add(row)
            await session.flush()
            partner_id = row.id
        await session.commit()
    await state.clear()
    await init_partner_bots()
    await admin_audit.log_action(
        callback.from_user.id,
        admin_audit.PARTNER_ADD,
        target=f"@{bot_username}",
        detail="обновлён" if exists else "подключён",
    )
    await callback.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_PARTNER_INTEGRATION,
                    callback_data=f"{INTEGRATION_CB}{partner_id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [admin_ui.back_button("adm:wlbl")],
        ],
    )
    await admin_ui.show(
        callback,
        T.PARTNER_ADDED.format(
            bot_username=bot_username,
            mode=T.PARTNER_MODE_POLLING if has_token else T.PARTNER_MODE_API,
            api_key=api_key,
        ),
        markup,
    )


@router.message(AdminFlow.wlbl_owner)
async def admin_wlbl_owner(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    owner_id = None
    if raw and raw != "-":
        try:
            owner_id = int(raw)
        except ValueError:
            await admin_ui.show(message, T.PARTNER_ADD_OWNER_BAD, _add_back(), edit=False)
            return
    await state.update_data(wlbl_owner_id=owner_id)
    await state.set_state(AdminFlow.wlbl_token)
    await admin_ui.prompt(message, T.PARTNER_ADD_TOKEN, _add_back())


@router.message(AdminFlow.wlbl_token)
async def admin_wlbl_token(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    token = None if raw in {"", "-"} else raw
    await state.update_data(wlbl_token=token)
    data = await state.get_data()
    if data.get("wlbl_brand"):
        await state.set_state(AdminFlow.wlbl_note)
        await admin_ui.prompt(
            message,
            T.PARTNER_ADD_NOTE.format(brand=data["wlbl_brand"]),
            _add_back(),
        )
        return
    await state.set_state(AdminFlow.wlbl_brand)
    await admin_ui.prompt(message, T.PARTNER_ADD_BRAND, _add_back())


@router.message(AdminFlow.wlbl_brand)
async def admin_wlbl_brand(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    if raw in {"", "-"} and data.get("wlbl_brand"):
        brand = data["wlbl_brand"]
    else:
        brand = None if raw in {"", "-"} else raw
    await state.update_data(wlbl_brand=brand)
    await state.set_state(AdminFlow.wlbl_note)
    await admin_ui.prompt(
        message,
        T.PARTNER_ADD_NOTE.format(brand=brand or "—"),
        _add_back(),
    )


@router.message(AdminFlow.wlbl_note)
async def admin_wlbl_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    import secrets

    from app.bot.runtime import init_partner_bots

    data = await state.get_data()
    note = (message.text or "").strip()
    if note == "-":
        note = None
    bot_username = data["wlbl_bot"]
    owner_id = data.get("wlbl_owner_id")
    bot_token = data.get("wlbl_token")
    brand_title = data.get("wlbl_brand")
    api_key = secrets.token_urlsafe(24)
    partner_id: int | None = None
    async with SessionLocal() as session:
        exists = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.bot_username == bot_username))
        if exists:
            exists.owner_telegram_id = owner_id
            exists.note = note
            exists.bot_token = bot_token
            exists.brand_title = brand_title
            exists.active = True
            api_key = exists.api_key
            partner_id = exists.id
        else:
            row = WhitelabelPartner(
                bot_username=bot_username,
                owner_telegram_id=owner_id,
                api_key=api_key,
                bot_token=bot_token,
                brand_title=brand_title,
                note=note,
            )
            session.add(row)
            await session.flush()
            partner_id = row.id
        await session.commit()
    await state.clear()
    await init_partner_bots()
    await admin_audit.log_action(
        message.from_user.id,
        admin_audit.PARTNER_ADD,
        target=f"@{bot_username}",
        detail="обновлён" if exists else "подключён",
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_PARTNER_INTEGRATION,
                    callback_data=f"{INTEGRATION_CB}{partner_id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [admin_ui.back_button("adm:wlbl")],
        ],
    )
    await admin_ui.show(
        message,
        T.PARTNER_ADDED.format(
            bot_username=bot_username,
            mode=T.PARTNER_MODE_POLLING if bot_token else T.PARTNER_MODE_API,
            api_key=api_key,
        ),
        markup,
        edit=False,
    )
