"""Раздел «Белый список» (ТЗ этапа 2, 6.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.core import admin_texts as T
from app.core.texts import BTN_BACK, BTN_DONE
from app.db.session import SessionLocal
from app.handlers.admin.common import LIST_PER_PAGE, WL_PER_PAGE, _is_admin
from app.handlers.admin.home import _show_panel
from app.keyboards.main import selection_button
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_DANGER, STYLE_MAIN, STYLE_PLAIN
from app.models.entities import Chat, City, User, WhitelistEntry
from app.services import admin_audit, admin_ui
from app.services.textfmt import chats_label, format_date
from app.services.whitelist import (
    parse_chat_ids,
    parse_city_keys,
    provision_whitelist_subscription,
)
from app.states.admin import AdminFlow

router = Router()


# Раздел открывается двумя путями, и так это и нарисовано в макете: `338:453`
# из меню панели и `349:522` из «Управления» (ТЗ 6.3 прямо оговаривает оба
# пути). Экран один и тот же, но ветка «Найти пользователя» у них разная —
# видно по стрелкам страницы, а не по кадрам:
#
#   338:453 ─[🔎 Найти пользователя]→ 338:654  поиск ЗАПИСИ белого списка
#   349:522 ─[🔎 Найти пользователя]→ 349:564  поиск ПОЛЬЗОВАТЕЛЯ бота
#
# Разница осмысленная: из панели ищут существующую запись, чтобы посмотреть
# или удалить; из «Управления» ищут любого пользователя, чтобы его добавить —
# у карточки `349:967` для этого и стоит кнопка «⭐ Белый список».
MANAGE_CB = "adm:manage"
MANAGE_WL_CB = "adm:manage:wl"


@router.callback_query(F.data == "adm:wl")
async def admin_wl_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Экран раздела из меню панели — кадр макета 338:453."""
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    await admin_ui.show(callback, *await _wl_menu_screen())


@router.callback_query(F.data == MANAGE_WL_CB)
async def admin_manage_wl_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Тот же экран, но из «Управления» — кадр макета 349:522."""
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    await admin_ui.show(callback, *await _wl_menu_screen(from_manage=True))


async def _wl_menu_screen(*, from_manage: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        rows = list((await session.scalars(select(WhitelistEntry))).all())
    expired = sum(1 for r in rows if r.expires_at and r.expires_at <= now)
    text = T.WHITELIST.format(total=len(rows), active=len(rows) - expired, expired=expired)
    find_cb = "adm:users" if from_manage else "adm:wl:find"
    back = (
        admin_ui.back_button(MANAGE_CB, T.BTN_BACK_TO_MANAGE)
        if from_manage
        else admin_ui.panel_button()
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_WL_ADD, callback_data="adm:wl:add", style=STYLE_MAIN)],
            [InlineKeyboardButton(text=T.BTN_WL_ALL, callback_data="adm:wl:list", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_WL_FIND, callback_data=find_cb, style=STYLE_PLAIN)],
            [back],
        ],
    )
    return text, markup


def entry_label(entry: WhitelistEntry, number: int) -> str:
    """Подпись записи в списке — кадр 338:725, одна строка.

    До 20.09.2026 подпись собиралась в три строки по кадру 349:580 — тот же
    экран, нарисованный иначе: под номером шли охват и срок. Заказчик указал
    на 338:725: в списке остаются номер и username, остальное видно в карточке.
    """
    who = f"@{entry.username}" if entry.username else (entry.telegram_id or "—")
    return f"#{number} · {who}"


def _term_label(entry: WhitelistEntry) -> str:
    if entry.expires_at is None:
        return T.WL_TERM_FOREVER
    return T.WL_TERM_UNTIL.format(date=format_date(entry.expires_at))


async def _load_city_labels() -> dict[str, str]:
    """{ключ города: название} в порядке справочника городов бота."""
    async with SessionLocal() as session:
        rows = (await session.scalars(select(City).order_by(City.id))).all()
    return {city.key: city.label for city in rows}


def _cities_of(entry: WhitelistEntry, labels: dict[str, str] | None = None) -> str:
    """Города записи названиями («Москва, Санкт-Петербург»), а не ключами (msk, spb).

    Города, которых уже нет в справочнике, показываем ключом — чтобы запись не
    выглядела пустой.
    """
    keys = parse_city_keys(entry.city_keys)
    if not keys:
        return "Все города"
    labels = labels or {}
    known = [labels[key] for key in labels if key in keys]
    unknown = sorted(key for key in keys if key not in labels)
    return ", ".join(known + unknown)


def _chats_of(entry: WhitelistEntry) -> str:
    chats = parse_chat_ids(entry.chat_ids)
    return "Все чаты" if not chats else chats_label(len(chats))


def _wl_card(
    entry: WhitelistEntry, labels: dict[str, str] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка записи — кадр макета 338:661.

    «← Назад» ведёт на шаг назад — в список записей (заказчик 21.09.2026).
    Кнопки в одну колонку: «Изменить», «Удалить» (без красной заливки), «Назад».
    """
    expired = bool(entry.expires_at and entry.expires_at <= datetime.now(UTC))
    text = T.WL_CARD.format(
        who=f"@{entry.username}" if entry.username else "—",
        telegram_id=entry.telegram_id or "—",
        status=T.WL_STATUS_EXPIRED if expired else T.WL_STATUS_ACTIVE,
        cities=_cities_of(entry, labels),
        chats=_chats_of(entry),
        term=_term_label(entry),
    )
    if entry.comment:
        text += f"\n\n<i>{entry.comment}</i>"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_WL_EDIT,
                    callback_data=f"adm:wl:edit:{entry.id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=T.BTN_WL_DELETE,
                    callback_data=f"adm:wl:del:{entry.id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [admin_ui.back_button("adm:wl:list")],
        ],
    )
    return text, markup


