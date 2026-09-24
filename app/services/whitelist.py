from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    Chat,
    City,
    Subscription,
    SubscriptionChat,
    SubscriptionStatus,
    User,
    WhitelistEntry,
)
from app.services.fraud import text_fingerprint
from app.services.pricing import PLAN_STANDARD, POSTS_VOLUME_UNLIMITED

logger = logging.getLogger("whitelist")

WHITELIST_CATEGORY = "WHITELIST"
WHITELIST_APPROVED_TEXT = "Доступ работодателя (whitelist)"


def parse_city_keys(raw: str | None) -> set[str]:
    try:
        keys = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return set()
    if not isinstance(keys, list):
        return set()
    return {str(k).strip().lower() for k in keys if str(k).strip()}


def parse_chat_ids(raw: str | None) -> set[int]:
    try:
        ids = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return set()
    if not isinstance(ids, list):
        return set()
    out: set[int] = set()
    for item in ids:
        try:
            out.add(int(item))
        except (TypeError, ValueError):
            continue
    return out


def city_allowed(entry: WhitelistEntry, city_key: str) -> bool:
    keys = parse_city_keys(entry.city_keys)
    if not keys or "*" in keys:
        return True
    return city_key.lower() in keys


def chats_allowed(entry: WhitelistEntry, selected_chat_ids: list[int]) -> bool:
    allowed = parse_chat_ids(entry.chat_ids)
    if not allowed:
        return True
    if not selected_chat_ids:
        return False
    return set(selected_chat_ids).issubset(allowed)


def has_city_restrictions(entry: WhitelistEntry | None) -> bool:
    if not entry:
        return False
    keys = parse_city_keys(entry.city_keys)
    return bool(keys) and "*" not in keys


def has_chat_restrictions(entry: WhitelistEntry | None) -> bool:
    if not entry:
        return False
    return bool(parse_chat_ids(entry.chat_ids))


def _norm_username(username: str | None) -> str | None:
    uname = (username or "").strip().lstrip("@").lower()
    return uname or None


def entry_is_active(entry: WhitelistEntry | None, now: datetime | None = None) -> bool:
    """Запись действует, если срок не задан или ещё не вышел (ТЗ этапа 2, 6.3).

    Пустой `expires_at` — «навсегда»: так заведены все записи, созданные до
    появления срока.
    """
    if entry is None:
        return False
    if entry.expires_at is None:
        return True
    return entry.expires_at > (now or datetime.now(UTC))


async def get_whitelist_entry(
    session: AsyncSession,
    telegram_id: int,
    *,
    include_expired: bool = False,
) -> WhitelistEntry | None:
    if not telegram_id:
        return None
    entry = await session.scalar(
        select(WhitelistEntry).where(WhitelistEntry.telegram_id == telegram_id),
    )
    if entry is not None and not include_expired and not entry_is_active(entry):
        return None
    return entry


async def get_whitelist_entry_by_username(
    session: AsyncSession,
    username: str | None,
    *,
    include_expired: bool = False,
) -> WhitelistEntry | None:
    uname = _norm_username(username)
    if not uname:
        return None
    entry = await session.scalar(
        select(WhitelistEntry).where(func.lower(WhitelistEntry.username) == uname),
    )
    if entry is not None and not include_expired and not entry_is_active(entry):
        return None
    return entry


async def get_whitelist_entry_for_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    *,
    include_expired: bool = False,
) -> WhitelistEntry | None:
    """Действующая запись белого списка. Истёкшая прав не даёт."""
    entry = await get_whitelist_entry(session, telegram_id, include_expired=include_expired)
    if entry:
        return entry
    return await get_whitelist_entry_by_username(session, username, include_expired=include_expired)


