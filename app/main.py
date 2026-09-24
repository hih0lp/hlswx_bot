import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import api_router
from app.bot.runtime import all_polling_bots, get_bot, get_dispatcher, init_partner_bots
from app.config import get_settings
from app.core.seed import seed, wait_db
from app.services import access, tariffs
from app.services.payment_reconcile import cancel_stale_payments, reconcile_pending_payments
from app.services.maintenance import purge_old_failed_publications
from app.services.publish import process_publish_queue
from app.services.subscription_reminders import process_subscription_reminders
from app.services.welcome import setup_bot_ui

logger = logging.getLogger("main")


async def _polling_loop() -> None:
    dp = get_dispatcher()
    while True:
        try:
            bots = all_polling_bots()
            labels = ", ".join(f"#{idx + 1}" for idx in range(len(bots)))
            logger.info("Starting Telegram polling for %s bot(s): %s", len(bots), labels)
            await dp.start_polling(*bots, handle_signals=False)
        except Exception:
            logger.exception("Polling crashed, retry in 10s")
            await asyncio.sleep(10)


async def _queue_worker() -> None:
    bot = get_bot()
    while True:
        try:
            await process_publish_queue(bot)
        except Exception:
            logger.exception("Queue worker error")
        await asyncio.sleep(15)


async def _subscription_reminder_worker() -> None:
    """Напоминание за 24 часа до конца подписки и уведомление об окончании.

    Горизонт суточный, поэтому тика раз в пять минут более чем достаточно;
    повторных сообщений не будет — отметки хранятся в самой подписке.
    """
    while True:
        try:
            sent = await process_subscription_reminders()
            if sent["reminded"] or sent["expired"]:
                logger.info(
                    "Subscription reminders: %s soon, %s expired",
                    sent["reminded"], sent["expired"],
                )
        except Exception:
            logger.exception("Subscription reminder error")
        await asyncio.sleep(300)


async def _payment_reconcile_worker() -> None:
    while True:
        try:
            count = await reconcile_pending_payments()
            if count:
                logger.info("Reconciled %s pending payment(s)", count)
            # Всё, что за двое суток так и не подтвердилось, закрываем: иначе
            # счётчик «платежи в обработке» в панели растёт и не убывает.
            await cancel_stale_payments()
        except Exception:
            logger.exception("Payment reconcile error")
        await asyncio.sleep(90)


async def _maintenance_worker() -> None:
    """Раз в сутки убираем неудачные публикации старше 30 дней."""
    while True:
        try:
            deleted = await purge_old_failed_publications()
            if deleted:
                logger.info("Maintenance: удалено неудачных публикаций старше 30 дней: %s", deleted)
        except Exception:
            logger.exception("Maintenance error")
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
    logger.info("HWLS bot started on port %s", settings.webhook_port)
    try:
        yield
    finally:
        polling_task.cancel()
        queue_task.cancel()
        reconcile_task.cancel()
        reminder_task.cancel()
        maintenance_task.cancel()
        await asyncio.gather(
            polling_task, queue_task, reconcile_task, reminder_task, maintenance_task,
            return_exceptions=True,
        )
        for bot in all_polling_bots():
            await bot.session.close()


app = FastAPI(title="HWLS Bot", lifespan=lifespan)
app.include_router(api_router)
