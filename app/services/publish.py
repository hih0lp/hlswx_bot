from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.entities import (
    Chat,
    PackageOrder,
    Publication,
    PublishJob,
    Subscription,
    SubscriptionChat,
    User,
)
from app.services import settings_store
from app.services.hammer_relay import RelayResult
from app.services.publish_errors import STATUS_UNCONFIRMED
from app.services.publish_log import log_event
from app.services.queue_slots import (
    PRIORITY_ONESHOT,
    PRIORITY_SUBSCRIBER,
    PRIORITY_WHITELIST,
    SLOT_PAUSE,
    SLOT_SILENCE,
    defer_chat_jobs,
    mark_published,
    pick_next_job,
    set_silence,
)

logger = logging.getLogger("publish")

MAX_ATTEMPTS = 5
# Первый повтор — почти сразу. Типичный сбой релея это одиночный таймаут его
# запроса к Telegram, который проходит за секунды: пауза в 2 минуты растягивала
# один пост по группам на 3 минуты и показывала пользователю «опубликовано
# частично» на ровном месте. Второй повтор ждёт дольше — там сбой уже не разовый.
# Третий и четвёртый повторы — на случай затяжного сбоя сети до Telegram:
# такие сбои бывают по несколько минут подряд.
RETRY_DELAYS_SEC = (20, 120, 300, 600)
RETRY_DELAY_MINUTES = 2  # legacy-имя, оставлено для совместимости

# Совместимость со старыми именами приоритетов
PRIORITY_PACKAGE = PRIORITY_ONESHOT
PRIORITY_SUBSCRIPTION = PRIORITY_SUBSCRIBER

SOURCE_SUBSCRIPTION = "subscription"
SOURCE_WHITELIST = "whitelist"
SOURCE_PACKAGE = "package"
SOURCE_ONESHOT = "oneshot"

# Ошибки релея, при которых имеет смысл попробовать опубликовать своим ботом
_RELAY_INFRA_ERRORS = (
    "relay_disabled",
    "relay_unreachable",
    "relay_secret_not_configured",
    "http_5",
)


def render_post(text: str, contact: str) -> str:
    contact = (contact or "").strip()
    body = (text or "").strip()
    if contact:
        if contact.startswith("@"):
            body += f"\n\n👤 Контакт: {contact}"
        else:
            body += f"\n\n📱 Контакт: {contact}"
    return body


def priority_for_subscription(subscription: Subscription) -> tuple[int, str]:
    """Подписчик — приоритет 2, белый список — 3 (ТЗ 6.3)."""
    from app.services.whitelist import WHITELIST_CATEGORY

    if subscription.category_code == WHITELIST_CATEGORY:
        return PRIORITY_WHITELIST, SOURCE_WHITELIST
    return PRIORITY_SUBSCRIBER, SOURCE_SUBSCRIPTION


async def _create_publication(
    session: AsyncSession,
    *,
    user_id: int,
    source: str,
    priority: int,
    text: str,
    contact: str,
    photo_url: str | None = None,
    subscription_id: int | None = None,
    package_order_id: int | None = None,
) -> Publication:
    publication = Publication(
        user_id=user_id,
        source=source,
        priority=priority,
        subscription_id=subscription_id,
        package_order_id=package_order_id,
        text=text,
        contact=contact or "",
        photo_url=photo_url,
        status="queued",
    )
    session.add(publication)
    await session.flush()
    return publication


async def enqueue_package_order(order_id: int) -> int:
    from app.core.texts import PACKAGE_TEXT_PENDING
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        order = await session.scalar(select(PackageOrder).where(PackageOrder.id == order_id))
        if not order or not order.text or order.text == PACKAGE_TEXT_PENDING:
            return 0
        chats = (
            await session.scalars(
                select(Chat).where(Chat.city_id == order.city_id, Chat.active.is_(True)).order_by(Chat.sort_order),
            )
        ).all()
        if not chats:
            return 0

        now = datetime.now(UTC)
        publication = await _create_publication(
            session,
            user_id=order.user_id,
            source=SOURCE_PACKAGE,
            priority=PRIORITY_ONESHOT,
            text=order.text,
            contact=order.contact,
            package_order_id=order.id,
        )
        for chat in chats:
            session.add(
                PublishJob(
                    publication_id=publication.id,
                    package_order_id=order.id,
                    author_user_id=order.user_id,
                    chat_id=chat.id,
                    text=order.text,
                    contact=order.contact,
                    priority=PRIORITY_ONESHOT,
                    status="queued",
                    scheduled_at=now,
                    expires_at=now + timedelta(minutes=await settings_store.queue_job_ttl_minutes()),
                ),
            )
        await log_event(
            session,
            "queued",
            publication_id=publication.id,
            detail=f"package order={order.id} chats={len(chats)} priority=1",
        )
        await session.commit()
        return len(chats)


