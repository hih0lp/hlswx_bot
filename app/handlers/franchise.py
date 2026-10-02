from __future__ import annotations

import logging
import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import WhitelabelPartner
from app.services.b2b import build_wl_publish_url
from app.services.tenant import current_partner_id, current_partner_owner_id, current_partner_tier
from app.services.users import get_or_create_user
from app.states.flows import FranchiseOnboarding, FranchisePayout

franchise_router = Router()
logger = logging.getLogger("franchise")


async def _partners_for_user(telegram_id: int) -> list[WhitelabelPartner]:
    """Return every clone owned by a user, newest first."""
    async with SessionLocal() as session:
        rows = await session.scalars(
            select(WhitelabelPartner)
            .where(WhitelabelPartner.owner_telegram_id == telegram_id)
            .order_by(WhitelabelPartner.id.desc())
        )
        return list(rows.all())


async def _partner_for_user(
    telegram_id: int,
    partner_id: int,
    *,
    active: bool | None = None,
) -> WhitelabelPartner | None:
    async with SessionLocal() as session:
        query = select(WhitelabelPartner).where(
            WhitelabelPartner.owner_telegram_id == telegram_id,
            WhitelabelPartner.id == partner_id,
        )
        if active is not None:
            query = query.where(WhitelabelPartner.active.is_(active))
        return await session.scalar(query)


async def show_franchise(message: Message, *, tg_user=None, edit: bool = False) -> None:
    """Фрейм «Франшиза».

    В основном боте владелец видит список всех своих клонов и может подключить
    ещё один. В клоне отображается только панель текущей франшизы.
    """
    if current_partner_id():
        clone_user = tg_user or message.from_user
        if not (clone_user and clone_user.id == current_partner_owner_id()):
            # Тариф, статус и оплата франшизы — только для владельца клона.
            await message.answer("Раздел доступен только владельцу бота.")
            return
        await _clone_franchise_panel(message, clone_user, edit=edit)
        return
    user_obj = tg_user or message.from_user
    partners = _without_unpaid_attempts(await _partners_for_user(user_obj.id))
    if not partners:
        await _franchise_start_screen(message, edit=edit)
        return
    await _owned_franchise_panel(message, user_obj.id, partners, edit=edit)


def _without_unpaid_attempts(partners: list[WhitelabelPartner]) -> list[WhitelabelPartner]:
    """Попытки оплаты, не доведённые до конца, пользователю не показываем.

    Выбрал тариф, открыл оплату и вышел — в «Мои боты-франшизы» такая попытка
    не попадает и кнопки «Продолжить …» от неё нет: следующий вход начинается с
    чистого экрана. Саму запись из базы не удаляем — если оплата по старой
    ссылке всё же пройдёт, платёж должен найти своего партнёра; а новый выбор
    тарифа переиспользует эту же запись (`franchise_join_tier`).
    """
    return [p for p in partners if p.franchise_billing_status != "pending"]


async def _franchise_start_screen(message: Message, *, edit: bool = False) -> None:
    """Стартовый экран «Франшиза» — для тех, у кого ещё нет ни одного бота."""
    from app.core.texts import BTN_BACK, BTN_FRANCHISE_START, FRANCHISE_TEXT
    from app.services import banners

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_FRANCHISE_START, callback_data="franchise:join", style=STYLE_MAIN)],
            [InlineKeyboardButton(text=BTN_BACK, callback_data="menu:home", style=STYLE_PLAIN)],
        ],
    )
    await banners.show_screen(message, banners.FRANCHISE, FRANCHISE_TEXT, kb, edit=edit, replace=edit)


@franchise_router.message(Command("franchise"))
@franchise_router.message(F.text == "🏷 Франшиза")
async def franchise_entry(message: Message, state) -> None:
    await state.clear()
    await show_franchise(message)


