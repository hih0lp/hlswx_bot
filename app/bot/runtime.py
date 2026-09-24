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

_bot: Bot | None = None
_partner_bots: list[Bot] = []
_partner_by_bot_id: dict[int, WhitelabelPartner] = {}
_dp: Dispatcher | None = None


def get_bot() -> Bot:
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


def get_partner_for_bot(bot_id: int) -> WhitelabelPartner | None:
    return _partner_by_bot_id.get(bot_id)


def all_polling_bots() -> list[Bot]:
    bots = [get_bot()]
    bots.extend(_partner_bots)
    return bots


def get_dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        _dp = Dispatcher(storage=MemoryStorage())
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


async def init_partner_bots() -> None:
    global _partner_bots, _partner_by_bot_id
    _partner_bots.clear()
    _partner_by_bot_id.clear()

    async with SessionLocal() as session:
        partners = (
            await session.scalars(
                select(WhitelabelPartner).where(
                    WhitelabelPartner.active.is_(True),
                    WhitelabelPartner.bot_token.isnot(None),
                ),
            )
        ).all()

    for partner in partners:
        token = (partner.bot_token or "").strip()
        if not token:
            continue
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
            _partner_bots.append(bot)
            _partner_by_bot_id[me.id] = partner
            logger.info(
                "Partner bot ready: @%s id=%s brand=%s",
                partner.bot_username,
                me.id,
                partner.brand_title or partner.bot_username,
            )
        except Exception:
            logger.exception("Failed to init partner bot @%s", partner.bot_username)
            await bot.session.close()


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
        logger.info("Проверка токена партнёра не прошла: %s", exc)
        return False, str(exc)[:200]
    else:
        return True, f"@{me.username}" if me.username else str(me.id)
    finally:
        await bot.session.close()


def partner_bot_is_running(partner_id: int) -> bool:
    """Поднят ли polling-бот этого партнёра в текущем процессе."""
    return any(partner.id == partner_id for partner in _partner_by_bot_id.values())


async def reload_partner_bot(partner_id: int) -> None:
    async with SessionLocal() as session:
        partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
        if not partner or not partner.active or not partner.bot_token:
            return
        token = partner.bot_token.strip()

    for index, bot in enumerate(_partner_bots):
        if bot.token == token:
            await bot.session.close()
            _partner_bots.pop(index)
            break

    await init_partner_bots()