async def enqueue_subscription_post(
    session: AsyncSession,
    subscription_id: int,
    text: str,
    contact: str,
    *,
    photo_url: str | None = None,
    city_key: str | None = None,
) -> int:
    from app.models.entities import City
    from app.services.whitelist import WHITELIST_CATEGORY, validate_scope

    sub = await session.scalar(select(Subscription).where(Subscription.id == subscription_id))
    if not sub:
        return 0
    user = await session.scalar(select(User).where(User.id == sub.user_id))
    links = (
        await session.scalars(
            select(SubscriptionChat).where(SubscriptionChat.subscription_id == subscription_id),
        )
    ).all()
    # Границы записи белого списка проверяем только у бесплатного доступа.
    # У купленной подписки область задана оплатой: владелец записи на «msk»
    # вправе оплатить Казань, и его пост туда должен уходить (правка 17.09.2026).
    if sub.category_code == WHITELIST_CATEGORY:
        from app.services.whitelist import whitelist_access_valid

        if not await whitelist_access_valid(session, sub):
            return 0
    if user and links and sub.category_code == WHITELIST_CATEGORY:
        chat_ids = [link.chat_id for link in links]
        scope_city_key = (city_key or "").strip().lower()
        if not scope_city_key:
            first_chat = await session.scalar(select(Chat).where(Chat.id == links[0].chat_id))
            if first_chat:
                city = await session.scalar(select(City).where(City.id == first_chat.city_id))
                scope_city_key = (city.key if city else "").lower()
        ok, _ = await validate_scope(
            session, user.telegram_id, scope_city_key, chat_ids, username=user.username,
        )
        if not ok:
            return 0

    priority, source = priority_for_subscription(sub)
    now = datetime.now(UTC)
    filter_key = (city_key or "").strip().lower()

    target_chat_ids: list[int] = []
    for link in links:
        chat = await session.scalar(select(Chat).where(Chat.id == link.chat_id))
        if not chat or not chat.active:
            continue
        if filter_key:
            chat_city = await session.scalar(select(City).where(City.id == chat.city_id))
            if not chat_city or chat_city.key.lower() != filter_key:
                continue
        target_chat_ids.append(link.chat_id)

    if not target_chat_ids:
        return 0

    publication = await _create_publication(
        session,
        user_id=sub.user_id,
        source=source,
        priority=priority,
        text=text,
        contact=contact,
        photo_url=photo_url,
        subscription_id=subscription_id,
    )
    for chat_id in target_chat_ids:
        # Все задачи одного поста встают в очередь одновременно: пост одного
        # пользователя уходит во все его группы без внутренней задержки (ТЗ 6.4).
        session.add(
            PublishJob(
                publication_id=publication.id,
                subscription_id=subscription_id,
                author_user_id=sub.user_id,
                chat_id=chat_id,
                text=text,
                contact=contact,
                photo_url=photo_url,
                priority=priority,
                status="queued",
                scheduled_at=now,
                expires_at=now + timedelta(minutes=await settings_store.queue_job_ttl_minutes()),
            ),
        )

    from app.services.volume_limits import log_publication

    await log_publication(session, subscription_id)
    await log_event(
        session,
        "queued",
        publication_id=publication.id,
        detail=f"subscription={subscription_id} chats={len(target_chat_ids)} priority={priority}",
    )
    await session.commit()
    return len(target_chat_ids)


def _relay_infra_failure(error: str) -> bool:
    """Релей не взял запрос — можно досылать своим ботом.

    Оборванный ответ сюда не попадает: релей мог успеть опубликовать, и
    досылка положила бы в группу второй такой же пост.
    """
    from app.services.publish_errors import is_uncertain

    low = (error or "").lower()
    if is_uncertain(low):
        return False
    return any(marker in low for marker in _RELAY_INFRA_ERRORS)


# Ответы Telegram на попытку писать в чат, где бота нет. «chat not found» сюда
# же: так Telegram отвечает и на живую группу, если бот в ней не состоял.
_BOT_MISSING_MARKERS = (
    "chat not found",
    "bot is not a member",
    "bot was kicked",
    "bot was blocked",
    "peer_id_invalid",
)