async def _show_exists(message: Message, entry: WhitelistEntry, *, who: str) -> None:
    """«Не удалось добавить» — кадр макета 340:677.

    Состояние ввода не сбрасываем: чаще всего администратор просто ошибся
    пользователем и тут же вводит следующего. Кнопка ведёт на карточку —
    оттуда запись правят, если охват всё-таки не тот.
    """
    labels = await _load_city_labels()
    await admin_ui.show(
        message,
        T.WL_ADD_EXISTS.format(
            who=who, cities=_cities_of(entry, labels), term=_term_label(entry),
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=T.BTN_WL_OPEN,
                        callback_data=f"adm:wl:card:{entry.id}",
                        style=STYLE_PLAIN,
                    ),
                ],
                [admin_ui.back_button("adm:wl")],
            ],
        ),
        edit=False,
    )


@router.callback_query(F.data == "adm:wl:find")
async def admin_wl_find_start(callback: CallbackQuery, state: FSMContext) -> None:
    """«Найти пользователя» — кадр макета 338:654."""
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.whitelist_find)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.WL_FIND_ASK,
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]]),
    )


@router.message(AdminFlow.whitelist_find)
async def admin_wl_find(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    raw = (message.text or "").strip().lstrip("@")
    async with SessionLocal() as session:
        if raw.isdigit():
            entry = await session.scalar(
                select(WhitelistEntry).where(WhitelistEntry.telegram_id == int(raw)),
            )
        else:
            entry = await session.scalar(
                select(WhitelistEntry).where(func.lower(WhitelistEntry.username) == raw.lower()),
            )
    if entry is None:
        await admin_ui.show(
            message,
            T.WL_NOT_FOUND.format(who=f"@{raw}"),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=T.BTN_WL_ADD_TO_LIST, callback_data="adm:wl:add", style=STYLE_MAIN)],
                    [admin_ui.back_button("adm:wl")],
                ],
            ),
            edit=False,
        )
        return
    text, markup = _wl_card(entry, await _load_city_labels())
    await admin_ui.show(message, text, markup, edit=False)


@router.callback_query(F.data.startswith("adm:wl:card:"))
async def admin_wl_card(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    entry_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        entry = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.id == entry_id))
    if entry is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await callback.answer()
    text, markup = _wl_card(entry, await _load_city_labels())
    await admin_ui.show(callback, text, markup)


@router.callback_query(F.data.startswith("adm:wl:edit:"))
async def admin_wl_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """«✏️ Изменить» на карточке — кнопка кадра 338:661.

    Отдельного экрана правки в макете нет, и заводить его не нужно: меняются
    ровно те три вещи, которые спрашивает мастер добавления, — города, чаты и
    срок. Поэтому правка — это тот же мастер, запущенный с уже отмеченным
    охватом записи. Сохранение в конце (`admin_wl_save`) существующую запись и
    так находит по telegram_id или username и переписывает, а не плодит новую.
    """
    if not _is_admin(callback.from_user.id):
        return
    entry_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        entry = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.id == entry_id))
        if entry is None:
            await callback.answer("Запись не найдена", show_alert=True)
            return
        cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    if not cities:
        await callback.answer()
        await admin_ui.show(
            callback,
            T.WL_NO_CITIES,
            InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]]),
        )
        return

    selected = set(parse_city_keys(entry.city_keys))
    await state.set_state(AdminFlow.whitelist_cities)
    await state.update_data(
        wl_edit_id=entry.id,
        wl_tg_id=entry.telegram_id,
        wl_username=entry.username,
        wl_pending=entry.telegram_id is None,
        wl_selected_cities=sorted(selected),
        # `parse_*` отдают множества, а состояние уезжает в Redis через JSON —
        # множество там не сериализуется. Весь мастер хранит эти два поля
        # списками, правка не исключение.
        wl_selected_chats=sorted(parse_chat_ids(entry.chat_ids)),
        wl_cities_page=0,
    )
    await callback.answer()
    await admin_ui.show(
        callback,
        T.WL_PICK_CITIES.format(who=_who(await state.get_data())),
        _wl_cities_keyboard(list(cities), selected),
    )