def _join_tier_keyboard() -> InlineKeyboardMarkup:
    from app.services.franchise_billing import TIER_LABELS, TIER_PRICES
    rows = [
        [InlineKeyboardButton(
            text=f"{TIER_LABELS[tier]} · {TIER_PRICES[tier]} ₽/мес",
            callback_data=f"franchise:join:tier:{tier}",
            style=STYLE_PLAIN,
        )]
        for tier in ("basic", "standard", "premium")
    ]
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="menu:home", style=STYLE_PLAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _pending_franchise_panel(message: Message, partner: WhitelabelPartner) -> None:
    from app.services.franchise_billing import TIER_LABELS, TIER_PRICES
    tier = partner.franchise_tier if partner.franchise_tier in TIER_PRICES else "basic"
    text = (
        f"<b>Подключение @{partner.bot_username} подготовлено</b>\n\n"
        f"Тариф: {TIER_LABELS[tier]} — {TIER_PRICES[tier]} ₽/мес\n"
        "Завершите оплату, чтобы запустить клон."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Продолжить оплату", callback_data=f"franchise:resume:{partner.id}", style=STYLE_MAIN)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home", style=STYLE_PLAIN)],
    ])
    await message.answer(text, reply_markup=keyboard)


async def _send_franchise_checkout(message: Message, partner_id: int, telegram_id: int, *, edit: bool = False) -> None:
    """Правило как в admin_ui: клик по кнопке перерисовывает экран на месте
    (`edit=True`), а не копит новые сообщения при каждой попытке оплаты.
    """
    from app.core.texts import FRANCHISE_PAYMENT_TEXT
    from app.services import banners
    from app.services.franchise_billing import (
        TIER_PRICES,
        FranchiseBotAlreadyRegistered,
        start_plan_checkout,
    )
    async with SessionLocal() as session:
        partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
    if not partner or partner.owner_telegram_id != telegram_id:
        await banners.show_screen(
            message, banners.PLAIN, "Подключение не найдено. Откройте раздел «Франшиза» заново.",
            edit=edit, replace=edit,
        )
        return
    tier = partner.franchise_tier
    try:
        url = await start_plan_checkout(partner_id, telegram_id, tier)
    except FranchiseBotAlreadyRegistered:
        await banners.show_screen(
            message, banners.PLAIN,
            "Этот бот уже зарегистрирован. Оплата не создана. "
            "Используйте отдельного бота, который ещё не подключался.",
            edit=edit, replace=edit,
        )
        return
    except Exception:
        logger.exception("Could not prepare first franchise checkout partner_id=%s", partner_id)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить оплату", callback_data=f"franchise:resume:{partner_id}", style=STYLE_MAIN)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home", style=STYLE_PLAIN)],
        ])
        await banners.show_screen(
            message, banners.PLAIN,
            "Не удалось создать ссылку оплаты. Подключение сохранено — попробуйте позже через раздел «Франшиза».",
            keyboard, edit=edit, replace=edit,
        )
        return
    price = TIER_PRICES.get(tier, TIER_PRICES["basic"])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {price} ₽", url=url, style=STYLE_MAIN)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home", style=STYLE_PLAIN)],
    ])
    await banners.show_screen(
        message, banners.PLAIN, FRANCHISE_PAYMENT_TEXT.format(price=price),
        keyboard, edit=edit, replace=edit,
    )