# Отказы, при которых запроса к релею не было вовсе: он выключен или не настроен.
_RELAY_NOT_TRIED = ("relay_disabled", "relay_secret_not_configured")


def _relay_was_tried(error: str) -> bool:
    """Релей действительно вызывали, и его ошибка что-то объясняет."""
    low = (error or "").lower()
    return not any(marker in low for marker in _RELAY_NOT_TRIED)


def _bot_missing_in_chat(error: str) -> bool:
    """Отказ из-за того, что отправляющего бота нет в группе."""
    low = (error or "").lower()
    return any(marker in low for marker in _BOT_MISSING_MARKERS)


def _photo_is_ours(photo_url: str | None) -> bool:
    """Картинка задана `file_id` нашего бота, а не ссылкой.

    Telegram выдаёт `file_id` каждому боту свой, и чужой бот его не откроет.
    Объявления с фото от пользователя (правка 17.09.2026) публикуем своим ботом
    напрямую, минуя релей Hammer/W; ссылки (их присылает B2B API) релей тянет
    сам и работает как раньше.
    """
    value = (photo_url or "").strip().lower()
    return bool(value) and not value.startswith(("http://", "https://"))


async def _deliver(
    bot: Bot,
    job: PublishJob,
    chat: Chat,
    *,
    city_key: str,
    pin: bool,
    premium: bool,
) -> tuple[RelayResult, str]:
    """Публикация: сначала релей Hammer/W, при отказе инфраструктуры — свой бот."""
    from app.services.direct_publish import (
        DELIVERY_DIRECT,
        DELIVERY_RELAY,
        direct_publish,
    )
    from app.services.hammer_relay import relay_publish

    settings = get_settings()
    from app.services.tenant import current_partner_id

    if current_partner_id():
        # Franchise groups must receive posts from the franchise bot itself.
        result = await direct_publish(
            bot,
            text=job.text,
            contact=job.contact,
            telegram_chat_id=int(chat.telegram_chat_id),
            photo_url=job.photo_url,
            pin=pin,
        )
        return result, DELIVERY_DIRECT

    if _photo_is_ours(job.photo_url):
        direct = await direct_publish(
            bot,
            text=job.text,
            contact=job.contact,
            telegram_chat_id=int(chat.telegram_chat_id),
            photo_url=job.photo_url,
            pin=pin,
        )
        if direct.ok or not _bot_missing_in_chat(direct.error):
            return direct, DELIVERY_DIRECT
        # Своего бота в группе нет — эта ветка молча роняла все посты с
        # пользовательским фото. Текст важнее картинки: досылаем релеем без
        # неё, потому что чужой бот наш file_id всё равно не откроет.
        logger.warning(
            "Direct publish rejected (%s), retrying via relay without photo chat=%s",
            direct.error,
            chat.id,
        )
        relayed = await relay_publish(
            text=job.text,
            contact=job.contact,
            telegram_chat_id=int(chat.telegram_chat_id),
            telegram_username=chat.telegram_username,
            network=chat.network or "hammer",
            city_key=city_key,
            pin=pin,
            photo_url=None,
            premium=premium,
            apply_chat_lock=premium,
        )
        if relayed.ok:
            return relayed, DELIVERY_RELAY
        return direct, DELIVERY_DIRECT

    result = await relay_publish(
        text=job.text,
        contact=job.contact,
        telegram_chat_id=int(chat.telegram_chat_id),
        telegram_username=chat.telegram_username,
        network=chat.network or "hammer",
        city_key=city_key,
        pin=pin,
        photo_url=job.photo_url,
        premium=premium,
        apply_chat_lock=premium,
    )
    if result.ok:
        return result, DELIVERY_RELAY

    if settings.direct_publish_fallback and _relay_infra_failure(result.error):
        logger.info("Relay unavailable (%s), publishing directly chat=%s", result.error, chat.id)
        direct = await direct_publish(
            bot,
            text=job.text,
            contact=job.contact,
            telegram_chat_id=int(chat.telegram_chat_id),
            photo_url=job.photo_url,
            pin=pin,
        )
        if direct.ok or not _bot_missing_in_chat(direct.error):
            return direct, DELIVERY_DIRECT
        # Наш бот в группе не состоит — это нормально, публикует релей. Показывать
        # админу «бот не добавлен в группу» вместо настоящей причины отказа релея
        # значит отправить его искать несуществующую поломку. Но когда релей
        # выключен и досылка была единственным путём, отказ досылки и есть причина.
        if _relay_was_tried(result.error):
            return result, DELIVERY_RELAY
        return direct, DELIVERY_DIRECT

    return result, DELIVERY_RELAY