async def _render_wl_list(page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Страница белого списка. None — список пуст."""
    async with SessionLocal() as session:
        rows = (await session.scalars(select(WhitelistEntry).order_by(WhitelistEntry.id))).all()
    if not rows:
        return None
    now = datetime.now(UTC)
    expired = sum(1 for r in rows if r.expires_at and r.expires_at <= now)
    shown, page, pages = page_slice(list(rows), page, LIST_PER_PAGE)

    # Кадр 338:725: под сводкой идут только кнопки-записи, каждая в одну
    # строку. Раньше то же самое дублировалось ещё и текстом над кнопками —
    # из-за этого экран упирался в лимит подписи к баннеру (1024 символа), а
    # комментарий администратора приходилось резать. Теперь текста ровно
    # столько, сколько в макете, а комментарий целиком виден в карточке.
    text = T.WL_LIST.format(total=len(rows), active=len(rows) - expired, expired=expired)
    buttons: list[list[InlineKeyboardButton]] = []
    # Номер — порядковый по действующим записям (заказчик 21.09.2026): удалённые
    # не оставляют дыр, а сама запись открывается по своему id в callback.
    for number, row in enumerate(shown, start=page * LIST_PER_PAGE + 1):
        buttons.append([
            InlineKeyboardButton(
                text=entry_label(row, number),
                callback_data=f"adm:wl:card:{row.id}",
                style=STYLE_PLAIN,
            ),
        ])

    pager = pager_row("adm:wl:lpage:", page, pages)
    if pager:
        buttons.append(pager)
    # Под пагинацией — только возврат. «Найти» и «Добавить» тут стояли по
    # кадру 349:580; заказчик 20.09.2026 указал следовать 338:725, где под
    # списком одна кнопка, а добавляют и ищут с экрана раздела.
    buttons.append([admin_ui.back_button("adm:wl")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "adm:wl:list")
@router.callback_query(F.data.startswith("adm:wl:lpage:"))
async def admin_wl_list(callback: CallbackQuery) -> None:
    """«Все записи» — кадр макета 338:725, листается по кругу."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    page = int(callback.data.rsplit(":", 1)[-1]) if callback.data.startswith("adm:wl:lpage:") else 0
    rendered = await _render_wl_list(page)
    if rendered is None:
        await admin_ui.show(
            callback,
            T.WL_LIST_EMPTY,
            InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]]),
        )
        return
    await admin_ui.show(callback, *rendered)


@router.callback_query(F.data.startswith("adm:wl:del:"))
async def admin_wl_del_cb(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    entry_id = int(callback.data.split(":")[-1])
    label = ""
    revoked = 0
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.id == entry_id))
        if row:
            if row.telegram_id:
                label = str(row.telegram_id)
            elif row.username:
                label = f"@{row.username}"
            else:
                label = f"#{row.id}"
            from app.services.whitelist import revoke_whitelist_access

            revoked = await revoke_whitelist_access(session, row.telegram_id, row.username)
            await session.delete(row)
            await session.commit()
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.WHITELIST_DELETE, target=label or f"#{entry_id}",
        detail=f"подписок закрыто: {revoked}",
    )
    await callback.answer("Удалён")
    await admin_ui.show(
        callback,
        T.WL_DELETED.format(who=f"<code>{label}</code>"),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl:list")]]),
    )


@router.callback_query(F.data == "adm:wl:add")
async def admin_wl_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.whitelist_username)
    # `wl_edit_id` гасим здесь же: после правки записи состояние не очищается
    # до конца мастера, и без сброса добавление отчиталось бы «обновлена».
    await state.update_data(wl_selected_cities=[], wl_selected_chats=[], wl_edit_id=None)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.WL_ADD_ASK,
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]]),
    )


@router.message(AdminFlow.whitelist_username)
async def admin_wl_username(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    if message.text in ("⬅️ В админку", "◀️ В админку") or (message.text and message.text.startswith("/admin")):
        await state.clear()
        if message.text and message.text.startswith("/admin"):
            await _show_panel(message)
        return

    raw = (message.text or "").strip()
    from app.bot.runtime import get_bot
    from app.services.telegram_resolve import resolve_telegram_user

    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]])
    resolved = await resolve_telegram_user(get_bot(), raw)
    if not resolved.telegram_id and not resolved.username:
        await admin_ui.show(message, T.WL_ADD_BAD, back, edit=False)
        return

    tg_id = resolved.telegram_id
    username = resolved.username
    pending = resolved.pending_username

    # getChat часто не резолвит @username — ищем в нашей таблице users
    if pending and username and not tg_id:
        async with SessionLocal() as session:
            known = await session.scalar(
                select(User).where(func.lower(User.username) == username.lower()),
            )
            if known:
                tg_id = known.telegram_id
                pending = False
                username = (known.username or username).lstrip("@").lower()

    if pending and username:
        async with SessionLocal() as session:
            exists = await session.scalar(
                select(WhitelistEntry).where(func.lower(WhitelistEntry.username) == username.lower()),
            )
            if exists:
                await _show_exists(message, exists, who=f"@{username}")
                return

    if tg_id and not pending:
        async with SessionLocal() as session:
            exists = await session.scalar(
                select(WhitelistEntry).where(WhitelistEntry.telegram_id == int(tg_id)),
            )
            if exists:
                await _show_exists(message, exists, who=f"<code>{tg_id}</code>")
                return

    await state.update_data(wl_tg_id=tg_id, wl_username=username, wl_pending=pending)
    await state.set_state(AdminFlow.whitelist_found)
    text, markup = _wl_found_screen(await state.get_data())
    await admin_ui.show(message, text, markup, edit=False)