@franchise_router.callback_query(F.data == "franchise:join")
async def franchise_join(callback, state) -> None:
    if current_partner_id():
        await callback.answer("Из бота-клона подключение недоступно", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    from app.services import banners
    await banners.show_screen(
        callback.message,
        banners.PLAIN,
        "<b>Подключение бота-клона</b>\nВыберите тариф франшизы:",
        _join_tier_keyboard(),
        edit=True,
        replace=True,
    )


@franchise_router.callback_query(F.data.startswith("franchise:join:tier:"))
async def franchise_join_tier(callback, state) -> None:
    """Тариф выбран — сразу оплата (ТЗ «Оплата -> Создание бота»): бот и его
    токен появляются только после успешного платежа.

    Повторный выбор тарифа без оплаты переиспользует уже заведённую
    неоплаченную запись вместо того, чтобы плодить новую на каждый клик —
    иначе в «Мои боты-франшизы» копятся дубли «ожидает оплаты».
    """
    if current_partner_id():
        await callback.answer("Из бота-клона подключение недоступно", show_alert=True)
        return
    from app.services.franchise_billing import TIER_PRICES
    tier = callback.data.rsplit(":", 1)[-1]
    if tier not in TIER_PRICES:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    async with SessionLocal() as session:
        pending = await session.scalar(
            select(WhitelabelPartner).where(
                WhitelabelPartner.owner_telegram_id == callback.from_user.id,
                WhitelabelPartner.bot_username.is_(None),
                WhitelabelPartner.franchise_billing_status == "pending",
            ).order_by(WhitelabelPartner.id.desc())
        )
        if pending:
            pending.franchise_tier = tier
            pending.owner_username = callback.from_user.username
            partner = pending
        else:
            owner = await get_or_create_user(session, callback.from_user)
            partner = WhitelabelPartner(
                bot_username=None,
                owner_telegram_id=callback.from_user.id,
                owner_username=callback.from_user.username,
                api_key=None,
                bot_token=None,
                brand_title=None,
                linked_user_id=owner.id,
                franchise_tier=tier,
                franchise_billing_status="pending",
                active=False,
            )
            session.add(partner)
        await session.commit()
        await session.refresh(partner)
        partner_id = partner.id
    await _send_franchise_checkout(callback.message, partner_id, callback.from_user.id, edit=True)


@franchise_router.callback_query(F.data.startswith("franchise:resume:"))
async def franchise_resume(callback) -> None:
    if current_partner_id():
        await callback.answer("Недоступно в боте-клоне", show_alert=True)
        return
    try:
        partner_id = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Подключение не найдено", show_alert=True)
        return
    partner = await _partner_for_user(callback.from_user.id, partner_id, active=False)
    if not partner or partner.franchise_billing_status == "awaiting_token":
        await callback.answer("Подключение не найдено", show_alert=True)
        return
    await callback.answer()
    await _send_franchise_checkout(callback.message, partner.id, callback.from_user.id, edit=True)


@franchise_router.callback_query(F.data.startswith("franchise:token:"))
async def franchise_enter_token(callback, state) -> None:
    """Оплата уже прошла — просим токен @BotFather (экран «Создайте своего бота»)."""
    if current_partner_id():
        await callback.answer("Недоступно в боте-клоне", show_alert=True)
        return
    try:
        partner_id = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Подключение не найдено", show_alert=True)
        return
    partner = await _partner_for_user(callback.from_user.id, partner_id, active=False)
    if not partner or partner.franchise_billing_status != "awaiting_token":
        await callback.answer("Подключение не найдено", show_alert=True)
        return
    from app.core.texts import FRANCHISE_CREATE_BOT_TEXT
    await state.clear()
    await state.update_data(franchise_partner_id=partner.id)
    await state.set_state(FranchiseOnboarding.waiting_bot_token)
    await callback.answer()
    await callback.message.answer(FRANCHISE_CREATE_BOT_TEXT, reply_markup=_cancel_kb("franchise:cancel_token"))


@franchise_router.callback_query(F.data == "franchise:cancel_token")
async def franchise_token_cancel(callback, state) -> None:
    if current_partner_id():
        await callback.answer("Недоступно в боте-клоне", show_alert=True)
        return
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _owned_franchise_panel(callback.message, callback.from_user.id, edit=False)


@franchise_router.message(FranchiseOnboarding.waiting_bot_token)
async def franchise_receive_token(message: Message, state) -> None:
    if current_partner_id():
        await state.clear()
        return
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if token.lower() in {"/cancel", "отмена", "назад"}:
        await state.clear()
        await message.answer("Подключение отменено. Оплата сохранена — вернитесь в «Франшиза», чтобы указать токен бота позже.")
        return
    if len(token) > 256 or ":" not in token:
        await message.answer("Токен выглядит некорректно. Отправьте токен из @BotFather или напишите «Отмена».")
        return
    from app.config import get_settings
    if token == get_settings().bot_token:
        await message.answer("Нельзя подключить основной бот. Нужен отдельный бот-клон из @BotFather.")
        return

    data = await state.get_data()
    partner_id = data.get("franchise_partner_id")
    if not partner_id:
        await state.clear()
        await message.answer("Шаг оплаты сброшен. Откройте «Франшиза» и начните подключение заново.")
        return

    from app.core.texts import FRANCHISE_CONNECTED_TEXT, FRANCHISE_CONNECTING_TEXT
    status_message = await message.answer(FRANCHISE_CONNECTING_TEXT)

    from app.bot.runtime import check_partner_token
    valid, bot_identity = await check_partner_token(token)
    if not valid or not bot_identity.startswith("@"):
        await status_message.edit_text("Telegram не подтвердил токен. Проверьте его в @BotFather и отправьте снова.")
        return
    username = bot_identity[1:]

    async with SessionLocal() as session:
        existing = await session.scalar(select(WhitelabelPartner).where(func.lower(WhitelabelPartner.bot_username) == username.lower()))
        if existing:
            await status_message.edit_text("Этот бот уже зарегистрирован. Используйте отдельного бота, который ещё не подключался.")
            return
        partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
        if not partner or partner.owner_telegram_id != message.from_user.id or partner.franchise_billing_status != "awaiting_token":
            await status_message.edit_text("Оплаченное подключение не найдено. Откройте «Франшиза» и попробуйте снова.")
            return
        partner.bot_username = username
        partner.api_key = secrets.token_urlsafe(32)
        partner.bot_token = token
        partner.brand_title = username
        partner.active = True
        partner.franchise_billing_status = "active" if partner.franchise_payment_method_id else "manual"
        await session.commit()
        launched_id = partner.id
        billing_status = partner.franchise_billing_status
        tier = partner.franchise_tier

    await state.clear()
    from app.bot.runtime import request_partner_bots_reload, set_partner_billing_state
    set_partner_billing_state(launched_id, billing_status, tier)
    request_partner_bots_reload()
    await _notify_admins_partner_connected(message.from_user, username, tier, launched_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🤖 Открыть @{username}", url=f"https://t.me/{username}", style=STYLE_MAIN)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home", style=STYLE_PLAIN)],
    ])
    await status_message.edit_text(FRANCHISE_CONNECTED_TEXT, reply_markup=kb)


