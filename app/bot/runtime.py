import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from app.bot.session import ResilientSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.handlers.admin import admin_router
from app.handlers.admin_broadcast import admin_broadcast_router
from app.handlers.connect import connect_router
from app.handlers.franchise import franchise_router
from app.handlers.menu import menu_router
from app.handlers.package import package_router
from app.handlers.publication_actions import publication_actions_router
from app.handlers.publish import publish_router
from app.handlers.start import start_router
from app.handlers.subscription import subscription_router
from app.handlers.wallet import wallet_router
from app.handlers.whitelabel_apply import whitelabel_apply_router
from app.models.entities import WhitelabelPartner

logger = logging.getLogger("bot")

POLLABLE_BILLING_STATUSES = ("active", "manual", "grace", "suspended")

_bot: Bot | None = None
_partner_bots: list[Bot] = []
_partner_by_bot_id: dict[int, WhitelabelPartner] = {}
_partner_bot_by_partner_id: dict[int, Bot] = {}
_dp: Dispatcher | None = None
_partner_reload_event: asyncio.Event | None = None
_retired_partner_bots: set[Bot] = set()
# Одна задача поллинга на бота (ТЗ этапа 3, раздел 7: «Остановка или
# перезапуск отдельного бота-клона не должны прерывать работу основного
# бота и других клонов»). Раньше все боты опрашивались одним общим
# `dp.start_polling(*bots)`, и перезапуск ради одного клона на время глушил
# вообще все — Telegram отвечал конфликтом getUpdates каждому боту разом.
_bot_tasks: dict[int, asyncio.Task] = {}


