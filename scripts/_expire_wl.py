import asyncio
from datetime import UTC, datetime
from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.entities import Subscription, SubscriptionStatus, User
from app.services.whitelist import WHITELIST_CATEGORY

TG = 8721912962


async def main() -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == TG))
        if not user:
            print("no user")
            return
        now = datetime.now(UTC)
        subs = (
            await session.scalars(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.category_code == WHITELIST_CATEGORY,
                    Subscription.status == SubscriptionStatus.active,
                ),
            )
        ).all()
        for sub in subs:
            sub.status = SubscriptionStatus.expired
            sub.expires_at = now
            print("expired", sub.id)
        if subs:
            await session.commit()
        else:
            print("no active wl subs")


asyncio.run(main())