async def _notify_admins_partner_connected(owner_user, bot_username: str, tier: str, partner_id: int) -> None:
    """Информационное уведомление админу о новом подключении (ТЗ 6.5, 6.8:
    «администратор получает информационное уведомление», без одобрения).
    """
    from app.bot.runtime import get_bot
    from app.services import access
    from app.services.franchise_billing import TIER_LABELS

    owner_label = f"@{owner_user.username}" if owner_user.username else f"id {owner_user.id}"
    text = (
        "🏷 <b>Новый партнёр подключён</b>\n\n"
        f"Бот: @{bot_username}\n"
        f"Тариф: {TIER_LABELS.get(tier, tier)}\n"
        f"Владелец: {owner_label}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть в «Партнёры»", callback_data=f"adm:wlbl:card:{partner_id}", style=STYLE_PLAIN)],
    ])
    bot = get_bot()
    for admin_id in await access.admin_ids():
        try:
            await bot.send_message(admin_id, text, reply_markup=kb)
        except Exception:
            logger.exception("Could not notify admin id=%s about new partner id=%s", admin_id, partner_id)


async def _clone_franchise_panel(message: Message, user, *, edit: bool = False) -> None:
    from app.core.texts import BTN_HOME
    from app.services.franchise_billing import TIER_LABELS, TIER_PRICES
    from app.services import banners

    tier = current_partner_tier() or "basic"
    price = TIER_PRICES.get(tier, TIER_PRICES["basic"])
    owner = user and user.id == current_partner_owner_id()
    async with SessionLocal() as session:
        partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == current_partner_id()))
    billing_status = partner.franchise_billing_status if partner else "pending"
    status = {"active": "подключён", "grace": "ожидает оплаты — льготный период", "suspended": "приостановлен", "manual": "подключён, продление вручную", "pending": "ожидает оплаты"}.get(billing_status, billing_status)
    lines = ["<b>Франшиза</b>", "", f"Тариф: <b>{TIER_LABELS.get(tier, 'Базовый')}</b> — {price} ₽/мес", f"Статус: {status}"]
    if owner:
        has_payout = bool(partner and partner.yookassa_shop_id and partner.yookassa_secret_key)
        lines.append(
            "Приём оплаты от клиентов: ✅ подключён" if has_payout
            else "Приём оплаты от клиентов: ⚠️ не настроен — укажите «Платёжные реквизиты», иначе клиенты не смогут оплатить"
        )
    lines += ["", "Тариф оплачивается на месяц. Чтобы продлить, выберите тариф ниже."]
    rows = []
    if owner:
        for code in ("basic", "standard", "premium"):
            rows.append([InlineKeyboardButton(text=f"{TIER_LABELS[code]} · {TIER_PRICES[code]} ₽/мес", callback_data=f"franchise:plan:{code}", style=STYLE_MAIN if code == tier else STYLE_PLAIN)])
        rows.append([InlineKeyboardButton(text="💳 Платёжные реквизиты", callback_data="franchise:payout", style=STYLE_PLAIN)])
    rows.append([InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home", style=STYLE_PLAIN)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await banners.show_screen(message, banners.FRANCHISE, "\n".join(lines), kb, edit=edit, replace=edit)


@franchise_router.callback_query(F.data == "franchise:menu")
async def clone_franchise_menu(callback) -> None:
    if current_partner_id() and not _require_clone_owner(callback):
        await callback.answer("Раздел доступен только владельцу бота", show_alert=True)
        return
    await callback.answer()
    await _clone_franchise_panel(callback.message, callback.from_user, edit=True)


@franchise_router.callback_query(F.data.startswith("franchise:plan:"))
async def clone_franchise_plan(callback) -> None:
    from app.services.franchise_billing import start_plan_checkout
    owner_id = current_partner_owner_id()
    if not current_partner_id() or callback.from_user.id != owner_id:
        await callback.answer("Раздел доступен только владельцу бота", show_alert=True)
        return
    tier = callback.data.rsplit(":", 1)[-1]
    try:
        url = await start_plan_checkout(current_partner_id(), callback.from_user.id, tier)
    except (ValueError, PermissionError, RuntimeError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Ссылка на оплату готова")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=url, style=STYLE_MAIN)],
        [InlineKeyboardButton(text="← Назад", callback_data="franchise:menu", style=STYLE_PLAIN)],
    ])
    from app.services import banners
    await banners.show_screen(
        callback.message, banners.PLAIN,
        "Оплата тарифа — за один месяц. Нажмите кнопку ниже, чтобы перейти к оплате.",
        kb, edit=True, replace=True,
    )


def _cancel_kb(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=callback_data, style=STYLE_PLAIN)],
    ])


