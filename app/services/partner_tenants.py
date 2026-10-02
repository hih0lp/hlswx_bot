"""Provision a newly connected franchise with its own starter catalog and owner role."""
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.entities import AdminAccess, AdminRole, TariffCategory, User
from app.services.tenant import partner_scope


async def provision_partner_tenant(partner) -> None:
    partner_id = int(partner.id)
    owner_id = partner.owner_telegram_id
    with partner_scope(partner_id, owner_id, getattr(partner, "franchise_tier", "basic")):
        async with SessionLocal() as session:
            existing_tariff = await session.scalar(select(TariffCategory).limit(1))
            if existing_tariff is None:
                main_tariffs = (await session.scalars(
                    select(TariffCategory)
                    .where(TariffCategory.partner_id == 0)
                    .execution_options(skip_partner_scope=True),
                )).all()
                for source in main_tariffs:
                    session.add(TariffCategory(
                        code=source.code,
                        label=source.label,
                        price_per_chat=source.price_per_chat,
                        sort_order=source.sort_order,
                        active=source.active,
                        blocked=source.blocked,
                        needs_review=source.needs_review,
                    ))

            if owner_id:
                owner = await session.scalar(select(User).where(User.telegram_id == owner_id))
                if owner is None:
                    session.add(User(
                        telegram_id=owner_id,
                        username=partner.owner_username,
                        full_name=partner.brand_title or partner.bot_username,
                    ))
                access = await session.scalar(
                    select(AdminAccess).where(AdminAccess.telegram_id == owner_id),
                )
                if access is None:
                    session.add(AdminAccess(
                        telegram_id=owner_id,
                        username=partner.owner_username,
                        role=AdminRole.admin,
                        partner_id=partner_id,
                    ))
                else:
                    access.role = AdminRole.admin
                    access.username = partner.owner_username or access.username
            await session.commit()
