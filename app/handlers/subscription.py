"""Оформление подписки — фреймы «Шаг 1 … Шаг 5» макета Figma (ТЗ 6.9).

Порядок шагов взят из макета: пример объявления → город → количество
публикаций → чаты → оформление. Обычная подписка заканчивается пятым шагом
(«Оформление подписки»), корпоративная — экраном «Ваш тариф», поэтому в ней
шагов четыре.
"""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.core.texts import (
    CORP_STEPS_TOTAL,
    CORP_TARIFF,
    ERR_BLOCKED,
    ERR_CATEGORY,
    ERR_PAYMENT,
    MYSUB_BUTTON,
    MYSUB_CARD,
    MYSUB_FINISHED,
    MYSUB_MARK_ACTIVE,
    MYSUB_MARK_FINISHED,
    MYSUB_UNTIL,
    MYSUBS_EMPTY,
    MYSUBS_HEADER,
    PAYMENT_CTA,
    SUB_ACCEPTED,
    SUB_ACTIVATED,
    SUB_ANALYZING,
    SUB_STEP1,
    SUB_STEP2,
    SUB_STEP3,
    SUB_STEP3_CORP,
    SUB_STEP4,
    SUB_STEP5,
    SUB_STEPS_TOTAL,
)
from app.db.session import SessionLocal
from app.keyboards.helpers import SUBSCRIPTION_BTN
from app.keyboards.main import (
    chats_keyboard,
    cities_keyboard,
    inline_error_retry,
    inline_back_home_row,
    inline_payment_failed,
    pay_button,
    payment_choice_keyboard,
    subscription_card_keyboard,
    subscriptions_keyboard,
    volume_keyboard,
)
from app.ml.classifier import get_classifier
from app.models.entities import (
    Chat,
    City,
    ClassificationLog,
    Subscription,
    SubscriptionChat,
    SubscriptionStatus,
)
from app.services import banners
from app.services.fraud import text_fingerprint
from app.services.media import read_ad_content
from app.services.menu_nav import dispatch_menu_button
from app.services.payments import (
    PaymentCreationError,
    activate_subscription_whitelist,
    create_subscription_payment,
)
from app.services.pricing import (
    PLAN_CORP_B2B,
    PLAN_STANDARD,
    POSTS_VOLUME_LABELS,
    POSTS_VOLUME_LOW,
    POSTS_VOLUME_MID,
    POSTS_VOLUME_UNLIMITED,
    apply_posts_volume,
    corp_b2b_price_per_chat,
)
from app.services.textfmt import chats_label, format_date
from app.services.users import get_or_create_user
from app.services.wallet import format_rub
from app.services.whitelist import is_user_whitelisted_for_city
from app.states.flows import SubscriptionFlow

subscription_router = Router()

BACK_TO_CITY = "sub:back:city"
BACK_TO_VOLUME = "sub:back:volume"
RETRY_TEXT = "sub:retry:text"

# Префиксы страниц круговой пагинации (ТЗ 6.8).
CITY_PAGE_CB = "sub:cpage:"
CHATS_PAGE_CB = "sub:chats:page:"
LIST_PAGE_CB = "sub:list:page:"


def _steps_total(data: dict) -> int:
    return CORP_STEPS_TOTAL if data.get("corp_mode") else SUB_STEPS_TOTAL


def _placeholder_contact(from_user) -> str:
    return f"@{from_user.username}" if from_user.username else "—"


# Белый список — это бесплатный доступ к своим городам, а не потолок покупки.
# Раньше запись вида `city_keys = ["msk"]` резала здесь весь каталог, и её
# владелец не мог оформить платную подписку ни в одном другом городе — видел
# одну «Москву» (жалоба заказчика от 17.09.2026). Ограничения остались там,
# где им место: в провижининге бесплатного доступа (`services/whitelist.py`).
async def _available_cities(session) -> list[City]:
    cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    return list(cities)


async def _available_chats(session, city_id: int) -> list[Chat]:
    chats = (
        await session.scalars(
            select(Chat).where(Chat.city_id == city_id, Chat.active.is_(True)).order_by(Chat.sort_order),
        )
    ).all()
    return list(chats)


async def _flow_alive(callback: CallbackQuery, data: dict, *keys: str) -> bool:
    """Кнопка из старого сообщения, когда состояние уже сброшено, ничего не ломает."""
    if all(key in data for key in keys):
        return True
    await callback.answer("Оформление уже завершено — начните заново", show_alert=True)
    return False