async def _handle_failure(
    session: AsyncSession,
    job: PublishJob,
    error: str,
    now: datetime,
) -> bool:
    """Ретраи по ТЗ 6.7. True — задача закрыта, нужно сообщить администратору."""
    from app.services.publish_errors import human_reason, is_permanent, is_uncertain

    job.attempts = (job.attempts or 0) + 1

    if is_uncertain(error):
        # Ответ оборвался — пост, скорее всего, в группе. Повтор положил бы
        # туда дубль, поэтому задача закрывается отдельным статусом: для
        # подписчика это публикация, для администратора — повод заглянуть.
        job.status = STATUS_UNCONFIRMED
        job.published_at = now
        job.error = error[:500]
        await log_event(
            session,
            "unconfirmed",
            publication_id=job.publication_id,
            job_id=job.id,
            chat_id=job.chat_id,
            detail=f"{human_reason(error)}: {error[:200]}",
        )
        return True

    permanent = is_permanent(error)

    if not permanent and job.attempts < MAX_ATTEMPTS:
        job.status = "queued"
        delay_sec = RETRY_DELAYS_SEC[min(job.attempts, len(RETRY_DELAYS_SEC)) - 1]
        job.scheduled_at = now + timedelta(seconds=delay_sec)
        job.error = f"retry {job.attempts}: {error[:200]}"
        await log_event(
            session,
            "retry",
            publication_id=job.publication_id,
            job_id=job.id,
            chat_id=job.chat_id,
            detail=f"attempt={job.attempts} {error[:200]}",
        )
        return False

    job.status = "failed"
    job.failed_at = now
    job.error = error[:500]
    await log_event(
        session,
        "failed",
        publication_id=job.publication_id,
        job_id=job.id,
        chat_id=job.chat_id,
        detail=f"{'permanent' if permanent else 'attempts_exhausted'}: {human_reason(error)}",
    )
    return True


async def _publish_one(
    bot: Bot,
    session: AsyncSession,
    job: PublishJob,
    now: datetime,
) -> tuple[int | None, tuple[int, int, str] | None]:
    """Опубликовать одну задачу. Возвращает (publication_id, уведомление админу)."""
    from app.models.entities import City

    chat = await session.scalar(select(Chat).where(Chat.id == job.chat_id))
    if not chat or not chat.telegram_chat_id:
        job.status = "failed"
        job.failed_at = now
        job.error = "chat_not_configured"
        await log_event(
            session,
            "failed",
            publication_id=job.publication_id,
            job_id=job.id,
            chat_id=job.chat_id,
            detail="chat_not_configured",
        )
        return job.publication_id, (job.publication_id or 0, job.chat_id, "chat_not_configured")

    city_key = ""
    city = await session.scalar(select(City).where(City.id == chat.city_id))
    if city:
        city_key = city.key

    is_oneshot = job.priority <= PRIORITY_ONESHOT
    pin = False
    if is_oneshot and job.package_order_id:
        order = await session.scalar(select(PackageOrder).where(PackageOrder.id == job.package_order_id))
        pin = bool(order and order.is_pinned)

    try:
        result, delivery = await _deliver(
            bot, job, chat, city_key=city_key, pin=pin, premium=is_oneshot,
        )
        if not result.ok:
            raise RuntimeError(result.error or "relay_failed")
    except Exception as exc:  # noqa: BLE001 — ошибка одной группы не трогает остальные
        logger.warning("Publish failed job=%s chat=%s: %s", job.id, job.chat_id, exc)
        exhausted = await _handle_failure(session, job, str(exc), now)
        if job.status == STATUS_UNCONFIRMED:
            # Пост, вероятнее всего, в группе — значит группа занята и следующий
            # пост должен отстоять свою паузу, как после обычной публикации.
            await mark_published(
                session, job.chat_id, published_at=now, author_user_id=job.author_user_id,
            )
        notice = (job.publication_id or 0, job.chat_id, str(exc)) if exhausted else None
        return job.publication_id, notice

    job.status = "published"
    job.message_id = result.message_id
    job.published_at = now
    job.delivery = delivery
    job.error = None

    await mark_published(session, job.chat_id, published_at=now, author_user_id=job.author_user_id)
    if is_oneshot:
        # Разовый пост уводит группу в тишину (ТЗ 6.4). Длительность
        # настраивается из панели, поэтому читается на каждой публикации.
        until = await set_silence(
            session, job.chat_id, published_at=now,
            minutes=await settings_store.queue_silence_minutes(),
        )
        await defer_chat_jobs(session, job.chat_id, until=until)
        await log_event(
            session,
            "silence_set",
            publication_id=job.publication_id,
            chat_id=job.chat_id,
            detail=f"until={until.isoformat()} source=own_package",
        )

    await log_event(
        session,
        "published",
        publication_id=job.publication_id,
        job_id=job.id,
        chat_id=job.chat_id,
        detail=f"delivery={delivery} message_id={result.message_id} priority={job.priority}",
    )
    return job.publication_id, None