def _wl_found_screen(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «✅ Пользователь найден» — после ввода ника, перед выбором города."""
    username, tg_id = data.get("wl_username"), data.get("wl_tg_id")
    text = T.WL_FOUND.format(
        username=f"@{username}" if username else "—",
        telegram_id=tg_id if tg_id else "—",
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_WL_CONTINUE,
                    callback_data="adm:wl:found:go",
                    style=STYLE_MAIN,
                ),
            ],
            # Назад на один шаг — снова ввод ника.
            [admin_ui.back_button("adm:wl:add")],
        ],
    )
    return text, markup


def _who_short(data: dict) -> str:
    """Подпись пользователя на шаге выбора города: «@username», без ID."""
    username, tg_id = data.get("wl_username"), data.get("wl_tg_id")
    return f"@{username}" if username else f"<code>{tg_id}</code>"


# «Нижний Новгород» в кнопке в половину ряда обрезается («Нижний Новгоро…»),
# «Санкт-Петербург» помещается впритык: всё длиннее этого — отдельным рядом.
CITY_HALF_WIDTH_CHARS = 14


def _pack_half_width(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """Короткие кнопки по две в ряд, длинные — отдельной строкой на всю ширину."""
    rows: list[list[InlineKeyboardButton]] = []
    pending: list[InlineKeyboardButton] = []
    for button in buttons:
        if len(button.text) > CITY_HALF_WIDTH_CHARS:
            if pending:
                rows.append(pending)
                pending = []
            rows.append([button])
            continue
        pending.append(button)
        if len(pending) == 2:
            rows.append(pending)
            pending = []
    if pending:
        rows.append(pending)
    return rows


def _wl_city_pick_keyboard(cities: list[City], *, page: int = 0) -> InlineKeyboardMarkup:
    """Выбор города при добавлении: по две кнопки в ряд, «Все города» и «← Назад».

    Мультивыбора и «Сохранить» здесь нет: нажатие города сразу ведёт дальше.
    """
    shown, page, pages = page_slice(cities, page, WL_PER_PAGE)
    buttons = [
        InlineKeyboardButton(text=city.label, callback_data=f"adm:wl:city:{city.key}", style=STYLE_PLAIN)
        for city in shown
    ]
    rows = _pack_half_width(buttons)
    pager = pager_row("adm:wl:cpage:", page, pages)
    if pager:
        rows.append(pager)
    rows.append([InlineKeyboardButton(text="🌍 Все города", callback_data="adm:wl:city:all", style=STYLE_PLAIN)])
    rows.append([admin_ui.back_button("adm:wl:found:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminFlow.whitelist_cities, F.data == "adm:wl:found:back")
async def admin_wl_found_back(callback: CallbackQuery, state: FSMContext) -> None:
    """«← Назад» с выбора города — на экран «Пользователь найден»."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    await state.set_state(AdminFlow.whitelist_found)
    text, markup = _wl_found_screen(await state.get_data())
    await admin_ui.show(callback, text, markup)


@router.callback_query(AdminFlow.whitelist_found, F.data == "adm:wl:found:go")
async def admin_wl_found_continue(callback: CallbackQuery, state: FSMContext) -> None:
    """«Продолжить →» на экране «Пользователь найден» — выбор городов."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]])
    async with SessionLocal() as session:
        cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    if not cities:
        await admin_ui.show(callback, T.WL_NO_CITIES, back)
        return
    await state.set_state(AdminFlow.whitelist_cities)
    await state.update_data(wl_cities_page=0)
    await admin_ui.show(
        callback,
        T.WL_PICK_CITY.format(who=_who_short(await state.get_data())),
        _wl_city_pick_keyboard(list(cities)),
    )


def _who(data: dict) -> str:
    """Подпись пользователя на шагах записи — одна на все экраны сценария."""
    username, tg_id = data.get("wl_username"), data.get("wl_tg_id")
    if data.get("wl_pending"):
        return f"<b>@{username}</b> <i>(ожидает /start)</i>"
    return f"<code>{tg_id}</code>" + (f" @{username}" if username else "")


def _cities_label(city_keys) -> str:
    return "Все города" if not city_keys else ", ".join(sorted(city_keys))


def _wl_cities_keyboard(cities: list[City], selected: set[str], *, page: int = 0) -> InlineKeyboardMarkup:
    """Выбор городов — та же механика, что в пользовательском шаге 4.

    Отличие одно: здесь пустой выбор означает «все города», поэтому кнопка
    завершения видна всегда, а не только при непустом выборе.
    """
    shown, page, pages = page_slice(cities, page, WL_PER_PAGE)
    rows = [
        [selection_button(city.label, f"adm:wl:city:{city.key}", selected=city.key in selected)]
        for city in shown
    ]
    pager = pager_row("adm:wl:cpage:", page, pages)
    if pager:
        rows.append(pager)
    done = f"✅ Сохранить · {len(selected)}" if selected else "✅ Сохранить"
    rows.append([
        InlineKeyboardButton(text="🌍 Все города", callback_data="adm:wl:city:all", style=STYLE_PLAIN),
        InlineKeyboardButton(text=done, callback_data="adm:wl:city:save", style=STYLE_MAIN),
    ])
    rows.append([InlineKeyboardButton(text=BTN_BACK, callback_data="adm:wl", style=STYLE_PLAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _wl_chats_mode_keyboard() -> InlineKeyboardMarkup:
    """Кадр 338:537 — «Все чаты» или «Выбрать чаты»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_WL_CHATS_ALL, callback_data="adm:wl:chatmode:all", style=STYLE_MAIN,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=T.BTN_WL_CHATS_PICK, callback_data="adm:wl:chatmode:pick", style=STYLE_PLAIN,
                ),
            ],
            # На два шага назад — к «Пользователь найден» (при правке — к карточке записи).
            [InlineKeyboardButton(text=BTN_BACK, callback_data="adm:wl:chatsback", style=STYLE_PLAIN)],
        ],
    )