def _volume_options(data: dict) -> list[tuple[str, str, int | None]]:
    """Варианты объёма для шага «Количество публикаций».

    В корпоративном тарифе цена за чат фиксированная и от объёма не зависит,
    поэтому цену в кнопках не показываем.
    """
    keys = (POSTS_VOLUME_LOW, POSTS_VOLUME_MID, POSTS_VOLUME_UNLIMITED)
    if data.get("corp_mode"):
        return [(key, POSTS_VOLUME_LABELS[key], None) for key in keys]
    base = int(data["base_price_per_chat"])
    category = data["category_code"]
    return [(key, POSTS_VOLUME_LABELS[key], apply_posts_volume(base, category, key)) for key in keys]


# ------------------------------------------------------------------ шаг 1

@subscription_router.message(Command("subscription"))
async def subscription_command(message: Message, state: FSMContext) -> None:
    await subscription_start(message, state)


@subscription_router.message(F.text.in_(SUBSCRIPTION_BTN))
async def subscription_button(message: Message, state: FSMContext) -> None:
    await subscription_start(message, state)


async def subscription_start(message: Message, state: FSMContext, *, tg_user=None) -> None:
    await state.clear()
    await state.update_data(plan_type=PLAN_STANDARD, corp_mode=False)
    # Шаг 1 просит прислать пример объявления — такой экран всегда новым
    # сообщением, чтобы просьба осталась над ответом пользователя.
    await banners.show_prompt(
        message,
        banners.AD,
        SUB_STEP1.format(total=SUB_STEPS_TOTAL),
        inline_back_home_row(),
    )
    await state.set_state(SubscriptionFlow.waiting_text)


async def corp_subscription_start(message: Message, state: FSMContext, *, tg_user=None) -> None:
    """Та же воронка, но для корпоративной подписки с API-интеграцией."""
    await state.clear()
    await state.update_data(plan_type=PLAN_CORP_B2B, corp_mode=True)
    await banners.show_prompt(
        message,
        banners.AD,
        SUB_STEP1.format(total=CORP_STEPS_TOTAL),
        inline_back_home_row(),
    )
    await state.set_state(SubscriptionFlow.waiting_text)


@subscription_router.callback_query(F.data == RETRY_TEXT)
async def subscription_retry_text(callback: CallbackQuery, state: FSMContext) -> None:
    """«✏️ Изменить текст» с экранов ошибок — снова просим прислать объявление."""
    data = await state.get_data()
    await callback.answer()
    await state.set_state(SubscriptionFlow.waiting_text)
    await banners.show_prompt(
        callback.message,
        banners.AD,
        SUB_STEP1.format(total=_steps_total(data)),
        inline_back_home_row(),
        edit=True,
    )


async def _error_screen(message: Message, text: str) -> None:
    """Фреймы ошибок — всегда новым сообщением: перед ними пользователь вводил текст."""
    await banners.show_new_screen(message, banners.ATTENTION, text, inline_error_retry(RETRY_TEXT))


async def _drop_message(message: Message | None) -> None:
    """Убрать промежуточный статус «Анализируем», чтобы он не копился в переписке."""
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        pass


