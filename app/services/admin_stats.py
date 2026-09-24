"""Показатели для админ-панели (ТЗ этапа 2, разделы 6.1, 6.2, 6.4).

Выручка считается по таблице `payments` — другого источника денег в базе нет.
Две ловушки, обе обходим явно:

* бесплатная активация по белому списку пишет успешный платёж на 0 ₽
  (`hwls-wl-…`) — он не выручка;
* оплата с баланса создаёт **второй** успешный платёж (`hwls-bal-…`) поверх
  пополнения — если сложить всё подряд, выручка удвоится.

Различить их можно только по префиксу `invoice_id`: отдельной колонки-признака
в модели нет. Префиксы собраны в `services.payments`.

Деления на «новые подписки» и «продления» в модели тоже нет: каждая покупка
создаёт новую строку `Subscription`. Продлением считаем покупку пользователя,
у которого уже была более ранняя подписка, — это приближение, но другого
признака в данных не существует.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import aliased

from app.services.textfmt import DISPLAY_TZ
from app.db.session import SessionLocal
from app.models.entities import (
    ClassificationLog,
    PackageOrder,
    Payment,
    PaymentStatus,
    Publication,
    PublishJob,
    Subscription,
    SubscriptionStatus,
    User,
    WhitelabelApplication,
    WhitelabelApplicationStatus,
    WhitelistEntry,
)
from app.services.payments import CASH_IN_PREFIXES

# Публикация считается неудавшейся и когда попытки исчерпаны (`failed`), и когда
# она протухла в очереди по TTL (`expired`). Раздел «Публикации» всегда показывал
# оба статуса, а главный экран — только первый; теперь определение одно на всех.
FAILED_STATUSES = ("failed", "expired")

# Периоды с экрана «Выручка» (кадры макета 337:364 и 337:410).
PERIOD_TODAY = "today"
PERIOD_7 = "7d"
PERIOD_30 = "30d"
PERIOD_ALL = "all"

PERIOD_LABELS = {
    PERIOD_TODAY: "Сегодня",
    PERIOD_7: "За 7 дней",
    PERIOD_30: "За 30 дней",
    PERIOD_ALL: "Всё время",
}

# Подпись сравнения с предыдущим отрезком той же длины.
PERIOD_PREV_LABELS = {
    PERIOD_TODAY: "к предыдущему дню",
    PERIOD_7: "к предыдущим 7 дням",
    PERIOD_30: "к предыдущим 30 дням",
}


def period_bounds(period: str, now: datetime | None = None) -> tuple[datetime | None, datetime]:
    """(начало, конец) периода. Начало `None` — «всё время».

    Сутки считаются по Москве, как и все времена в панели: иначе счётчики за
    сегодня обнулялись бы в 03:00, а не в полночь (заказчик 21.09.2026).
    """
    now = now or datetime.now(UTC)
    if period == PERIOD_ALL:
        return None, now
    if period == PERIOD_TODAY:
        local = now.astimezone(DISPLAY_TZ)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(UTC), now
    days = 7 if period == PERIOD_7 else 30
    return now - timedelta(days=days), now


def _cash_in_filter():
    """Только настоящий приход денег — без оплат с баланса и нулевых записей."""
    return or_(*[Payment.invoice_id.startswith(prefix) for prefix in CASH_IN_PREFIXES])


@dataclass(frozen=True)
class Revenue:
    period: str
    amount: Decimal
    previous: Decimal
    payments: int
    new_subscriptions: int
    renewals: int

    @property
    def change_percent(self) -> int | None:
        """Изменение к предыдущему периоду. None — сравнивать не с чем."""
        if not self.previous:
            return None
        return round((self.amount - self.previous) / self.previous * 100)


async def revenue(period: str = PERIOD_TODAY, now: datetime | None = None) -> Revenue:
    """Подраздел «Выручка» (ТЗ 6.2)."""
    now = now or datetime.now(UTC)
    start, end = period_bounds(period, now)

    async with SessionLocal() as session:
        amount = await _sum_paid(session, start, end)
        count = await _count_paid(session, start, end)

        previous = Decimal(0)
        if start is not None:
            span = end - start
            previous = await _sum_paid(session, start - span, start)

        # Продление — покупка пользователя, у которого уже была подписка раньше.
        # Признака в данных нет, поэтому смотрим на более раннюю строку того
        # же пользователя коррелированным EXISTS.
        previous_sub = aliased(Subscription)
        is_renewal = exists().where(
            previous_sub.user_id == Subscription.user_id,
            previous_sub.id != Subscription.id,
            previous_sub.created_at < Subscription.created_at,
        )
        activated = [Subscription.starts_at.is_not(None), Subscription.starts_at <= end]
        if start is not None:
            activated.append(Subscription.starts_at >= start)

        renewals = await session.scalar(
            select(func.count()).select_from(Subscription).where(*activated, is_renewal),
        ) or 0
        new_subs = await session.scalar(
            select(func.count()).select_from(Subscription).where(*activated, ~is_renewal),
        ) or 0

    return Revenue(
        period=period,
        amount=amount,
        previous=previous,
        payments=count,
        new_subscriptions=new_subs,
        renewals=renewals,
    )


async def _sum_paid(session, start: datetime | None, end: datetime) -> Decimal:
    query = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.status == PaymentStatus.succeeded,
        Payment.paid_at.is_not(None),
        Payment.paid_at <= end,
        _cash_in_filter(),
    )
    if start is not None:
        query = query.where(Payment.paid_at >= start)
    return Decimal(await session.scalar(query) or 0)


async def _count_paid(session, start: datetime | None, end: datetime) -> int:
    query = select(func.count()).select_from(Payment).where(
        Payment.status == PaymentStatus.succeeded,
        Payment.paid_at.is_not(None),
        Payment.paid_at <= end,
        _cash_in_filter(),
    )
    if start is not None:
        query = query.where(Payment.paid_at >= start)
    return await session.scalar(query) or 0


async def payments_summary(now: datetime | None = None) -> dict:
    """Сводка раздела «Оплаты» — кадр макета 340:320."""
    now = now or datetime.now(UTC)
    start, end = period_bounds(PERIOD_TODAY, now)
    async with SessionLocal() as session:
        return {
            "revenue_today": await _sum_paid(session, start, end),
            "paid": await _count_paid(session, start, end),
            "processing": await session.scalar(
                select(func.count()).select_from(Payment).where(Payment.status == PaymentStatus.pending),
            ) or 0,
            # Ошибки — только за текущие сутки: иначе счётчик копит отменённые
            # счета месяцами и не убывает (заказчик 21.09.2026).
            "failed": await session.scalar(
                select(func.count())
                .select_from(Payment)
                .where(
                    Payment.status.in_((PaymentStatus.failed, PaymentStatus.canceled)),
                    Payment.created_at >= start,
                ),
            ) or 0,
            "awaiting_subs": await session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.status == SubscriptionStatus.pending_payment),
            ) or 0,
        }


async def publications_summary(now: datetime | None = None) -> dict:
    """Сводка раздела «Публикации» — кадр макета 340:733."""
    now = now or datetime.now(UTC)
    day_start, _ = period_bounds(PERIOD_TODAY, now)
    async with SessionLocal() as session:
        return {
            "queued": await session.scalar(
                select(func.count()).select_from(PublishJob).where(PublishJob.status == "queued"),
            ) or 0,
            "published_today": await session.scalar(
                select(func.count())
                .select_from(PublishJob)
                .where(PublishJob.status == "published", PublishJob.published_at >= day_start),
            ) or 0,
            # Ошибки — только за текущие сутки (заказчик 21.09.2026).
            "failed": await session.scalar(
                select(func.count())
                .select_from(PublishJob)
                .where(
                    PublishJob.status.in_(FAILED_STATUSES),
                    func.coalesce(PublishJob.failed_at, PublishJob.scheduled_at) >= day_start,
                ),
            ) or 0,
        }


async def stats_overview(period: str = PERIOD_7) -> dict:
    """Показатели раздела «Статистика» — кадр макета 337:336."""
    start, end = period_bounds(period)
    async with SessionLocal() as session:
        users = select(func.count()).select_from(User)
        if start is not None:
            users = users.where(User.created_at >= start, User.created_at <= end)
        return {
            "period": period,
            "users": await session.scalar(users) or 0,
            "active_subs": await session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.status == SubscriptionStatus.active),
            ) or 0,
            "awaiting_payment": await session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.status == SubscriptionStatus.pending_payment),
            ) or 0,
            "payments_processing": await session.scalar(
                select(func.count()).select_from(Payment).where(Payment.status == PaymentStatus.pending),
            ) or 0,
        }


async def gather_dashboard_stats() -> dict:
    """Сводка главного экрана панели (ТЗ 6.1) плюс счётчики для старых экранов."""
    now = datetime.now(UTC)
    day_start, _ = period_bounds(PERIOD_TODAY, now)
    async with SessionLocal() as session:
        users = await session.scalar(select(func.count()).select_from(User)) or 0
        active_subs = await session.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.status == SubscriptionStatus.active),
        ) or 0
        pending_subs = await session.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.status == SubscriptionStatus.pending_payment),
        ) or 0
        pending_pkgs = await session.scalar(
            select(func.count()).select_from(PackageOrder).where(PackageOrder.status == "pending_payment"),
        ) or 0
        # «Требует внимания» — сводка за сегодняшний день, а не за всё время:
        # иначе счётчик только растёт и висит мёртвым грузом (правка 17.09.2026).
        # Ошибка — это и `failed`, и протухшая по TTL `expired`: так же считает
        # раздел «Публикации» (handlers/admin/publications.py).
        pending_payments = await session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(Payment.status == PaymentStatus.pending, Payment.created_at >= day_start),
        ) or 0
        queue_queued = await session.scalar(
            select(func.count()).select_from(PublishJob).where(PublishJob.status == "queued"),
        ) or 0
        queue_failed = await session.scalar(
            select(func.count())
            .select_from(PublishJob)
            .where(PublishJob.status.in_(FAILED_STATUSES), PublishJob.failed_at >= day_start),
        ) or 0
        publications_today = await session.scalar(
            select(func.count())
            .select_from(Publication)
            .where(Publication.created_at >= day_start),
        ) or 0
        wl_count = await session.scalar(select(func.count()).select_from(WhitelistEntry)) or 0
        # Объявления, ждущие вердикта администратора (ТЗ 6.11, кадр 349:354).
        # Раньше считались только те, что модель отнесла к «Прочему», — но на
        # экране проверки нужны все непроверенные, а не подмножество.
        review_logs = await session.scalar(
            select(func.count())
            .select_from(ClassificationLog)
            .where(ClassificationLog.admin_verdict.is_(None)),
        ) or 0
        wl_pending = await session.scalar(
            select(func.count())
            .select_from(WhitelabelApplication)
            .where(WhitelabelApplication.status == WhitelabelApplicationStatus.pending),
        ) or 0
    return {
        "users": users,
        "active_subs": active_subs,
        "pending_subs": pending_subs,
        "pending_pkgs": pending_pkgs,
        "pending_payments": pending_payments,
        "queue_queued": queue_queued,
        "queue_failed": queue_failed,
        "publications_today": publications_today,
        "whitelist": wl_count,
        "wl_pending": wl_pending,
        "review_logs": review_logs,
    }