def _require_clone_owner(callback) -> bool:
    return bool(current_partner_id()) and callback.from_user.id == current_partner_owner_id()


async def _payout_menu_text_kb(partner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
    has_creds = bool(partner and partner.yookassa_shop_id and partner.yookassa_secret_key)
    if has_creds:
        masked = partner.yookassa_shop_id
        lines = [
            "<b>💳 Платёжные реквизиты</b>", "",
            f"Магазин ЮKassa: <code>{masked}</code>",
            "Оплаты клиентов клона идут на этот магазин.",
        ]
        rows = [
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="franchise:payout:start", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="franchise:payout:clear", style=STYLE_PLAIN)],
        ]
    else:
        lines = [
            "<b>💳 Платёжные реквизиты</b>", "",
            "Не указаны — оплаты клиентов клона пока некуда принимать.",
        ]
        rows = [
            [InlineKeyboardButton(text="✏️ Указать", callback_data="franchise:payout:start", style=STYLE_MAIN)],
        ]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="franchise:menu", style=STYLE_PLAIN)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@franchise_router.callback_query(F.data == "franchise:payout")
async def franchise_payout_menu(callback, state) -> None:
    if not _require_clone_owner(callback):
        await callback.answer("Раздел доступен только владельцу бота", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    text, kb = await _payout_menu_text_kb(current_partner_id())
    from app.services import banners
    await banners.show_screen(callback.message, banners.PLAIN, text, kb, edit=True, replace=True)


@franchise_router.callback_query(F.data == "franchise:payout:start")
async def franchise_payout_start(callback, state) -> None:
    if not _require_clone_owner(callback):
        await callback.answer("Раздел доступен только владельцу бота", show_alert=True)
        return
    from app.core.texts import FRANCHISE_PAYOUT_ASK_SHOP_ID
    from app.states.flows import FranchisePayout

    await state.clear()
    await state.update_data(franchise_payout_partner_id=current_partner_id())
    await state.set_state(FranchisePayout.waiting_shop_id)
    await callback.answer()
    prompt = await callback.message.answer(
        FRANCHISE_PAYOUT_ASK_SHOP_ID, reply_markup=_cancel_kb("franchise:payout:cancel"),
    )
    await state.update_data(franchise_payout_prompt_id=prompt.message_id)


@franchise_router.callback_query(F.data == "franchise:payout:cancel")
async def franchise_payout_cancel(callback, state) -> None:
    if not _require_clone_owner(callback):
        await callback.answer("Раздел доступен только владельцу бота", show_alert=True)
        return
    await state.clear()
    await callback.answer("Отменено")
    text, kb = await _payout_menu_text_kb(current_partner_id())
    from app.services import banners
    await banners.show_screen(callback.message, banners.PLAIN, text, kb, edit=True, replace=True)


async def _drop_prompt_markup(message: Message, state) -> None:
    prompt_id = (await state.get_data()).get("franchise_payout_prompt_id")
    if not prompt_id:
        return
    try:
        await message.bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=prompt_id, reply_markup=None)
    except Exception:
        pass


