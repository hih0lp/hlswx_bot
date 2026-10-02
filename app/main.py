import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import api_router
from app.bot.runtime import (
    all_polling_bots,
    get_bot,
    get_partner_for_bot,
    init_partner_bots,
    set_partner_reload_event,
    start_main_bot_polling,
    stop_all_polling,
)
from app.config import get_settings
from app.core.seed import seed, wait_db
from app.services import access, tariffs
from app.services.payment_reconcile import cancel_stale_payments, reconcile_pending_payments
from app.services.maintenance import purge_old_failed_publications
from app.services.tenant import partner_scope
from app.services.publish import process_publish_queue
from app.services.subscription_reminders import process_subscription_reminders
from app.services.franchise_billing import process_franchise_billing
from app.services.welcome import setup_bot_ui

logger = logging.getLogger("main")


async def _polling_loop() -> None:
    """Держит основного бота в поллинге и пересобирает набор ботов-партнёров
    по сигналу перезагрузки.

    Каждый бот опрашивается своей отдельной задачей (см. `_start_bot_task` /
    `init_partner_bots` в `app/bot/runtime.py`) — перезапуск одного клона
    не трогает поллинг ни основного бота, ни других клонов (ТЗ этапа 3,
    раздел 7).
    """
    start_main_bot_polling()
    reload_event = asyncio.Event()
    set_partner_reload_event(reload_event)

    while True:
        await reload_event.wait()
        reload_event.clear()
        logger.info("Partner bot registry reload requested")
        try:
            await init_partner_bots()
        except Exception:
            logger.exception("Partner bot registry reload failed")
        else:
            logger.info("Partner bot registry reloaded")


async def _queue_worker() -> None:
    while True:
        for bot in all_polling_bots():
            partner = get_partner_for_bot(bot.id)
            with partner_scope(
                partner.id if partner else 0,
                partner.owner_telegram_id if partner else None,
                getattr(partner, "franchise_tier", "") if partner else "",
            ):
                try:
                    await process_publish_queue(bot)
                except Exception:
                    logger.exception("Queue worker error bot_id=%s", bot.id)
        await asyncio.sleep(15)


async def _subscription_reminder_worker() -> None:
    while True:
        for bot in all_polling_bots():
            partner = get_partner_for_bot(bot.id)
            with partner_scope(
                partner.id if partner else 0,
                partner.owner_telegram_id if partner else None,
                getattr(partner, "franchise_tier", "") if partner else "",
            ):
                try:
                    sent = await process_subscription_reminders()
                    if sent["reminded"] or sent["expired"]:
                        logger.info(
                            "Subscription reminders bot_id=%s: %s soon, %s expired",
                            bot.id, sent["reminded"], sent["expired"],
                        )
                except Exception:
                    logger.exception("Subscription reminder error bot_id=%s", bot.id)
        await asyncio.sleep(300)
async def _payment_reconcile_worker() -> None:
    while True:
        for bot in all_polling_bots():
            partner = get_partner_for_bot(bot.id)
            with partner_scope(
                partner.id if partner else 0,
                partner.owner_telegram_id if partner else None,
                getattr(partner, "franchise_tier", "") if partner else "",
            ):
                try:
                    count = await reconcile_pending_payments()
                    if count:
                        logger.info("Reconciled %s pending payment(s) bot_id=%s", count, bot.id)
                    await cancel_stale_payments()
                except Exception:
                    logger.exception("Payment reconcile error bot_id=%s", bot.id)
        await asyncio.sleep(90)
async def _franchise_billing_worker() -> None:
    while True:
        try:
            result = await process_franchise_billing()
            if any(result.values()):
                logger.info("Franchise billing run: %s", result)
        except Exception:
            logger.exception("Franchise billing worker error")
        await asyncio.sleep(3600)


async def _maintenance_worker() -> None:
    while True:
        for bot in all_polling_bots():
            partner = get_partner_for_bot(bot.id)
            with partner_scope(
                partner.id if partner else 0,
                partner.owner_telegram_id if partner else None,
                getattr(partner, "franchise_tier", "") if partner else "",
            ):
                try:
                    deleted = await purge_old_failed_publications()
                    if deleted:
                        logger.info("Maintenance bot_id=%s removed %s old failures", bot.id, deleted)
                except Exception:
                    logger.exception("Maintenance error bot_id=%s", bot.id)
        await asyncio.sleep(24 * 3600)
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    logging.basicConfig(level=logging.INFO)

    await wait_db()
    await seed()
    # Роли доступа к панели читаются из памяти процесса — прогреваем до того,
    # как первый апдейт попросит собрать клавиатуру (ТЗ этапа 2, 7).
    await access.refresh()
    await tariffs.refresh()
    await setup_bot_ui(get_bot())
    await init_partner_bots()

    polling_task = asyncio.create_task(_polling_loop())
    queue_task = asyncio.create_task(_queue_worker())
    reconcile_task = asyncio.create_task(_payment_reconcile_worker())
    reminder_task = asyncio.create_task(_subscription_reminder_worker())
    maintenance_task = asyncio.create_task(_maintenance_worker())
    franchise_billing_task = asyncio.create_task(_franchise_billing_worker())
    logger.info("HWLS bot started on port %s", settings.webhook_port)
    try:
        yield
    finally:
        polling_task.cancel()
        queue_task.cancel()
        reconcile_task.cancel()
        reminder_task.cancel()
        maintenance_task.cancel()
        franchise_billing_task.cancel()
        await asyncio.gather(
            polling_task, queue_task, reconcile_task, reminder_task, maintenance_task, franchise_billing_task,
            return_exceptions=True,
        )
        await stop_all_polling()
        for bot in all_polling_bots():
            await bot.session.close()


app = FastAPI(title="HWLS Bot", lifespan=lifespan)
app.include_router(api_router)
