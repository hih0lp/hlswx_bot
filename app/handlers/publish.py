"""Публикация по активной подписке — фреймы «Опубликовать», «Шаг 1 из 2», «Шаг 2 из 2»."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.core.texts import (
    BTN_HOME,
    ERR_CATEGORY,
    PUB_CITY,
    PUB_IN_PROGRESS,
    PUB_STEP1,
    PUB_STEP2,
    PUBLISH_PAYWALL,
)
from app.config import get_settings
from app.db.session import SessionLocal
from app.keyboards.helpers import PUBLICATION_BTNS
from app.keyboards.main import (
    CITIES_PER_PAGE,
    HOME_CB,
    inline_home_row,
    inline_nav,
    inline_publish_paywall,
    saved_contacts_keyboard,
)
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_PLAIN
from app.models.entities import (
    Chat,
    City,
    Subscription,
    SubscriptionChat,
    SubscriptionStatus,
)
from app.services import banners
from app.services.contacts import list_saved_contacts, remember_contact
from app.services.fraud import check_publish_allowed
from app.services.media import read_ad_content
from app.services.menu_nav import dispatch_menu_button
from app.services.publish import enqueue_subscription_post
from app.services.users import get_or_create_user
from app.services.volume_limits import check_volume_allowed
from app.services.whitelist import get_whitelist_entry_for_user
from app.states.flows import PublishFlow

publish_router = Router()

PUBLISH_CITY_ALL = "__all__"
BACK_TO_CITY = "pub:back:city"
BACK_TO_TEXT = "pub:back:text"
# Отдельный префикс, чтобы страница не попала в обработчик выбора города.
CITY_PAGE_CB = "pub:cpage:"


async def _active_subscriptions(session, user_id: int) -> list[Subscription]:
    from app.services.whitelist import whitelist_access_valid

    now = datetime.now(UTC)
    subs = (
        await session.scalars(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.active,
                Subscription.expires_at > now,
            )
            .order_by(Subscription.id.desc()),
        )
    ).all()
    # Бесплатная подписка белого списка без действующей записи подпиской не считается.
    return [sub for sub in subs if await whitelist_access_valid(session, sub)]


async def _subscription_city_map(session, subscription_id: int) -> dict[str, str]:
    """city_key -> city label для чатов подписки."""
    links = (
        await session.scalars(
            select(SubscriptionChat).where(SubscriptionChat.subscription_id == subscription_id),
        )
    ).all()
    out: dict[str, str] = {}
    for link in links:
        chat = await session.scalar(select(Chat).where(Chat.id == link.chat_id, Chat.active.is_(True)))
        if not chat:
            continue
        city = await session.scalar(select(City).where(City.id == chat.city_id))
        if city:
            out[city.key.lower()] = city.label
    return out


async def _all_user_cities(session, subs: list[Subscription]) -> dict[str, str]:
    out: dict[str, str] = {}
    for sub in subs:
        out.update(await _subscription_city_map(session, sub.id))
    return out


async def _pick_subscription_for_city(session, subs, city_key: str | None) -> Subscription | None:
    if not subs:
        return None
    if not city_key:
        return subs[0]
    key = city_key.lower()
    for sub in subs:
        if key in await _subscription_city_map(session, sub.id):
            return sub
    return subs[0]


def _city_picker_kb(cities: dict[str, str], *, show_all: bool = False, page: int = 0) -> InlineKeyboardMarkup:
    """Фрейм «Опубликовать»: города подписки, по два в ряд, с круговым листанием."""
    items = sorted(cities.items(), key=lambda x: x[1])
    shown, page, pages = page_slice(items, page, CITIES_PER_PAGE)
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(0, len(shown), 2):
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"pub:city:{key}", style=STYLE_PLAIN)
            for key, label in shown[idx:idx + 2]
        ])
    pager = pager_row(CITY_PAGE_CB, page, pages)
    if pager:
        rows.append(pager)
    if show_all and len(items) > 1:
        rows.append([
            InlineKeyboardButton(
                text="🌐 Во все города",
                callback_data=f"pub:city:{PUBLISH_CITY_ALL}",
                style=STYLE_PLAIN,
            ),
        ])
    rows.append([InlineKeyboardButton(text=BTN_HOME, callback_data=HOME_CB, style=STYLE_PLAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@publish_router.callback_query(PublishFlow.choosing_city, F.data.startswith(CITY_PAGE_CB))
async def publish_city_page(callback: CallbackQuery, state: FSMContext) -> None:
    """Перелистывание городов по кругу (ТЗ 6.8)."""
    page = int(callback.data.rsplit(":", 1)[-1])
    data = await state.get_data()
    cities = data.get("city_choices") or {}
    await callback.message.edit_reply_markup(
        reply_markup=_city_picker_kb(cities, show_all=True, page=page),
    )
    await callback.answer()


async def _show_city_picker(
    message: Message,
    state: FSMContext,
    cities: dict[str, str],
    *,
    edit: bool = False,
) -> None:
    await state.set_state(PublishFlow.choosing_city)
    await state.update_data(city_choices=cities)
    await banners.show_screen(
        message,
        banners.PUBLICATION,
        PUB_CITY,
        _city_picker_kb(cities, show_all=True),
        edit=edit,
    )


async def _begin_publish_text(
    message: Message,
    state: FSMContext,
    *,
    subscription_id: int,
    city_key: str = "",
    multi_city: bool = False,
    edit: bool = False,
) -> None:
    await state.set_state(PublishFlow.waiting_text)
    await state.update_data(subscription_id=subscription_id, publish_city_key=city_key or None)
    markup = inline_nav(BACK_TO_CITY) if multi_city else inline_home_row()
    # Шаг 1 из 2 просит прислать текст объявления. Если на него пришли выбором
    # города — перерисовываем тот же экран, иначе уходит новым сообщением.
    await banners.show_prompt(message, banners.PUBLICATION, PUB_STEP1, markup, edit=edit)


@publish_router.message(Command("publish"))
@publish_router.message(F.text.in_(PUBLICATION_BTNS))
async def publish_entry(message: Message, state: FSMContext) -> None:
    await publish_start(message, state)


async def publish_start(message: Message, state: FSMContext, *, tg_user=None, edit: bool = False) -> None:
    user_obj = tg_user or message.from_user
    async with SessionLocal() as session:
        user = await get_or_create_user(session, user_obj)
        subs = await _active_subscriptions(session, user.id)

    if not subs:
        async with SessionLocal() as session:
            entry = await get_whitelist_entry_for_user(session, user_obj.id, user_obj.username)
        if entry:
            await banners.show_screen(
                message,
                banners.ATTENTION,
                "<b>📢 Размещение объявлений</b>\n\n"
                "Доступ выдан, но подписка ещё не активирована.\n"
                "Напишите администратору или попробуйте через минуту.",
                inline_home_row(),
                edit=edit,
            )
            return
        await banners.show_screen(
            message, banners.PUBLICATION, PUBLISH_PAYWALL, inline_publish_paywall(), edit=edit,
        )
        return

    async with SessionLocal() as session:
        cities = await _all_user_cities(session, subs)

    if len(cities) <= 1:
        city_key = next(iter(cities), "")
        async with SessionLocal() as session:
            sub = await _pick_subscription_for_city(session, subs, city_key or None)
        if sub is None:
            await message.answer("Подписка не найдена.", reply_markup=inline_home_row())
            await state.clear()
            return
        await _begin_publish_text(message, state, subscription_id=sub.id, city_key=city_key)
        return

    await _show_city_picker(message, state, cities, edit=edit)


@publish_router.callback_query(F.data == BACK_TO_CITY)
async def publish_back_to_city(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cities = data.get("city_choices") or {}
    await callback.answer()
    if not cities:
        await publish_start(callback.message, state, tg_user=callback.from_user, edit=True)
        return
    await _show_city_picker(callback.message, state, cities, edit=True)


@publish_router.callback_query(PublishFlow.choosing_city, F.data.startswith("pub:city:"))
async def publish_pick_city(callback: CallbackQuery, state: FSMContext) -> None:
    raw_key = callback.data.split(":")[2]
    city_key = "" if raw_key == PUBLISH_CITY_ALL else raw_key

    async with SessionLocal() as session:
        user = await get_or_create_user(session, callback.from_user)
        subs = await _active_subscriptions(session, user.id)
        sub = await _pick_subscription_for_city(session, subs, city_key or None)

    if sub is None:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    await callback.answer()
    await _begin_publish_text(
        callback.message,
        state,
        subscription_id=sub.id,
        city_key=city_key,
        multi_city=True,
        edit=True,
    )


@publish_router.callback_query(F.data == "pub:cancel")
async def publish_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    from app.services.welcome import cancel_inline_flow

    await cancel_inline_flow(callback, state)


@publish_router.message(PublishFlow.waiting_text)
async def publish_text(message: Message, state: FSMContext) -> None:
    if await dispatch_menu_button(message, state):
        return

    data = await state.get_data()
    # Объявление приходит текстом, фото с подписью или одним фото — последнее
    # распознаётся OCR (правка заказчика от 17.09.2026).
    content = await read_ad_content(message)
    if content.empty:
        await banners.show_new_screen(message, banners.ATTENTION, ERR_CATEGORY, inline_nav(BACK_TO_CITY))
        return
    text = content.text

    async with SessionLocal() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.id == data["subscription_id"]))
        if not sub:
            await message.answer("Подписка не найдена.", reply_markup=inline_home_row())
            await state.clear()
            return

        user = await get_or_create_user(session, message.from_user)
        saved = await list_saved_contacts(session, user.id)
        # Сюда попадают только с активной подпиской (см. publish_start), а по
        # решению заказчика у оплативших объявление засчитывается автоматически:
        # сверка с согласованным текстом по умолчанию выключена.
        if get_settings().fraud_check_enabled:
            whitelisted = await get_whitelist_entry_for_user(
                session, message.from_user.id, message.from_user.username,
            )
            if not whitelisted:
                ok, score, _ = check_publish_allowed(sub.approved_text, sub.approved_text_hash, text)
                if not ok:
                    await banners.show_new_screen(
                        message,
                        banners.ATTENTION,
                        "<b>⚠️ Не удалось создать публикацию</b>\n\n"
                        f"Текст слишком отличается от согласованного при оформлении подписки "
                        f"(совпадение {score:.0%}, нужно от 90%).\n"
                        "Менять можно контакты и детали, но не смысл объявления.",
                        inline_home_row(),
                    )
                    await state.clear()
                    return

    await state.update_data(post_text=text, post_photo_id=content.photo_id, saved_contacts=saved)
    await state.set_state(PublishFlow.waiting_contact)
    kb = saved_contacts_keyboard(saved) if saved else inline_nav(BACK_TO_TEXT)
    await banners.show_prompt(message, banners.PUBLICATION, PUB_STEP2, kb)


@publish_router.callback_query(F.data == BACK_TO_TEXT)
async def publish_back_to_text(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.answer()
    await state.set_state(PublishFlow.waiting_text)
    markup = inline_nav(BACK_TO_CITY) if data.get("city_choices") else inline_home_row()
    await banners.show_prompt(callback.message, banners.PUBLICATION, PUB_STEP1, markup, edit=True)


@publish_router.callback_query(PublishFlow.waiting_contact, F.data == "pub:contact:new")
async def publish_contact_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await banners.show_prompt(
        callback.message, banners.PUBLICATION, PUB_STEP2, inline_nav(BACK_TO_TEXT), edit=True,
    )


@publish_router.callback_query(PublishFlow.waiting_contact, F.data.startswith("pub:contact:"))
async def publish_contact_pick(callback: CallbackQuery, state: FSMContext) -> None:
    token = callback.data.split(":", 2)[-1]
    if token == "new":
        return
    data = await state.get_data()
    saved = data.get("saved_contacts") or []
    try:
        contact = saved[int(token)]
    except (ValueError, IndexError):
        await callback.answer("Контакт не найден", show_alert=True)
        return
    await callback.answer()
    await _publish_with_contact(callback.message, state, contact, tg_user=callback.from_user, edit=True)


@publish_router.message(PublishFlow.waiting_contact)
async def publish_contact(message: Message, state: FSMContext) -> None:
    if await dispatch_menu_button(message, state):
        return
    contact = (message.text or "").strip()
    # Колонка `contact` — VARCHAR(255): длинный текст (например, само объявление,
    # присланное на шаге контакта) ронял обновление подписки.
    if len(contact) < 3 or len(contact) > 255:
        await banners.show_new_screen(
            message,
            banners.ATTENTION,
            "Укажите контакт: @username или номер телефона (до 255 символов).",
            inline_nav(BACK_TO_TEXT),
        )
        return
    await _publish_with_contact(message, state, contact, tg_user=message.from_user)


async def _publish_with_contact(
    message: Message,
    state: FSMContext,
    contact: str,
    *,
    tg_user,
    edit: bool = False,
) -> None:
    data = await state.get_data()
    text = data.get("post_text") or ""
    photo_id = data.get("post_photo_id")
    city_key = data.get("publish_city_key")

    async with SessionLocal() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.id == data["subscription_id"]))
        if not sub:
            await message.answer("Подписка не найдена.", reply_markup=inline_home_row())
            await state.clear()
            return
        user = await get_or_create_user(session, tg_user)
        allowed, limit_msg = await check_volume_allowed(session, sub.id, sub.posts_volume or "low")
        if not allowed:
            await state.clear()
            await banners.show_screen(
                message, banners.ATTENTION, limit_msg, inline_home_row(), edit=edit,
            )
            return
        sub.contact = contact
        await session.commit()
        await remember_contact(session, user.id, contact)
        count = await enqueue_subscription_post(
            session, sub.id, text, contact, city_key=city_key, photo_url=photo_id,
        )

    if count == 0:
        await state.clear()
        await banners.show_screen(
            message,
            banners.ATTENTION,
            "<b>⚠️ Не удалось создать публикацию</b>\n\n"
            "Чаты подписки сейчас недоступны. Обратитесь к администратору.",
            inline_home_row(),
            edit=edit,
        )
        return

    await state.clear()
    # Статус — «в очереди» / «опубликовано» / «частично» — придёт отдельным
    # уведомлением из воркера очереди (ТЗ 6.8, app/services/publication_status.py).
    await banners.show_screen(message, banners.PUBLICATION, PUB_IN_PROGRESS, None, edit=edit)
