from collections.abc import AsyncGenerator

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, with_loader_criteria

from app.config import get_settings
from app.models.entities import TenantScoped
from app.services.tenant import current_partner_id

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(Session, "do_orm_execute")
def _apply_partner_scope(execute_state) -> None:
    if execute_state.execution_options.get("skip_partner_scope"):
        return
    partner_id = current_partner_id()
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda model: model.partner_id == partner_id,
            include_aliases=True,
        ),
    )


@event.listens_for(Session, "before_attach")
def _assign_partner_scope_on_attach(session, instance) -> None:
    """Bind new tenant rows when they enter the session, not at a later flush.

    A flush may happen after an async flow switches tenant scopes; assigning at
    flush time could silently move previously-added rows into the next tenant.
    """
    if isinstance(instance, TenantScoped) and inspect(instance).transient:
        instance.partner_id = current_partner_id()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