def _wl_chats_keyboard(chats: list[Chat], selected: set[int], *, page: int = 0) -> InlineKeyboardMarkup:
    """Кадр 338:724 — мультивыбор ровно как у подписчика на шаге 4.

    Кнопки «Все чаты» здесь нет: этот вариант выбирается развилкой до входа
    сюда, а зелёная «Готово» появляется только после первого отмеченного чата
    — как в `keyboards.main.chats_keyboard`.
    """
    shown, page, pages = page_slice(chats, page, WL_PER_PAGE)
    rows = []
    for chat in shown:
        topic = f" · {chat.topic}" if chat.topic else ""
        rows.append([
            selection_button(
                f"@{chat.telegram_username}{topic}",
                f"adm:wl:chat:{chat.id}",
                selected=chat.id in selected,
            ),
        ])
    pager = pager_row("adm:wl:chpage:", page, pages)
    if pager:
        rows.append(pager)
    if selected:
        rows.append([
            InlineKeyboardButton(
                text=f"{BTN_DONE} · {chats_label(len(selected))}",
                callback_data="adm:wl:chat:save",
                style=STYLE_MAIN,
            ),
        ])
    rows.append([
        InlineKeyboardButton(text=BTN_BACK, callback_data="adm:wl:chatmode", style=STYLE_PLAIN),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminFlow.whitelist_cities, F.data.startswith("adm:wl:cpage:"))
async def admin_wl_cities_page(callback: CallbackQuery, state: FSMContext) -> None:
    """Перелистывание городов: выбор живёт в FSM и переживает смену страницы."""
    if not _is_admin(callback.from_user.id):
        return
    page = int(callback.data.rsplit(":", 1)[-1])
    await state.update_data(wl_cities_page=page)
    data = await state.get_data()
    async with SessionLocal() as session:
        cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    if data.get("wl_edit_id") is None:
        markup = _wl_city_pick_keyboard(list(cities), page=page)
    else:
        markup = _wl_cities_keyboard(list(cities), set(data.get("wl_selected_cities") or []), page=page)
    await callback.message.edit_reply_markup(reply_markup=markup)
    await callback.answer()


@router.callback_query(AdminFlow.whitelist_chats, F.data.startswith("adm:wl:chpage:"))
async def admin_wl_chats_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    page = int(callback.data.rsplit(":", 1)[-1])
    await state.update_data(wl_chats_page=page)
    data = await state.get_data()
    async with SessionLocal() as session:
        chats = await _wl_chats_for_state(session, data)
    await callback.message.edit_reply_markup(
        reply_markup=_wl_chats_keyboard(chats, set(data.get("wl_selected_chats") or []), page=page),
    )
    await callback.answer()


async def _wl_chats_for_state(session, data: dict) -> list[Chat]:
    city_keys = data.get("wl_selected_cities") or []
    cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    if city_keys:
        city_ids = [c.id for c in cities if c.key in city_keys]
    else:
        city_ids = [c.id for c in cities]
    if not city_ids:
        return []
    return list(
        await session.scalars(
            select(Chat).where(Chat.city_id.in_(city_ids), Chat.active.is_(True)).order_by(Chat.sort_order),
        ),
    )


@router.callback_query(AdminFlow.whitelist_cities, F.data.startswith("adm:wl:city:"))
async def admin_wl_toggle_city(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    key = callback.data.split(":")[-1]
    data = await state.get_data()
    if key != "save" and data.get("wl_edit_id") is None:
        # Добавление: один город (или «Все города») — и сразу дальше, без «Сохранить».
        await state.update_data(wl_selected_cities=[] if key == "all" else [key])
        key = "save"
    if key == "save":
        await callback.answer()
        data = await state.get_data()
        city_keys = data.get("wl_selected_cities") or []
        async with SessionLocal() as session:
            cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
            if city_keys:
                city_ids = [c.id for c in cities if c.key in city_keys]
            else:
                city_ids = [c.id for c in cities]
            if not city_ids:
                await admin_ui.show(
                    callback,
                    T.WL_NO_CITIES,
                    InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]]),
                )
                return
            chats = (
                await session.scalars(
                    select(Chat)
                    .where(Chat.city_id.in_(city_ids), Chat.active.is_(True))
                    .order_by(Chat.sort_order),
                )
            ).all()
        if not chats:
            await callback.answer()
            await _ask_term(callback, state)
            return
        await _ask_chats_mode(callback, state)
        return
    selected = set(data.get("wl_selected_cities") or [])
    if key == "all":
        selected = set()
        await state.update_data(wl_selected_cities=[])
    else:
        if key in selected:
            selected.remove(key)
        else:
            selected.add(key)
        await state.update_data(wl_selected_cities=list(selected))
    async with SessionLocal() as session:
        cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    await callback.message.edit_reply_markup(
        reply_markup=_wl_cities_keyboard(list(cities), selected, page=int(data.get("wl_cities_page") or 0)),
    )
    await callback.answer()