async def bind_whitelist_entry_to_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
) -> WhitelistEntry | None:
    """Привязать запись whitelist к telegram_id (pending @username → id)."""
    if not telegram_id:
        return None

    entry = await get_whitelist_entry(session, telegram_id)
    if entry:
        uname = _norm_username(username)
        if uname and not entry.username:
            entry.username = uname
            await session.commit()
        return entry

    uname = _norm_username(username)
    if not uname:
        # username скрыт в Telegram — попробуем взять из users
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        uname = _norm_username(user.username if user else None)
    if not uname:
        return None

    entry = await get_whitelist_entry_by_username(session, uname)
    if not entry:
        return None

    if entry.telegram_id and entry.telegram_id != telegram_id:
        logger.warning(
            "Whitelist entry #%s username=@%s bound to tg=%s, rebinding to tg=%s",
            entry.id,
            uname,
            entry.telegram_id,
            telegram_id,
        )

    entry.telegram_id = telegram_id
    entry.username = uname
    await session.commit()
    return entry


async def try_bind_entry_from_existing_user(
    session: AsyncSession,
    entry: WhitelistEntry,
) -> WhitelistEntry:
    """Если пользователь уже писал боту — привязать telegram_id по username."""
    if entry.telegram_id or not entry.username:
        return entry
    uname = _norm_username(entry.username)
    if not uname:
        return entry
    user = await session.scalar(select(User).where(func.lower(User.username) == uname))
    if not user:
        return entry
    # Не перезаписываем, если этот telegram_id уже занят другой записью
    other = await get_whitelist_entry(session, user.telegram_id)
    if other and other.id != entry.id:
        logger.warning(
            "Cannot bind whitelist #%s to tg=%s: already used by entry #%s",
            entry.id,
            user.telegram_id,
            other.id,
        )
        return entry
    entry.telegram_id = user.telegram_id
    entry.username = uname
    await session.commit()
    logger.info("Bound whitelist #%s @%s -> tg=%s from users table", entry.id, uname, user.telegram_id)
    return entry


async def activate_pending_whitelist_entry(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
) -> WhitelistEntry | None:
    """Привязать pending-запись (@username без ID) к пользователю после /start."""
    entry = await bind_whitelist_entry_to_user(session, telegram_id=telegram_id, username=username)
    if not entry:
        return None
    await _provision_and_notify(session, entry)
    return entry


async def ensure_whitelist_subscription(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
) -> int | None:
    """Активировать whitelist-подписку, если пользователь в белом списке."""
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    effective_username = _norm_username(username) or _norm_username(user.username if user else None)

    entry = await bind_whitelist_entry_to_user(
        session,
        telegram_id=telegram_id,
        username=effective_username,
    )
    if not entry:
        return None

    if not entry.telegram_id:
        entry = await try_bind_entry_from_existing_user(session, entry)
    if not entry.telegram_id:
        return None

    ok, reason, sub_id = await provision_whitelist_subscription(session, entry)
    if ok and sub_id:
        return sub_id

    logger.info(
        "Whitelist subscription not ensured for tg=%s entry=%s reason=%s",
        telegram_id,
        entry.id,
        reason,
    )
    return None


async def resolve_chats_for_entry(session: AsyncSession, entry: WhitelistEntry) -> list[Chat]:
    allowed_chat_ids = parse_chat_ids(entry.chat_ids)
    if allowed_chat_ids:
        rows = (
            await session.scalars(
                select(Chat).where(Chat.id.in_(allowed_chat_ids), Chat.active.is_(True)).order_by(Chat.sort_order),
            )
        ).all()
        return list(rows)

    city_keys = parse_city_keys(entry.city_keys)
    query = select(Chat).where(Chat.active.is_(True)).order_by(Chat.sort_order)
    if city_keys and "*" not in city_keys:
        city_ids = list(
            await session.scalars(
                select(City.id).where(City.key.in_(tuple(city_keys)), City.active.is_(True)),
            ),
        )
        if not city_ids:
            return []
        query = query.where(Chat.city_id.in_(city_ids))
    return list((await session.scalars(query)).all())