@franchise_router.callback_query(F.data == "franchise:payout:clear")
async def franchise_payout_clear(callback) -> None:
    if not _require_clone_owner(callback):
        await callback.answer("Раздел доступен только владельцу бота", show_alert=True)
        return
    async with SessionLocal() as session:
        partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == current_partner_id()))
        if partner:
            partner.yookassa_shop_id = None
            partner.yookassa_secret_key = None
            await session.commit()
    await callback.answer("Реквизиты удалены")
    text, kb = await _payout_menu_text_kb(current_partner_id())
    from app.services import banners
    await banners.show_screen(callback.message, banners.PLAIN, text, kb, edit=True, replace=True)


@franchise_router.message(FranchisePayout.waiting_shop_id)
async def franchise_payout_receive_shop_id(message: Message, state) -> None:
    if current_partner_id() != (await state.get_data()).get("franchise_payout_partner_id"):
        await state.clear()
        return
    shop_id = (message.text or "").strip()
    if shop_id.lower() in {"/cancel", "отмена", "назад"}:
        await state.clear()
        await message.answer("Отменено.")
        return
    if not shop_id.isdigit() or not (3 <= len(shop_id) <= 20):
        await message.answer(
            "shopId выглядит некорректно — это просто число из личного кабинета ЮKassa. Отправьте его ещё раз.",
            reply_markup=_cancel_kb("franchise:payout:cancel"),
        )
        return
    from app.core.texts import FRANCHISE_PAYOUT_ASK_SECRET_KEY
    from app.states.flows import FranchisePayout

    await _drop_prompt_markup(message, state)
    await state.update_data(franchise_payout_shop_id=shop_id)
    await state.set_state(FranchisePayout.waiting_secret_key)
    prompt = await message.answer(
        FRANCHISE_PAYOUT_ASK_SECRET_KEY, reply_markup=_cancel_kb("franchise:payout:cancel"),
    )
    await state.update_data(franchise_payout_prompt_id=prompt.message_id)