async def _ask_chats_mode(callback: CallbackQuery, state: FSMContext) -> None:
    """Кадр 338:537 — «Все чаты» или «Выбрать чаты».

    Выбор при входе сбрасывается: сюда возвращает «Назад» с мультивыбора, и
    отмеченные там чаты не должны утечь в вариант «все».
    """
    await state.set_state(AdminFlow.whitelist_chats_mode)
    await state.update_data(wl_selected_chats=[], wl_chats_page=0)
    data = await state.get_data()
    await admin_ui.show(
        callback,
        T.WL_CHATS_MODE.format(
            who=_who(data), cities=_cities_label(data.get("wl_selected_cities")),
        ),
        _wl_chats_mode_keyboard(),
    )


@router.callback_query(AdminFlow.whitelist_chats_mode, F.data == "adm:wl:chatsback")
async def admin_wl_chats_back(callback: CallbackQuery, state: FSMContext) -> None:
    """«← Назад» с «Выберите чаты» — на два шага назад (заказчик 21.09.2026).

    При добавлении это «Пользователь найден» (выбор города пропускается), при
    правке записи — её карточка. Остальные кнопки мастера не менялись.
    """
    if not _is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    entry_id = data.get("wl_edit_id")
    if entry_id is None:
        await callback.answer()
        await state.set_state(AdminFlow.whitelist_found)
        text, markup = _wl_found_screen(data)
        await admin_ui.show(callback, text, markup)
        return
    async with SessionLocal() as session:
        entry = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.id == entry_id))
    await state.clear()
    await callback.answer()
    if entry is None:
        await admin_ui.show(
            callback,
            T.WL_NOT_FOUND.format(who="Запись"),
            InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]]),
        )
        return
    text, markup = _wl_card(entry, await _load_city_labels())
    await admin_ui.show(callback, text, markup)


@router.callback_query(
    StateFilter(AdminFlow.whitelist_chats_mode, AdminFlow.whitelist_chats),
    F.data.startswith("adm:wl:chatmode"),
)
async def admin_wl_chats_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    choice = callback.data.rsplit(":", 1)[-1]
    # «Назад» с мультивыбора приходит без варианта — рисуем развилку заново.
    if choice == "chatmode":
        await callback.answer()
        await _ask_chats_mode(callback, state)
        return
    if choice == "all":
        # Пустой выбор и означает «все чаты» — см. `services/whitelist.py`.
        await state.update_data(wl_selected_chats=[])
        await callback.answer()
        await _ask_term(callback, state)
        return

    async with SessionLocal() as session:
        chats = await _wl_chats_for_state(session, await state.get_data())
    await state.set_state(AdminFlow.whitelist_chats)
    await state.update_data(wl_selected_chats=[], wl_chats_page=0)
    await callback.answer()
    await admin_ui.show(
        callback,
        T.WL_PICK_CHATS.format(
            cities=_cities_label((await state.get_data()).get("wl_selected_cities")),
        ),
        _wl_chats_keyboard(chats, set()),
    )