async def _sync_subscription_chats(session: AsyncSession, subscription_id: int, chat_ids: list[int]) -> None:
    from sqlalchemy import delete

    await session.execute(
        delete(SubscriptionChat).where(SubscriptionChat.subscription_id == subscription_id),
    )
    for chat_id in chat_ids:
        session.add(SubscriptionChat(subscription_id=subscription_id, chat_id=chat_id))


async def provision_whitelist_subscription(
    session: AsyncSession,
    entry: WhitelistEntry,
) -> tuple[bool, str, int | None]:
    """Создать или синхронизировать активную WHITELIST-подписку."""
    if not entry.telegram_id:
        return False, "pending_user", None

    user = await session.scalar(select(User).where(User.telegram_id == entry.telegram_id))
    if not user:
        return False, "user_not_started", None

    chats = await resolve_chats_for_entry(session, entry)
    if not chats:
        return False, "no_chats", None

    chat_ids = [chat.id for chat in chats]
    now = datetime.now(UTC)
    contact = f"@{entry.username}" if entry.username else "—"

    # Только WHITELIST-подписка — не трогаем платные active
    active_wl = await session.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.category_code == WHITELIST_CATEGORY,
            Subscription.status == SubscriptionStatus.active,
            Subscription.expires_at > now,
        )
        .order_by(Subscription.id.desc()),
    )
    if active_wl:
        await _sync_subscription_chats(session, active_wl.id, chat_ids)
        # Продлеваем только если осталось меньше 7 дней
        if active_wl.expires_at and active_wl.expires_at < now + timedelta(days=7):
            active_wl.expires_at = now + timedelta(days=30)
        active_wl.total_price = 0
        active_wl.posts_volume = POSTS_VOLUME_UNLIMITED
        await session.commit()
        return True, "synced", active_wl.id

    pending_wl = await session.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.category_code == WHITELIST_CATEGORY,
            Subscription.status == SubscriptionStatus.pending_payment,
        )
        .order_by(Subscription.id.desc()),
    )
    if pending_wl:
        await _sync_subscription_chats(session, pending_wl.id, chat_ids)
        pending_wl.posts_volume = POSTS_VOLUME_UNLIMITED
        pending_wl.total_price = 0
        await session.commit()
        from app.services.payments import activate_subscription_whitelist

        await activate_subscription_whitelist(session, user.id, pending_wl.id)
        return True, "activated_pending", pending_wl.id

    sub = Subscription(
        user_id=user.id,
        category_code=WHITELIST_CATEGORY,
        approved_text=WHITELIST_APPROVED_TEXT,
        approved_text_hash=text_fingerprint(WHITELIST_APPROVED_TEXT),
        contact=contact,
        price_per_chat=0,
        total_price=0,
        posts_volume=POSTS_VOLUME_UNLIMITED,
        plan_type=PLAN_STANDARD,
        status=SubscriptionStatus.pending_payment,
    )
    session.add(sub)
    await session.flush()
    for chat_id in chat_ids:
        session.add(SubscriptionChat(subscription_id=sub.id, chat_id=chat_id))
    await session.commit()

    from app.services.payments import activate_subscription_whitelist

    await activate_subscription_whitelist(session, user.id, sub.id)
    return True, "created", sub.id


async def _provision_and_notify(session: AsyncSession, entry: WhitelistEntry) -> None:
    ok, reason, sub_id = await provision_whitelist_subscription(session, entry)
    if not ok or not sub_id:
        logger.info("Whitelist provision skipped entry=%s reason=%s", entry.id, reason)
        return
    if reason == "synced":
        # Уже был доступ — повторно не спамим
        return
    user = await session.scalar(select(User).where(User.telegram_id == entry.telegram_id))
    if not user:
        return
    from app.services.notifications import notify_payment_success

    await notify_payment_success(user.id, "subscription_whitelist", sub_id)