def _get_main_bot() -> Bot:
    global _bot
    if _bot is None:
        settings = get_settings()
        session = ResilientSession(timeout=120)
        _bot = Bot(
            token=settings.bot_token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _bot


def get_bot() -> Bot:
    """Return the bot for the active tenant; never fall back to the platform bot."""
    from app.services.tenant import current_partner_id

    partner_id = current_partner_id()
    if partner_id:
        bot = _partner_bot_by_partner_id.get(partner_id)
        if bot is None:
            raise RuntimeError(f"Partner bot {partner_id} is not running")
        return bot
    return _get_main_bot()


def get_partner_for_bot(bot_id: int) -> WhitelabelPartner | None:
    return _partner_by_bot_id.get(bot_id)


def set_partner_billing_state(partner_id: int, status: str, tier: str | None = None) -> None:
    """Refresh the cached tenant record after a payment or grace-period update."""
    for partner in _partner_by_bot_id.values():
        if partner.id == partner_id:
            partner.franchise_billing_status = status
            if tier:
                partner.franchise_tier = tier


def all_polling_bots() -> list[Bot]:
    bots = [_get_main_bot()]
    bots.extend(_partner_bots)
    return bots


def get_dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        _dp = Dispatcher(storage=MemoryStorage())
        from app.bot.tenant_middleware import PartnerScopeMiddleware

        _dp.update.outer_middleware(PartnerScopeMiddleware())
        from app.bot.album_middleware import AlbumFirstOnlyMiddleware

        _dp.message.outer_middleware(AlbumFirstOnlyMiddleware())
        _dp.include_router(menu_router)
        _dp.include_router(connect_router)
        _dp.include_router(whitelabel_apply_router)
        _dp.include_router(wallet_router)
        _dp.include_router(start_router)
        _dp.include_router(subscription_router)
        _dp.include_router(package_router)
        _dp.include_router(publish_router)
        _dp.include_router(publication_actions_router)
        _dp.include_router(franchise_router)
        _dp.include_router(admin_broadcast_router)
        _dp.include_router(admin_router)
    return _dp


def _start_bot_task(bot: Bot) -> None:
    """Запустить независимый поллинг ровно для одного бота.

    Публичный `dp.start_polling()` держит блокировку на весь Dispatcher и не
    даёт запустить его второй раз, пока первый вызов не завершится — для
    набора ботов, который меняется на лету (подключение франшизы), это не
    подходит: второй вызов просто зависнет в ожидании блокировки первого.
    Используем внутренний `Dispatcher._polling()` — ровно то, что
    `start_polling()` сама создаёт как отдельную задачу на каждого бота,
    только без общей блокировки и без startup/shutdown-хуков (в проекте
    `dp.startup`/`dp.shutdown` нигде не регистрируются, поэтому пропускать
    их безопасно).
    """
    dp = get_dispatcher()
    _bot_tasks[bot.id] = asyncio.create_task(
        dp._polling(bot=bot, polling_timeout=10, allowed_updates=dp.resolve_used_update_types())
    )


async def _stop_bot_task(bot_id: int) -> None:
    """Остановить поллинг ровно одного бота, не затрагивая остальных."""
    task = _bot_tasks.pop(bot_id, None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Polling task for bot id=%s ended with an error", bot_id)


def start_main_bot_polling() -> None:
    """Запустить поллинг основного бота — вызывается один раз при старте."""
    bot = _get_main_bot()
    if bot.id not in _bot_tasks:
        _start_bot_task(bot)


async def stop_all_polling() -> None:
    """Остановить поллинг всех ботов — используется только при остановке процесса."""
    for bot_id in list(_bot_tasks):
        await _stop_bot_task(bot_id)


def set_partner_reload_event(event: asyncio.Event) -> None:
    """Attach the polling supervisor's reload event."""
    global _partner_reload_event
    _partner_reload_event = event


def request_partner_bots_reload() -> None:
    """Request a safe partner-bot registry reload.

    The request is intentionally non-blocking. The polling supervisor owns the
    Dispatcher lifecycle and performs the actual reload only after polling has
    stopped, so a bot token is never polled by two Bot instances at once.
    """
    if _partner_reload_event is None:
        logger.warning("Partner bot reload requested before polling supervisor is ready")
        return
    _partner_reload_event.set()


async def _retire_partner_bot(bot: Bot) -> None:
    """Close a replaced partner session after in-flight workers have had time to finish."""
    try:
        await asyncio.sleep(30)
        await bot.session.close()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Failed to close retired partner bot session id=%s", getattr(bot, "id", "?"))
    finally:
        _retired_partner_bots.discard(bot)


def _pollable_partner_statement():
    """Select only active franchise tenants whose billing state permits polling."""
    return (
        select(WhitelabelPartner)
        .where(
            WhitelabelPartner.active.is_(True),
            WhitelabelPartner.bot_token.isnot(None),
            WhitelabelPartner.franchise_billing_status.in_(POLLABLE_BILLING_STATUSES),
        )
        .order_by(WhitelabelPartner.id)
    )


async def init_partner_bots() -> None:
    """Synchronize the in-process partner bot registry with the database.

    Existing Bot instances are reused when their partner id and token are
    unchanged. New/changed partners receive a new Bot instance. Removed or
    replaced instances are retired after a short grace period so background
    workers cannot hit a freshly closed aiohttp session.

    This function must be called by the polling supervisor, not directly from
    an update handler while polling is running.
    """
    global _partner_bots, _partner_by_bot_id, _partner_bot_by_partner_id

    async with SessionLocal() as session:
        partners = (
            await session.scalars(
                _pollable_partner_statement(),
            )
        ).all()

    old_by_partner_id = dict(_partner_bot_by_partner_id)
    new_bots: list[Bot] = []
    new_by_bot_id: dict[int, WhitelabelPartner] = {}
    new_by_partner_id: dict[int, Bot] = {}
    retired: list[Bot] = []

    for partner in partners:
        from app.services.partner_tenants import provision_partner_tenant

        await provision_partner_tenant(partner)
        token = (partner.bot_token or "").strip()
        if not token:
            continue

        existing = old_by_partner_id.get(partner.id)
        me = None
        if existing is not None and existing.token == token:
            bot = existing
            try:
                me = await bot.get_me()
            except Exception:
                logger.exception("Existing partner bot health check failed for @%s", partner.bot_username)
                retired.append(bot)
                bot = None
        else:
            bot = None
            if existing is not None:
                retired.append(existing)

        if bot is None:
            bot = Bot(
                token=token,
                session=ResilientSession(timeout=120),
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                me = await bot.get_me()
                if partner.brand_title:
                    try:
                        await bot.set_my_name(partner.brand_title)
                    except Exception:
                        logger.exception("set_my_name failed for @%s", partner.bot_username)
            except Exception:
                logger.exception("Failed to init partner bot @%s", partner.bot_username)
                await bot.session.close()
                continue
        if me is None:
            me = await bot.get_me()

        new_bots.append(bot)
        new_by_bot_id[me.id] = partner
        new_by_partner_id[partner.id] = bot
        logger.info(
            "Partner bot ready: @%s id=%s brand=%s",
            partner.bot_username,
            me.id,
            partner.brand_title or partner.bot_username,
        )

    current_ids = {id(bot) for bot in new_bots}
    for bot in old_by_partner_id.values():
        if id(bot) not in current_ids and bot not in retired:
            retired.append(bot)

    _partner_bots = new_bots
    _partner_by_bot_id = new_by_bot_id
    _partner_bot_by_partner_id = new_by_partner_id

    # Своя задача поллинга на каждого бота: у снятых/заменённых партнёров
    # останавливаем именно их задачу, у новых — запускаем именно их. Бота,
    # чей токен не менялся, эта синхронизация вообще не трогает — его
    # поллинг продолжает крутиться как ни в чём не бывало.
    for bot in retired:
        await _stop_bot_task(bot.id)
    for bot in new_bots:
        if bot.id not in _bot_tasks:
            _start_bot_task(bot)

    for bot in retired:
        if bot in _retired_partner_bots:
            continue
        _retired_partner_bots.add(bot)
        asyncio.create_task(_retire_partner_bot(bot))

    logger.info(
        "Partner bot registry synchronized: %s polling-enabled partner bot(s)",
        len(_partner_bots),
    )

    from app.services import access, tariffs

    await access.refresh()
    await tariffs.refresh()


async def check_partner_token(token: str) -> tuple[bool, str]:
    """Живая проверка токена партнёрского бота (ТЗ 6.7, кадр 344:1478).

    Возвращает (успех, что показать администратору). Ровно этот вызов делает
    `init_partner_bots` при старте, но там результат уходит только в лог —
    поэтому в админке партнёр с протухшим токеном всё равно выглядел
    подключённым.

    Проверка ничего не сохраняет: поля «последняя активность» в таблице
    партнёров нет, а заводить его ради галочки в этап не входит.
    """
    token = (token or "").strip()
    if not token:
        return False, "токен не задан"
    bot = Bot(
        token=token,
        session=ResilientSession(timeout=30),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        me = await bot.get_me()
    except Exception as exc:
        # Bot API exceptions can contain the submitted token in their URL.
        logger.info("Проверка токена партнёра не прошла (%s)", type(exc).__name__)
        return False, "не удалось проверить токен"
    else:
        return True, f"@{me.username}" if me.username else str(me.id)
    finally:
        await bot.session.close()


def partner_bot_is_running(partner_id: int) -> bool:
    """Поднят ли polling-бот этого партнёра в текущем процессе."""
    return any(partner.id == partner_id for partner in _partner_by_bot_id.values())


async def reload_partner_bot(partner_id: int) -> None:
    """Backward-compatible API: request a safe registry reload."""
    request_partner_bots_reload()
