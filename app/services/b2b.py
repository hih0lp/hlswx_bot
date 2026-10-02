from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.entities import B2bIntegration, Subscription, SubscriptionStatus
from app.services.pricing import PLAN_CORP_B2B


def new_b2b_token() -> str:
    return secrets.token_urlsafe(24)


def build_webhook_url(token: str) -> str:
    base = get_settings().public_url.rstrip("/")
    return f"{base}/api/b2b/publish/{token}"


def build_wl_publish_url() -> str:
    base = get_settings().public_url.rstrip("/")
    return f"{base}/api/wl/publish"


async def get_active_integration(session: AsyncSession, user_id: int) -> B2bIntegration | None:
    return await session.scalar(
        select(B2bIntegration)
        .where(B2bIntegration.user_id == user_id, B2bIntegration.active.is_(True))
        .order_by(B2bIntegration.id.desc()),
    )


async def get_active_corp_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    """Любая активная подписка даёт право на вебхук-интеграцию (ТЗ 6.10):

    заказчик подтвердил, что это не отдельный корп-тариф, а обычная платная
    подписка (хоть на один чат) — компания получает по ней вебхук-ссылку
    и вставляет её в код своего бота вместо ручной публикации в HLSWX.
    Имя функции сохранено — используется во всех точках входа ниже.
    """
    now = datetime.now(UTC)
    return await session.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.active,
            Subscription.expires_at > now,
        )
        .order_by(Subscription.id.desc()),
    )


async def ensure_integration_for_subscription(
    session: AsyncSession,
    user_id: int,
    subscription_id: int,
    *,
    commit: bool = True,
) -> B2bIntegration:
    sub = await session.scalar(select(Subscription).where(Subscription.id == subscription_id))
    if not sub or sub.status != SubscriptionStatus.active:
        raise ValueError("B2B доступен только по активной подписке")

    existing = await get_active_integration(session, user_id)
    if existing:
        existing.subscription_id = subscription_id
        existing.active = True
        row = existing
    else:
        row = B2bIntegration(
            user_id=user_id,
            subscription_id=subscription_id,
            token=new_b2b_token(),
            active=True,
        )
        session.add(row)

    if commit:
        await session.commit()
        await session.refresh(row)
    else:
        await session.flush()
    return row


async def on_corp_subscription_activated(session: AsyncSession, user_id: int, subscription_id: int) -> None:
    sub = await session.scalar(select(Subscription).where(Subscription.id == subscription_id))
    if not sub or (sub.plan_type or "standard") != PLAN_CORP_B2B:
        return
    await ensure_integration_for_subscription(session, user_id, subscription_id, commit=False)


async def integration_for_token(session: AsyncSession, token: str) -> B2bIntegration | None:
    return await session.scalar(
        select(B2bIntegration)
        .where(B2bIntegration.token == token, B2bIntegration.active.is_(True))
        # Public webhook token is the capability used to resolve tenant scope.
        .execution_options(skip_partner_scope=True),
    )


async def integration_is_live(session: AsyncSession, integration: B2bIntegration) -> bool:
    if not integration.active:
        return False
    if not integration.subscription_id:
        return False
    sub = await session.scalar(select(Subscription).where(Subscription.id == integration.subscription_id))
    if not sub or sub.status != SubscriptionStatus.active:
        return False
    if sub.expires_at and sub.expires_at < datetime.now(UTC):
        return False
    return True