@franchise_router.message(FranchisePayout.waiting_secret_key)
async def franchise_payout_receive_secret_key(message: Message, state) -> None:
    data = await state.get_data()
    partner_id = data.get("franchise_payout_partner_id")
    if current_partner_id() != partner_id:
        await state.clear()
        return
    secret_key = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if secret_key.lower() in {"/cancel", "отмена", "назад"}:
        await state.clear()
        await message.answer("Отменено.")
        return
    if len(secret_key) < 20 or "_" not in secret_key:
        await message.answer(
            "Секретный ключ выглядит некорректно. Отправьте его ещё раз из личного кабинета ЮKassa.",
            reply_markup=_cancel_kb("franchise:payout:cancel"),
        )
        return
    shop_id = data.get("franchise_payout_shop_id")
    if not shop_id:
        await state.clear()
        await message.answer("Шаг с shopId сброшен. Откройте «Платёжные реквизиты» заново.")
        return

    # Не сохраняем вслепую: спрашиваем у ЮKassa, подходит ли пара shopId/ключ.
    # Опечатка вылезает сразу у владельца, а не у первого клиента на оплате.
    from app.config import get_settings
    from app.core.texts import (
        FRANCHISE_PAYOUT_MODE_LIVE,
        FRANCHISE_PAYOUT_MODE_TEST,
        FRANCHISE_PAYOUT_REJECTED,
        FRANCHISE_PAYOUT_UNVERIFIED,
        FRANCHISE_PAYOUT_WEBHOOK_HINT,
    )
    from app.services.yookassa import YooKassaService

    settings = get_settings()
    checker = YooKassaService(settings.model_copy(update={
        "yookassa_shop_id": shop_id, "yookassa_secret_key": secret_key,
    }))
    retry_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ввести заново", callback_data="franchise:payout:start", style=STYLE_MAIN)],
        [InlineKeyboardButton(text="← Назад", callback_data="franchise:payout", style=STYLE_PLAIN)],
    ])
    info: dict | None = None
    try:
        info = await checker.verify_account()
    except Exception as exc:
        reason = type(exc).__name__
        logger.warning("Payout credentials check failed partner_id=%s reason=%s", partner_id, reason)
        if reason == "UnauthorizedError":
            # Единственный явный отказ: ЮKassa не знает такой пары shopId/ключ.
            await _drop_prompt_markup(message, state)
            await state.clear()
            await message.answer(FRANCHISE_PAYOUT_REJECTED, reply_markup=retry_kb)
            return
        if reason not in {"ForbiddenError", "NotFoundError", "BadRequestError"}:
            # Сеть/сбой ЮKassa — не сохраняем вслепую, пусть повторит.
            await _drop_prompt_markup(message, state)
            await state.clear()
            await message.answer(FRANCHISE_PAYOUT_UNVERIFIED, reply_markup=retry_kb)
            return
        # ЮKassa отвечает, но не отдаёт сведения о магазине по этому ключу —
        # валидную пару из-за этого не отвергаем.
        info = None
    is_test = False
    receipts_on = False
    if info is not None:
        account_id = str(info.get("account_id") or "")
        if account_id and account_id != shop_id:
            await _drop_prompt_markup(message, state)
            await state.clear()
            await message.answer(FRANCHISE_PAYOUT_REJECTED, reply_markup=retry_kb)
            return
        is_test = bool(info.get("test"))
        fiscal = info.get("fiscalization")
        receipts_on = bool(info.get("fiscalization_enabled")) or (
            isinstance(fiscal, dict) and bool(fiscal.get("enabled"))
        )

    async with SessionLocal() as session:
        partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
        if not partner:
            await state.clear()
            await message.answer("Подключение не найдено.")
            return
        partner.yookassa_shop_id = shop_id
        partner.yookassa_secret_key = secret_key
        await session.commit()
    await _drop_prompt_markup(message, state)
    await state.clear()

    from app.core.texts import (
        FRANCHISE_PAYOUT_RECEIPT_WARN,
        FRANCHISE_PAYOUT_SAVED,
        FRANCHISE_PAYOUT_UNCHECKED,
    )
    if info is None:
        mode = "режим не определён"
    else:
        mode = FRANCHISE_PAYOUT_MODE_TEST if is_test else FRANCHISE_PAYOUT_MODE_LIVE
    text = FRANCHISE_PAYOUT_SAVED.format(shop_id=shop_id, mode=mode)
    if info is None:
        text += FRANCHISE_PAYOUT_UNCHECKED
    if receipts_on:
        text += FRANCHISE_PAYOUT_RECEIPT_WARN
    base = settings.public_url.rstrip("/")
    # ЮKassa принимает уведомления только по HTTPS (порт 443 или 8443).
    if base.startswith("https://"):
        text += FRANCHISE_PAYOUT_WEBHOOK_HINT.format(url=f"{base}/payments/yookassa/webhook")
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Платёжные реквизиты", callback_data="franchise:payout", style=STYLE_PLAIN)],
    ]))