async def _expire_stale_jobs(
    session: AsyncSession,
    now: datetime,
) -> tuple[set[int], list[tuple[int, int, str]]]:
    """Сценарий 10: задача провисела в очереди дольше срока — отменяем.

    ТЗ требует уведомить и пользователя, и администратора, поэтому возвращаем
    ещё и список уведомлений для админов.
    """
    stale = (
        await session.scalars(
            select(PublishJob).where(
                PublishJob.status == "queued",
                PublishJob.expires_at.isnot(None),
                PublishJob.expires_at < now,
            ),
        )
    ).all()
    touched: set[int] = set()
    notices: list[tuple[int, int, str]] = []
    for job in stale:
        job.status = "expired"
        job.failed_at = now
        job.error = "queue_timeout"
        await log_event(
            session,
            "expired",
            publication_id=job.publication_id,
            job_id=job.id,
            chat_id=job.chat_id,
            detail="ждала в очереди дольше срока",
        )
        if job.publication_id:
            touched.add(job.publication_id)
        notices.append((job.publication_id or 0, job.chat_id, "queue_timeout"))
    return touched, notices


async def _publications_awaiting_first_notice(session: AsyncSession) -> set[int]:
    """Публикации с ждущими задачами, которым мы ещё ничего не сообщили."""
    rows = await session.scalars(
        select(PublishJob.publication_id)
        .join(Publication, Publication.id == PublishJob.publication_id)
        .where(PublishJob.status == "queued", Publication.notified_state.is_(None))
        .distinct(),
    )
    return {row for row in rows if row}


async def process_publish_queue(bot: Bot, now: datetime | None = None) -> None:
    """Один проход воркера: каждая группа разбирает свою очередь независимо."""
    from app.db.session import SessionLocal
    from app.services.publication_status import refresh_publication_status

    # Интервал между постами настраивается администратором и читается на
    # каждом проходе воркера — изменение действует без перезапуска (ТЗ 6.10).
    pause_sec = await settings_store.queue_user_pause_sec()
    now = now or datetime.now(UTC)
    touched: set[int] = set()
    admin_notices: list[tuple[int, int, str]] = []

    async with SessionLocal() as session:
        expired_touched, expired_notices = await _expire_stale_jobs(session, now)
        touched |= expired_touched
        admin_notices.extend(expired_notices)

        chat_ids = list(
            await session.scalars(
                select(PublishJob.chat_id)
                .where(PublishJob.status == "queued", PublishJob.scheduled_at <= now)
                .distinct(),
            ),
        )

        for chat_id in chat_ids:
            decision = await pick_next_job(
                session, chat_id, now=now, pause_sec=pause_sec,
            )
            if decision.job is None:
                if decision.ready_at:
                    # Группа занята: посты не отклоняются, а ждут своего слота
                    await defer_chat_jobs(session, chat_id, until=decision.ready_at)
                    if decision.state in (SLOT_SILENCE, SLOT_PAUSE):
                        await log_event(
                            session,
                            "paused",
                            chat_id=chat_id,
                            detail=f"{decision.state} until={decision.ready_at.isoformat()}",
                        )
                continue

            publication_id, notice = await _publish_one(bot, session, decision.job, now)
            if publication_id:
                touched.add(publication_id)
            if notice:
                admin_notices.append(notice)

        # Публикации, которые целиком стоят в очереди (группа в тишине или на паузе),
        # тоже должны получить уведомление — иначе пользователь не узнает об очереди
        touched |= await _publications_awaiting_first_notice(session)

        await session.commit()

    for publication_id in touched:
        try:
            await refresh_publication_status(bot, publication_id)
        except Exception:
            logger.exception("Publication status refresh failed publication=%s", publication_id)

    if admin_notices:
        from app.services.notifications import notify_admins_publish_failed

        for publication_id, chat_id, error in admin_notices:
            try:
                await notify_admins_publish_failed(publication_id, chat_id, error)
            except Exception:
                logger.exception("Admin notify failed chat=%s", chat_id)