@subscription_router.message(SubscriptionFlow.waiting_text)
async def subscription_text(message: Message, state: FSMContext) -> None:
    if await dispatch_menu_button(message, state):
        return

    # Пример объявления принимаем и картинкой: текст для тарифа берём из
    # подписи, а у фото без подписи — из OCR (правка заказчика от 17.09.2026).
    content = await read_ad_content(message)
    if content.empty:
        await _error_screen(message, ERR_CATEGORY)
        return
    text = content.text

    analyzing = await message.answer(SUB_ANALYZING)
    result = get_classifier().classify(text)

    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user)
        log = ClassificationLog(
            user_id=user.id,
            text=text,
            predicted_category=result.category_code,
            confidence=result.confidence,
        )
        session.add(log)
        await session.commit()
        log_id = log.id

    if result.category_code == "INCOMPLETE":
        await _drop_message(analyzing)
        await _error_screen(message, ERR_CATEGORY)
        return

    if result.blocked:
        await _drop_message(analyzing)
        await _error_screen(message, ERR_BLOCKED)
        return

    data = await state.get_data()
    corp = bool(data.get("corp_mode"))
    await state.update_data(
        ad_text=text,
        category_code="CORP_B2B" if corp else result.category_code,
        base_price_per_chat=corp_b2b_price_per_chat() if corp else result.price_per_chat,
        confidence=result.confidence,
        log_id=log_id,
        selected_chat_ids=[],
    )

    async with SessionLocal() as session:
        cities = await _available_cities(session)
    if not cities:
        await _drop_message(analyzing)
        await message.answer(
            "Нет доступных городов по вашему доступу. Обратитесь к администратору.",
            reply_markup=inline_back_home_row(),
        )
        await state.clear()
        return

    # «Анализируем» превращаем в «Объявление получено» — это фреймы одного места
    # в макете, и лишнего сообщения в переписке не остаётся.
    try:
        await analyzing.edit_text(SUB_ACCEPTED)
    except Exception:
        await _drop_message(analyzing)
        await message.answer(SUB_ACCEPTED)

    await state.set_state(SubscriptionFlow.selecting_city)
    await state.update_data(city_page=0)
    await banners.show_new_screen(
        message,
        banners.AD,
        SUB_STEP2.format(total=_steps_total(await state.get_data())),
        cities_keyboard(cities, "sub:city:", RETRY_TEXT, page=0, page_prefix=CITY_PAGE_CB),
    )

    if result.needs_review and not corp:
        await _notify_admins_review(text, result, log_id)


async def _notify_admins_review(text: str, result, log_id: int) -> None:
    """Низкая уверенность модели — тариф проверит администратор.

    Пользователю об этом не сообщаем: в макете такой строки нет, а тариф
    ему уже показан.
    """
    from app.bot.runtime import get_bot
    from app.keyboards.admin import admin_log_keyboard
    from app.services import access, tariffs

    bot = get_bot()
    # Клавиатура категорий синхронная и читает кэш как есть — прогреваем.
    await tariffs.ensure_loaded()
    label = tariffs.get(result.category_code).label
    # Только администраторам: раздел проверки категорий менеджеру закрыт.
    for admin_id in await access.admin_ids():
        try:
            await bot.send_message(
                admin_id,
                f"⚠️ <b>Тариф на проверке</b> #{log_id}\n"
                f"Категория: {label} ({result.confidence:.0%})\n\n{text[:500]}",
                reply_markup=admin_log_keyboard(log_id),
            )
        except Exception:
            pass


# ------------------------------------------------------------------ шаг 2

