import asyncio
from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.entities import User, Subscription, WhitelistEntry

TG = 8721912962


async def main() -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == TG))
        print("USER", user.id if user else None, user.username if user else None)
        if user:
            subs = (
                await session.scalars(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.id.desc()),
                )
            ).all()
            print("SUBS", len(subs))
            for s in subs:
                print(
                    f"  id={s.id} status={s.status.value} cat={s.category_code} "
                    f"expires={s.expires_at} price={s.total_price}",
                )
        wl = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.telegram_id == TG))
        print("WL", wl.id if wl else None, getattr(wl, "access_days", None))
        entries = (await session.scalars(select(WhitelistEntry).order_by(WhitelistEntry.id.desc()).limit(8))).all()
        for e in entries:
            print(f"  wl#{e.id} tg={e.telegram_id} @{e.username} days={e.access_days}")


asyncio.run(main())