async def _owned_franchise_panel(message: Message, telegram_id: int, partners: list[WhitelabelPartner] | None = None, *, edit: bool = False) -> None:
    from app.core.texts import FRANCHISE_TEXT
    from app.services import banners

    from app.services.franchise_billing import TIER_LABELS

    partners = _without_unpaid_attempts(
        partners if partners is not None else await _partners_for_user(telegram_id),
    )
    if not partners:
        await _franchise_start_screen(message, edit=edit)
        return
    lines = [FRANCHISE_TEXT, "", "<b>Мои боты-франшизы</b>", ""]
    rows = []
    status_labels = {
        "active": "работает",
        "manual": "подключён",
        "grace": "льготный период оплаты",
        "suspended": "приостановлен",
        "pending": "ожидает оплаты",
        "awaiting_token": "ожидает токена бота",
    }
    for partner in partners:
        status = status_labels.get(partner.franchise_billing_status, partner.franchise_billing_status)
        label = f"@{partner.bot_username}" if partner.bot_username else TIER_LABELS.get(partner.franchise_tier, "Новый бот")
        lines.append(f"{label} — {status}")
        if partner.franchise_billing_status == "awaiting_token":
            rows.append([InlineKeyboardButton(
                text=f"🤖 Указать токен: {label}",
                callback_data=f"franchise:token:{partner.id}",
                style=STYLE_MAIN,
            )])
        elif partner.active:
            rows.append([InlineKeyboardButton(
                text=f"⚙️ {label}",
                callback_data=f"franchise:open:{partner.id}",
                style=STYLE_PLAIN,
            )])
        else:
            # Бот остановлен за неуплату (льготный период, приостановка) —
            # единственный случай, когда «Продолжить» нужно: неоплаченные
            # попытки подключения сюда не попадают (`_without_unpaid_attempts`).
            rows.append([InlineKeyboardButton(
                text=f"💳 Продолжить {label}",
                callback_data=f"franchise:resume:{partner.id}",
                style=STYLE_MAIN,
            )])
    rows.append([InlineKeyboardButton(
        text="➕ Подключить ещё бота",
        callback_data="franchise:join",
        style=STYLE_MAIN,
    )])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home", style=STYLE_PLAIN)])
    await banners.show_screen(
        message,
        banners.FRANCHISE,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
        replace=edit,
    )


@franchise_router.callback_query(F.data == "franchise:owned")
async def franchise_owned(callback) -> None:
    if current_partner_id():
        await callback.answer("Недоступно в боте-клоне", show_alert=True)
        return
    await callback.answer()
    await _owned_franchise_panel(callback.message, callback.from_user.id, edit=True)


@franchise_router.callback_query(F.data.startswith("franchise:open:"))
async def franchise_open_owned(callback) -> None:
    if current_partner_id():
        await callback.answer("Недоступно в боте-клоне", show_alert=True)
        return
    try:
        partner_id = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Бот не найден", show_alert=True)
        return
    partner = await _partner_for_user(callback.from_user.id, partner_id, active=True)
    if not partner:
        await callback.answer("Бот не найден", show_alert=True)
        return
    await callback.answer()
    await _franchise_panel(callback.message, partner, edit=True)


async def _franchise_panel(message: Message, partner: WhitelabelPartner, *, edit: bool = False) -> None:
    from app.core.texts import BTN_HOME
    from app.services import banners

    token_status = "✅ токен подключён, бот в polling" if partner.bot_token else "⏳ добавьте bot token в админке"
    brand = partner.brand_title or partner.bot_username
    lines = [
        f"<b>🏷 Франшиза — {brand}</b>",
        "",
        f"Бот: <b>@{partner.bot_username}</b>",
        f"Статус: {token_status}",
        "",
        "<b>API-ключ</b>",
        f"<code>{partner.api_key}</code>",
        "",
        "<b>ML классификация</b>",
        "<code>POST /api/ml/classify</code>",
        "Header: <code>X-API-Key</code>",
        "",
        "<b>Публикация (webhook партнёра)</b>",
        f"<code>{build_wl_publish_url()}</code>",
        "Header: <code>X-API-Key</code>",
        'Body: <code>{"text": "...", "contact": "@user", "photo_url": "https://..."}</code>',
    ]
    if partner.note:
        lines.append(f"\n<i>{partner.note}</i>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Мои боты-франшизы", callback_data="franchise:owned", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home", style=STYLE_PLAIN)],
        ],
    )
    await banners.show_screen(message, banners.PLAIN, "\n".join(lines), kb, edit=edit, replace=edit)