@subscription_router.callback_query(F.data == BACK_TO_CITY)
async def subscription_back_to_city(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        cities = await _available_cities(session)
    await state.set_state(SubscriptionFlow.selecting_city)
    await callback.answer()
    await banners.show_screen(
        callback.message,
        banners.AD,
        SUB_STEP2.format(total=_steps_total(data)),
        cities_keyboard(cities, "sub:city:", RETRY_TEXT, page=int(data.get("city_page") or 0), page_prefix=CITY_PAGE_CB),
        edit=True,
    )


@subscription_router.callback_query(SubscriptionFlow.selecting_city, F.data.startswith(CITY_PAGE_CB))
async def subscription_city_page(callback: CallbackQuery, state: FSMContext) -> None:
    """Перелистывание городов по кругу (ТЗ 6.8)."""
    page = int(callback.data.rsplit(":", 1)[-1])
    await state.update_data(city_page=page)
    async with SessionLocal() as session:
        cities = await _available_cities(session)
    await callback.message.edit_reply_markup(
        reply_markup=cities_keyboard(cities, "sub:city:", RETRY_TEXT, page=page, page_prefix=CITY_PAGE_CB),
    )
    await callback.answer()


@subscription_router.callback_query(SubscriptionFlow.selecting_city, F.data.startswith("sub:city:"))
async def subscription_city(callback: CallbackQuery, state: FSMContext) -> None:
    city_id = int(callback.data.split(":")[-1])
    await state.update_data(city_id=city_id, selected_chat_ids=[])
    data = await state.get_data()
    if not await _flow_alive(callback, data, "base_price_per_chat", "category_code"):
        return
    await state.set_state(SubscriptionFlow.selecting_volume)
    await callback.answer()
    await _show_volume_step(callback, data)


# ------------------------------------------------------------------ шаг 3

async def _show_volume_step(callback: CallbackQuery, data: dict) -> None:
    """Шаг 3. Цены стоят в подписях кнопок, в тексте их больше нет (ТЗ 6.8)."""
    options = _volume_options(data)
    has_prices = any(price is not None for _key, _label, price in options)
    text = (
        SUB_STEP3 if has_prices else SUB_STEP3_CORP
    ).format(total=_steps_total(data))
    await banners.show_screen(
        callback.message,
        banners.AD,
        text,
        volume_keyboard(options, BACK_TO_CITY),
        edit=True,
    )


@subscription_router.callback_query(F.data == BACK_TO_VOLUME)
async def subscription_back_to_volume(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _flow_alive(callback, data, "base_price_per_chat", "category_code"):
        return
    await state.set_state(SubscriptionFlow.selecting_volume)
    await callback.answer()
    await _show_volume_step(callback, data)


@subscription_router.callback_query(SubscriptionFlow.selecting_volume, F.data.startswith("sub:vol:"))
async def subscription_volume(callback: CallbackQuery, state: FSMContext) -> None:
    volume = callback.data.split(":")[-1]
    data = await state.get_data()
    if not await _flow_alive(callback, data, "city_id", "base_price_per_chat"):
        return
    if data.get("corp_mode"):
        price_per = corp_b2b_price_per_chat()
    else:
        price_per = apply_posts_volume(int(data["base_price_per_chat"]), data["category_code"], volume)
    await state.update_data(posts_volume=volume, price_per_chat=price_per)

    async with SessionLocal() as session:
        chats = await _available_chats(session, data["city_id"])
    if not chats:
        await callback.answer("В этом городе пока нет доступных чатов", show_alert=True)
        return

    await state.set_state(SubscriptionFlow.selecting_chats)
    await state.update_data(chats_page=0)
    await callback.answer()
    await banners.show_screen(
        callback.message,
        banners.AD,
        SUB_STEP4.format(total=_steps_total(data)),
        chats_keyboard(chats, set(), BACK_TO_VOLUME),
        edit=True,
    )


# ------------------------------------------------------------------ шаг 4

@subscription_router.callback_query(SubscriptionFlow.selecting_chats, F.data.startswith(CHATS_PAGE_CB))
async def subscription_chats_page(callback: CallbackQuery, state: FSMContext) -> None:
    """Перелистывание чатов: отметки сохраняются, они живут в FSM, а не на кнопках."""
    page = int(callback.data.rsplit(":", 1)[-1])
    data = await state.get_data()
    if not await _flow_alive(callback, data, "city_id"):
        return
    await state.update_data(chats_page=page)
    async with SessionLocal() as session:
        chats = await _available_chats(session, data["city_id"])
    await callback.message.edit_reply_markup(
        reply_markup=chats_keyboard(chats, set(data.get("selected_chat_ids") or []), BACK_TO_VOLUME, page=page),
    )
    await callback.answer()


@subscription_router.callback_query(SubscriptionFlow.selecting_chats, F.data.startswith("sub:chat:"))
async def subscription_toggle_chat(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    if not await _flow_alive(callback, data, "city_id"):
        return
    selected = set(data.get("selected_chat_ids") or [])
    selected.symmetric_difference_update({chat_id})
    await state.update_data(selected_chat_ids=list(selected))

    async with SessionLocal() as session:
        chats = await _available_chats(session, data["city_id"])
    await callback.message.edit_reply_markup(
        reply_markup=chats_keyboard(chats, selected, BACK_TO_VOLUME, page=int(data.get("chats_page") or 0)),
    )
    await callback.answer()


@subscription_router.callback_query(SubscriptionFlow.selecting_chats, F.data == "sub:chats:done")
async def subscription_chats_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _flow_alive(callback, data, "city_id", "price_per_chat", "ad_text"):
        return
    selected = data.get("selected_chat_ids") or []
    if not selected:
        await callback.answer("Выберите хотя бы один чат", show_alert=True)
        return

    # Проверки границ whitelist здесь нет намеренно: это платная подписка,
    # её область задаёт сам покупатель (см. комментарий у `_available_cities`).
    total = int(data["price_per_chat"]) * len(selected)
    await state.update_data(total_price=total)
    await callback.answer()
    await _finalize_subscription(
        callback.message,
        state,
        {**data, "total_price": total, "selected_chat_ids": selected},
        tg_user=callback.from_user,
        edit=True,
    )


# ------------------------------------------------------------------ шаг 5

def _summary(city_label: str, chats_count: int, volume_key: str) -> dict:
    return {
        "city": city_label,
        "chats": chats_label(chats_count),
        "volume": POSTS_VOLUME_LABELS.get(volume_key, POSTS_VOLUME_LABELS[POSTS_VOLUME_LOW]),
    }


async def _finalize_subscription(
    message: Message,
    state: FSMContext,
    data: dict,
    *,
    tg_user,
    edit: bool = False,
) -> None:
    """Экран «Оформление подписки» / «Ваш тариф» и переход к оплате."""
    corp = bool(data.get("corp_mode"))
    selected = data["selected_chat_ids"]
    total = int(data["total_price"])
    sub_id = 0

    try:
        async with SessionLocal() as session:
            user = await get_or_create_user(session, tg_user)
            city = await session.scalar(select(City).where(City.id == data["city_id"]))
            city_key = city.key if city else ""
            city_label = city.label if city else "—"

            sub = Subscription(
                user_id=user.id,
                category_code=data["category_code"],
                approved_text=data["ad_text"],
                approved_text_hash=text_fingerprint(data["ad_text"]),
                contact=_placeholder_contact(tg_user),
                price_per_chat=int(data["price_per_chat"]),
                total_price=total,
                posts_volume=data.get("posts_volume", POSTS_VOLUME_LOW),
                plan_type=data.get("plan_type", PLAN_STANDARD),
                status=SubscriptionStatus.pending_payment,
            )
            session.add(sub)
            await session.flush()
            for chat_id in selected:
                session.add(SubscriptionChat(subscription_id=sub.id, chat_id=chat_id))
            await session.commit()
            sub_id = sub.id

            summary = _summary(city_label, len(selected), data.get("posts_volume", POSTS_VOLUME_LOW))

            whitelisted, _ = await is_user_whitelisted_for_city(
                session, tg_user.id, city_key, selected, username=tg_user.username,
            )
            if whitelisted:
                await activate_subscription_whitelist(session, user.id, sub_id)
                await state.clear()
                await banners.show_screen(
                    message,
                    banners.SUBSCRIPTION,
                    SUB_ACTIVATED.format(**summary),
                    inline_back_home_row(),
                    edit=edit,
                )
                return

            await session.refresh(user)
            balance = user.balance or Decimal("0")
            screen = (
                CORP_TARIFF.format(price=format_rub(total), **summary)
                if corp
                else SUB_STEP5.format(price=format_rub(total), **summary)
            )

            if balance >= total and total > 0:
                await state.clear()
                await banners.show_screen(
                    message,
                    banners.AD,
                    screen,
                    payment_choice_keyboard("sub", sub_id, total, balance),
                    edit=edit,
                )
                return

            _, url = await create_subscription_payment(
                session, user.id, sub_id, total, f"Подписка HLSWX #{sub_id}",
            )
    except PaymentCreationError:
        await state.clear()
        await banners.show_screen(
            message,
            banners.ATTENTION,
            ERR_PAYMENT,
            inline_payment_failed(f"sub:pay:{sub_id}"),
            edit=edit,
        )
        return

    await state.clear()
    await banners.show_screen(
        message,
        banners.AD,
        screen + PAYMENT_CTA,
        pay_button(url, f"💳 Оплатить {format_rub(total)} ₽"),
        edit=edit,
    )


@subscription_router.callback_query(F.data.startswith("sub:pay:"))
async def subscription_retry_payment(callback: CallbackQuery) -> None:
    """«💳 Повторить» с экрана «Не удалось провести оплату»."""
    sub_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.id == sub_id))
        if not sub or sub.status != SubscriptionStatus.pending_payment:
            await callback.answer("Подписка не найдена или уже оплачена", show_alert=True)
            return
        await callback.answer()
        total = int(sub.total_price)
        try:
            _, url = await create_subscription_payment(
                session, sub.user_id, sub.id, total, f"Подписка HLSWX #{sub.id}",
            )
        except PaymentCreationError:
            await banners.show_screen(
                callback.message,
                banners.ATTENTION,
                ERR_PAYMENT,
                inline_payment_failed(f"sub:pay:{sub_id}"),
                edit=True,
            )
            return

    await banners.show_screen(
        callback.message,
        banners.AD,
        PAYMENT_CTA.strip(),
        pay_button(url, f"💳 Оплатить {format_rub(total)} ₽"),
        edit=True,
    )


# ---------------------------------------------------------- мои подписки

async def _subscription_city_and_chats(session, subscription_id: int) -> tuple[str, list[Chat]]:
    links = (
        await session.scalars(
            select(SubscriptionChat).where(SubscriptionChat.subscription_id == subscription_id),
        )
    ).all()
    chats: list[Chat] = []
    city_label = "—"
    for link in links:
        chat = await session.scalar(select(Chat).where(Chat.id == link.chat_id))
        if not chat:
            continue
        chats.append(chat)
        if city_label == "—":
            city = await session.scalar(select(City).where(City.id == chat.city_id))
            if city:
                city_label = city.label
    return city_label, chats


def _subscription_status_line(sub: Subscription) -> str:
    from datetime import UTC, datetime

    expired = not sub.expires_at or sub.expires_at <= datetime.now(UTC)
    if expired or sub.status != SubscriptionStatus.active:
        return MYSUB_FINISHED.format(date=format_date(sub.expires_at))
    return MYSUB_UNTIL.format(date=format_date(sub.expires_at))


@subscription_router.message(Command("mysubs"))
@subscription_router.message(F.text.in_({"💳 Мои подписки", "📋 Мои подписки"}))
async def mysubs_entry(message: Message) -> None:
    await my_subscriptions(message)


async def my_subscriptions(message: Message, *, tg_user=None, edit: bool = False, page: int = 0) -> None:
    """Фрейм «Мои подписки»: список кнопок по подпискам."""
    user_obj = tg_user or message.from_user
    async with SessionLocal() as session:
        user = await get_or_create_user(session, user_obj)
        subs = (
            await session.scalars(
                select(Subscription)
                .where(
                    Subscription.user_id == user.id,
                    Subscription.status != SubscriptionStatus.pending_payment,
                )
                .order_by(Subscription.id.desc()),
            )
        ).all()
        items = []
        active = 0
        finished = 0
        for sub in subs:
            city_label, chats = await _subscription_city_and_chats(session, sub.id)
            status = _subscription_status_line(sub)
            is_finished = status.startswith(MYSUB_MARK_FINISHED)
            if is_finished:
                finished += 1
            else:
                active += 1
            # Дата в кнопку не влезает — она в карточке подписки (ТЗ 6.8).
            items.append((
                sub.id,
                MYSUB_BUTTON.format(
                    mark=MYSUB_MARK_FINISHED if is_finished else MYSUB_MARK_ACTIVE,
                    city=city_label,
                    chats=chats_label(len(chats)),
                ),
            ))

    if not items:
        await banners.show_screen(message, banners.SUBSCRIPTION, MYSUBS_EMPTY, inline_back_home_row(), edit=edit)
        return

    from app.services.welcome import subscriptions_status_text

    await banners.show_screen(
        message,
        banners.SUBSCRIPTION,
        MYSUBS_HEADER.format(status=subscriptions_status_text(active, finished)),
        subscriptions_keyboard(items, page=page),
        edit=edit,
    )


@subscription_router.callback_query(F.data.startswith(LIST_PAGE_CB))
async def subscriptions_page(callback: CallbackQuery) -> None:
    """Перелистывание «Моих подписок» по кругу."""
    page = int(callback.data.rsplit(":", 1)[-1])
    await callback.answer()
    await my_subscriptions(callback.message, tg_user=callback.from_user, edit=True, page=page)


@subscription_router.callback_query(F.data.startswith("sub:card:"))
async def subscription_card(callback: CallbackQuery) -> None:
    """Фрейм «Моя подписка»."""
    sub_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        user = await get_or_create_user(session, callback.from_user)
        sub = await session.scalar(select(Subscription).where(Subscription.id == sub_id))
        if not sub or sub.user_id != user.id:
            await callback.answer("Подписка не найдена", show_alert=True)
            return
        city_label, chats = await _subscription_city_and_chats(session, sub.id)

    await callback.answer()
    await banners.show_screen(
        callback.message,
        banners.SUBSCRIPTION,
        MYSUB_CARD.format(
            status=_subscription_status_line(sub),
            city=city_label,
            chats=chats_label(len(chats)),
            price=format_rub(int(sub.total_price)),
        ),
        subscription_card_keyboard(chats),
        edit=True,
    )
