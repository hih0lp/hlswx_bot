"""Общие фикстуры: чистая тестовая БД, фейковый бот, стартовый мир из групп и людей."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.seed import ensure_schema
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.entities import (
    Chat,
    City,
    Subscription,
    SubscriptionChat,
    SubscriptionStatus,
    User,
)
from app.services.whitelist import WHITELIST_CATEGORY

def now_utc() -> datetime:
    return datetime.now(UTC)


class FakeBot:
    """Заменяет Telegram: запоминает отправленное, умеет падать на выбранных чатах."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.pinned: list[tuple[int, int]] = []
        self.fail_chats: dict[int, str] = {}
        self._next_id = 1000

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_message(self, chat_id, text=None, **kwargs):
        if chat_id in self.fail_chats:
            raise RuntimeError(self.fail_chats[chat_id])
        message_id = self._new_id()
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "message_id": message_id,
                "reply_markup": kwargs.get("reply_markup"),
            },
        )
        return SimpleNamespace(message_id=message_id)

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        # Экраны-баннеры — это фото с подписью: для проверок важны текст
        # подписи и кнопки, поэтому kwargs пробрасываем как есть.
        return await self.send_message(chat_id, caption, **kwargs)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kwargs):
        if chat_id in self.fail_chats:
            raise RuntimeError(self.fail_chats[chat_id])
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return SimpleNamespace(message_id=message_id)

    async def edit_message_caption(self, chat_id=None, message_id=None, caption=None, **kwargs):
        return await self.edit_message_text(caption, chat_id=chat_id, message_id=message_id)

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned.append((chat_id, message_id))

    # --- помощники для проверок ---

    def messages_to(self, chat_id: int) -> list[dict]:
        return [m for m in self.sent if m["chat_id"] == chat_id]

    def user_notifications(self, telegram_id: int) -> list[str]:
        return [m["text"] or "" for m in self.sent if m["chat_id"] == telegram_id]


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema()
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_db(_schema):
    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest_asyncio.fixture
async def world():
    """Три группы одного города, подписчик, второй подписчик и пользователь из белого списка."""
    async with SessionLocal() as session:
        city = City(key="msk", label="Москва")
        session.add(city)
        await session.flush()

        chats = []
        for idx, (title, username, tg_id) in enumerate(
            [
                ("Группа A", "group_a", -100_000_001),
                ("Группа B", "group_b", -100_000_002),
                ("Группа C", "group_c", -100_000_003),
            ],
        ):
            chat = Chat(
                city_id=city.id,
                title=title,
                telegram_username=username,
                telegram_chat_id=tg_id,
                topic="Работа",
                network="hammer",
                sort_order=idx,
                active=True,
            )
            session.add(chat)
            chats.append(chat)
        await session.flush()

        users = {}
        for key, tg_id, uname in [
            ("sub1", 5001, "subscriber_one"),
            ("sub2", 5002, "subscriber_two"),
            ("wl", 5003, "whitelisted"),
        ]:
            user = User(telegram_id=tg_id, username=uname, full_name=key, balance=Decimal("0"))
            session.add(user)
            users[key] = user
        await session.flush()

        subs = {}
        for key, category in [("sub1", "SHABASHKA"), ("sub2", "SHABASHKA"), ("wl", WHITELIST_CATEGORY)]:
            sub = Subscription(
                user_id=users[key].id,
                category_code=category,
                approved_text=f"текст объявления {key}",
                approved_text_hash="hash",
                contact=f"@{users[key].username}",
                price_per_chat=500,
                total_price=1500,
                posts_volume="unlimited",
                status=SubscriptionStatus.active,
                starts_at=now_utc() - timedelta(days=1),
                expires_at=now_utc() + timedelta(days=30),
            )
            session.add(sub)
            await session.flush()
            for chat in chats:
                session.add(SubscriptionChat(subscription_id=sub.id, chat_id=chat.id))
            subs[key] = sub

        await session.commit()

        return SimpleNamespace(
            city_id=city.id,
            chat_ids=[c.id for c in chats],
            chat_tg_ids=[int(c.telegram_chat_id) for c in chats],
            user_ids={k: u.id for k, u in users.items()},
            user_tg_ids={k: u.telegram_id for k, u in users.items()},
            sub_ids={k: s.id for k, s in subs.items()},
        )


@pytest_asyncio.fixture
async def enqueue(world):
    """Поставить в очередь пост подписчика (sub1/sub2) или пользователя из белого списка (wl)."""
    from app.services.publish import enqueue_subscription_post

    async def _enqueue(who: str, text_body: str = "новое объявление", **kwargs) -> int:
        async with SessionLocal() as session:
            return await enqueue_subscription_post(
                session,
                world.sub_ids[who],
                text_body,
                f"@{who}",
                **kwargs,
            )

    return _enqueue


@pytest_asyncio.fixture
async def enqueue_oneshot(world):
    """Разовый пост, купленный у нас (пакет) — приоритет 1."""
    from app.models.entities import PackageOrder
    from app.services.publish import enqueue_package_order

    async def _enqueue(user_key: str = "sub2", text_body: str = "разовый пост") -> int:
        async with SessionLocal() as session:
            order = PackageOrder(
                user_id=world.user_ids[user_key],
                city_id=world.city_id,
                text=text_body,
                contact="@oneshot",
                price=3000,
                status="paid",
            )
            session.add(order)
            await session.commit()
            order_id = order.id
        return await enqueue_package_order(order_id)

    return _enqueue


async def jobs_for_chat(chat_id: int):
    from sqlalchemy import select

    from app.models.entities import PublishJob

    async with SessionLocal() as session:
        return list(
            (
                await session.scalars(
                    select(PublishJob).where(PublishJob.chat_id == chat_id).order_by(PublishJob.id),
                )
            ).all(),
        )


async def all_jobs():
    from sqlalchemy import select

    from app.models.entities import PublishJob

    async with SessionLocal() as session:
        return list((await session.scalars(select(PublishJob).order_by(PublishJob.id))).all())


async def slot_for(chat_id: int):
    from sqlalchemy import select

    from app.models.entities import ChatSlotState

    async with SessionLocal() as session:
        return await session.scalar(select(ChatSlotState).where(ChatSlotState.chat_id == chat_id))


async def publication(publication_id: int):
    from sqlalchemy import select

    from app.models.entities import Publication

    async with SessionLocal() as session:
        return await session.scalar(select(Publication).where(Publication.id == publication_id))