@router.callback_query(AdminFlow.whitelist_chats, F.data.startswith("adm:wl:chat:"))
async def admin_wl_toggle_chat(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    token = callback.data.split(":")[-1]
    data = await state.get_data()
    if token == "save":
        await callback.answer()
        await _ask_term(callback, state)
        return
    selected = set(data.get("wl_selected_chats") or [])
    chat_id = int(token)
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        selected.add(chat_id)
    await state.update_data(wl_selected_chats=list(selected))
    async with SessionLocal() as session:
        chats = await _wl_chats_for_state(session, data)
    await callback.message.edit_reply_markup(
        reply_markup=_wl_chats_keyboard(chats, selected, page=int(data.get("wl_chats_page") or 0)),
    )
    await callback.answer()


async def _ask_term(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг «Срок доступа» — кадр макета 349:713."""
    await state.set_state(AdminFlow.whitelist_term)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_WL_FOREVER, callback_data="adm:wl:term:forever", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_WL_UNTIL, callback_data="adm:wl:term:date", style=STYLE_PLAIN)],
            [admin_ui.back_button("adm:wl")],
        ],
    )
    await admin_ui.show(callback, T.WL_TERM_ASK, markup)


@router.callback_query(AdminFlow.whitelist_term, F.data == "adm:wl:term:forever")
async def admin_wl_term_forever(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.update_data(wl_expires_at=None)
    await callback.answer()
    await _finish_save(callback, state, comment=None, set_comment=False)


@router.callback_query(AdminFlow.whitelist_term, F.data == "adm:wl:term:date")
async def admin_wl_term_date(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.whitelist_term_date)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.WL_TERM_DATE_ASK,
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]]),
    )


@router.message(AdminFlow.whitelist_term_date)
async def admin_wl_term_date_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]])
    raw = (message.text or "").strip()
    try:
        day = datetime.strptime(raw, "%d.%m.%Y").replace(tzinfo=UTC)
    except ValueError:
        await admin_ui.show(message, T.WL_TERM_DATE_BAD, back, edit=False)
        return
    # Конец выбранного дня: «до 20.09» значит, что 20-е ещё действует.
    expires = day.replace(hour=23, minute=59, second=59)
    if expires <= datetime.now(UTC):
        await admin_ui.show(message, T.WL_TERM_DATE_PAST, back, edit=False)
        return

    await state.update_data(wl_expires_at=expires.isoformat())
    await _finish_save(message, state, comment=None, set_comment=False)


@router.message(AdminFlow.whitelist_comment)
async def admin_wl_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    comment = (message.text or "").strip()
    if comment == "-":
        comment = None
    await _finish_save(message, state, comment=comment, set_comment=True)


async def _finish_save(
    dest: Message | CallbackQuery,
    state: FSMContext,
    *,
    comment: str | None,
    set_comment: bool,
) -> None:
    """Записать запись белого списка и показать итоговый экран.

    Шага «комментарий» в макете нет: итог показываем сразу после срока.
    `set_comment=False` — комментарий не трогаем (у существующей записи он
    остаётся как был, у новой пуст).
    """
    data = await state.get_data()
    tg_id = data.get("wl_tg_id")
    pending = bool(data.get("wl_pending"))
    cities = data.get("wl_selected_cities") or []
    chat_ids = data.get("wl_selected_chats") or []
    username = (data.get("wl_username") or "").strip().lstrip("@") or None
    raw_expires = data.get("wl_expires_at")
    expires_at = datetime.fromisoformat(raw_expires) if raw_expires else None
    if username:
        username = username.lower()
    sub_note = ""
    bound_tg = int(tg_id) if tg_id else None
    async with SessionLocal() as session:
        from app.services.whitelist import try_bind_entry_from_existing_user

        if pending or not tg_id:
            row = None
            if username:
                row = await session.scalar(
                    select(WhitelistEntry).where(func.lower(WhitelistEntry.username) == username.lower()),
                )
        else:
            row = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.telegram_id == int(tg_id)))
        if row:
            row.city_keys = json.dumps(cities)
            row.chat_ids = json.dumps(chat_ids)
            row.username = username or row.username
            if set_comment:
                row.comment = comment
            row.expires_at = expires_at
            if tg_id and not row.telegram_id:
                row.telegram_id = int(tg_id)
        else:
            row = WhitelistEntry(
                telegram_id=int(tg_id) if tg_id else None,
                username=username,
                city_keys=json.dumps(cities),
                chat_ids=json.dumps(chat_ids),
                comment=comment,
                expires_at=expires_at,
            )
            session.add(row)
        await session.commit()
        await session.refresh(row)

        # Если юзер уже писал боту — привязать telegram_id сразу (getChat часто не резолвит)
        row = await try_bind_entry_from_existing_user(session, row)
        if row and row.telegram_id:
            bound_tg = int(row.telegram_id)

        if row and row.telegram_id:
            ok, reason, sub_id = await provision_whitelist_subscription(session, row)
            if ok and sub_id:
                from app.services.notifications import notify_payment_success

                user = await session.scalar(select(User).where(User.telegram_id == row.telegram_id))
                if user and reason != "synced":
                    await notify_payment_success(user.id, "subscription_whitelist", sub_id)
                sub_note = f"\n📋 Подписка <b>#{sub_id}</b> активирована автоматически."
            elif reason == "no_chats":
                sub_note = "\n⚠️ Подписка не создана: нет активных чатов по выбранному охвату."
            elif reason == "user_not_started":
                sub_note = "\n⚠️ Пользователь ещё не писал боту — подписка создастся после /start."
            elif reason == "pending_user":
                sub_note = "\n⏳ Подписка включится после <b>/start</b> пользователя."
        elif row and not row.telegram_id:
            sub_note = (
                "\n⏳ Пользователь ещё не найден в боте — "
                "подписка включится после его <b>/start</b>."
            )

    await state.clear()
    scope = "все города" if not cities else ", ".join(cities)
    chat_scope = f" · чаты: {len(chat_ids)}" if chat_ids else " · все чаты"
    if bound_tg and username:
        who = f"<code>{bound_tg}</code> @{username}"
    elif bound_tg:
        who = f"<code>{bound_tg}</code>"
    elif username:
        who = f"@{username}"
    else:
        who = "пользователь"
    editing = bool(data.get("wl_edit_id"))
    await admin_audit.log_action(
        dest.from_user.id,
        admin_audit.WHITELIST_EDIT if editing else admin_audit.WHITELIST_ADD,
        target=str(bound_tg or username or "—"),
        detail=f"города: {scope}{chat_scope}",
    )
    city_names: dict[str, str] = {}
    if cities:
        async with SessionLocal() as session:
            city_names = {
                c.key: c.label
                for c in (await session.scalars(select(City).where(City.key.in_(cities)))).all()
            }
    cities_view = "Все города" if not cities else ", ".join(city_names.get(k, k) for k in cities)
    if editing:
        who_view = who
    else:
        who_view = f"@{username}" if username else (f"<code>{bound_tg}</code>" if bound_tg else who)
    text = (T.WL_UPDATED if editing else T.WL_SAVED).format(
        who=who_view,
        cities=cities_view,
        chats=f"{len(chat_ids)} шт." if chat_ids else "все чаты",
        term="Навсегда" if expires_at is None else f"До {format_date(expires_at)}",
    )
    if editing:
        markup = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:wl")]])
    else:
        # Кнопка ведёт в раздел — подпись как в макете.
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=T.BTN_WHITELIST, callback_data="adm:wl", style=STYLE_PLAIN)],
            ],
        )
    await admin_ui.show(
        dest,
        text + sub_note,
        markup,
        edit=False if isinstance(dest, Message) else None,
    )


@router.message(Command("whitelist_add"))
async def whitelist_add(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 3:
        await message.answer(
            "Формат: /whitelist_add <telegram_id> <city_keys> [chat_ids] [comment]\n"
            "city_keys: msk,spb или all\n"
            "chat_ids: 1,2,3 или -\n"
            "Или /admin → Whitelist",
        )
        return
    tg_id = int(parts[1])
    raw_cities = parts[2].strip().lower()
    cities: list[str] = [] if raw_cities in {"all", "*", "все"} else [x.strip().lower() for x in parts[2].split(",") if x.strip()]
    chat_ids: list[int] = []
    comment = None
    if len(parts) > 3:
        raw_chats = parts[3].strip()
        if raw_chats not in {"-", ""}:
            for item in raw_chats.split(","):
                item = item.strip()
                if item:
                    chat_ids.append(int(item))
        comment = parts[4] if len(parts) > 4 else None
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.telegram_id == tg_id))
        if row:
            row.city_keys = json.dumps(cities)
            row.chat_ids = json.dumps(chat_ids)
            row.comment = comment
        else:
            row = WhitelistEntry(
                telegram_id=tg_id,
                city_keys=json.dumps(cities),
                chat_ids=json.dumps(chat_ids),
                comment=comment,
            )
            session.add(row)
        await session.commit()
        sub_note = ""
        ok, reason, sub_id = await provision_whitelist_subscription(session, row)
        if ok and sub_id:
            from app.services.notifications import notify_payment_success

            user = await session.scalar(select(User).where(User.telegram_id == tg_id))
            if user:
                await notify_payment_success(user.id, "subscription_whitelist", sub_id)
            sub_note = f"\n📋 Подписка <b>#{sub_id}</b> активирована автоматически."
        elif reason == "no_chats":
            sub_note = "\n⚠️ Подписка не создана: нет активных чатов."
        elif reason == "user_not_started":
            sub_note = "\n⚠️ Пользователь ещё не писал боту — подписка создастся после /start."
    scope = "все города" if not cities else ", ".join(cities)
    chats_scope = "все чаты" if not chat_ids else ", ".join(str(x) for x in chat_ids)
    await message.answer(f"✅ Whitelist: <code>{tg_id}</code> → {scope} · чаты: {chats_scope}{sub_note}")


@router.message(Command("whitelist_del"))
async def whitelist_del(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /whitelist_del <telegram_id>")
        return
    tg_id = int(parts[1])
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.telegram_id == tg_id))
        if row:
            from app.services.whitelist import revoke_whitelist_access

            await revoke_whitelist_access(session, row.telegram_id, row.username)
            await session.delete(row)
            await session.commit()
    await message.answer(f"🗑 Удалён: <code>{tg_id}</code>")