async def is_user_whitelisted_for_city(
    session: AsyncSession,
    telegram_id: int,
    city_key: str,
    selected_chat_ids: list[int] | None = None,
    username: str | None = None,
) -> tuple[bool, WhitelistEntry | None]:
    entry = await get_whitelist_entry_for_user(session, telegram_id, username)
    if not entry:
        return False, None
    if not city_allowed(entry, city_key):
        return False, entry
    if selected_chat_ids is not None and not chats_allowed(entry, selected_chat_ids):
        return False, entry
    return True, entry


def filter_cities_for_entry(entry: WhitelistEntry | None, cities: list) -> list:
    if not entry or not has_city_restrictions(entry):
        return cities
    allowed = parse_city_keys(entry.city_keys)
    return [city for city in cities if city.key.lower() in allowed]


def filter_chats_for_entry(entry: WhitelistEntry | None, chats: list) -> list:
    if not entry or not has_chat_restrictions(entry):
        return chats
    allowed = parse_chat_ids(entry.chat_ids)
    return [chat for chat in chats if chat.id in allowed]


async def whitelist_access_valid(session: AsyncSession, sub: Subscription) -> bool:
    """Бесплатная подписка белого списка действует, только пока есть действующая запись.

    Раньше подписка жила своей жизнью: удалили запись или у неё вышел срок, а
    человек продолжал публиковать бесплатно (заказчик 24.09.2026: «выкидываешь
    из белого списка — публикации закрываются, только через оплату»).
    Платные подписки этой проверкой не затрагиваются.
    """
    if sub.category_code != WHITELIST_CATEGORY:
        return True
    user = await session.scalar(select(User).where(User.id == sub.user_id))
    if user is None:
        return False
    entry = await get_whitelist_entry_for_user(session, user.telegram_id, user.username)
    return entry is not None


async def revoke_whitelist_access(
    session: AsyncSession, telegram_id: int | None, username: str | None = None,
) -> int:
    """Закрыть бесплатный доступ при удалении записи белого списка.

    Подписки категории WHITELIST становятся истёкшими, ждущие в очереди
    публикации этих подписок отменяются. Платные подписки не трогаем.
    Коммитит вызывающий код. Возвращает число закрытых подписок.
    """
    uname = _norm_username(username)
    conds = []
    if telegram_id:
        conds.append(User.telegram_id == telegram_id)
    if uname:
        conds.append(func.lower(User.username) == uname)
    if not conds:
        return 0
    from app.models.entities import PublishJob

    now = datetime.now(UTC)
    user_ids = [u.id for u in (await session.scalars(select(User).where(or_(*conds)))).all()]
    if not user_ids:
        return 0
    subs = (
        await session.scalars(
            select(Subscription).where(
                Subscription.user_id.in_(user_ids),
                Subscription.category_code == WHITELIST_CATEGORY,
                Subscription.status == SubscriptionStatus.active,
            ),
        )
    ).all()
    for sub in subs:
        sub.status = SubscriptionStatus.expired
        sub.expires_at = now
        await session.execute(
            update(PublishJob)
            .where(PublishJob.subscription_id == sub.id, PublishJob.status == "queued")
            .values(status="cancelled", error="whitelist_revoked"),
        )
    return len(subs)


async def validate_scope(
    session: AsyncSession,
    telegram_id: int,
    city_key: str,
    selected_chat_ids: list[int],
    username: str | None = None,
) -> tuple[bool, str]:
    entry = await get_whitelist_entry_for_user(session, telegram_id, username)
    if not entry:
        return True, ""
    if has_city_restrictions(entry) and not city_allowed(entry, city_key):
        return False, "Этот город недоступен по вашему whitelist."
    if has_chat_restrictions(entry) and not chats_allowed(entry, selected_chat_ids):
        return False, "Доступны только чаты из вашего whitelist."
    return True, ""
