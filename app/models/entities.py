import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    succeeded = "succeeded"
    canceled = "canceled"
    failed = "failed"


class SubscriptionStatus(str, enum.Enum):
    pending_payment = "pending_payment"
    active = "active"
    expired = "expired"
    blocked = "blocked"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Последнее обращение к боту — строка «Последняя активность» в карточке
    # пользователя (кадр 349:967). Заполняется с момента появления колонки,
    # у давних пользователей пустая, пока они снова не напишут боту.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    saved_contacts: Mapped[list["SavedContact"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class SavedContact(Base):
    __tablename__ = "saved_contacts"
    __table_args__ = (UniqueConstraint("user_id", "contact", name="uq_user_contact"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    contact: Mapped[str] = mapped_column(String(255))
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="saved_contacts")


class City(Base):
    __tablename__ = "cities"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    telegram_username: Mapped[str] = mapped_column(String(100))
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    topic: Mapped[str] = mapped_column(String(100), default="Работа")
    network: Mapped[str] = mapped_column(String(20), default="hammer", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    city: Mapped["City"] = relationship()


class TrainingSample(Base):
    __tablename__ = "training_dataset"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    target_category: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClassificationLog(Base):
    __tablename__ = "classification_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    predicted_category: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Numeric(6, 4))
    final_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    admin_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    category_code: Mapped[str] = mapped_column(String(32))
    approved_text: Mapped[str] = mapped_column(Text)
    approved_text_hash: Mapped[str] = mapped_column(String(64))
    contact: Mapped[str] = mapped_column(String(255))
    price_per_chat: Mapped[int] = mapped_column(Integer)
    total_price: Mapped[int] = mapped_column(Integer)
    posts_volume: Mapped[str] = mapped_column(String(16), default="low")
    plan_type: Mapped[str] = mapped_column(String(20), default="standard")
    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.pending_payment)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Отметки фоновых уведомлений об окончании подписки: напоминание за сутки
    # и само сообщение «Подписка закончилась». Нужны, чтобы не отправить дважды.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chats: Mapped[list["SubscriptionChat"]] = relationship(back_populates="subscription", cascade="all, delete-orphan")


class SubscriptionChat(Base):
    __tablename__ = "subscription_chats"
    __table_args__ = (UniqueConstraint("subscription_id", "chat_id", name="uq_sub_chat"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"))

    subscription: Mapped["Subscription"] = relationship(back_populates="chats")
    chat: Mapped["Chat"] = relationship()


class PackageOrder(Base):
    __tablename__ = "package_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"))
    text: Mapped[str] = mapped_column(Text)
    contact: Mapped[str] = mapped_column(String(255))
    price: Mapped[int] = mapped_column(Integer)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="pending_payment")
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    purpose: Mapped[str] = mapped_column(String(32))
    purpose_id: Mapped[int] = mapped_column(Integer)
    invoice_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.pending)
    confirmation_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(20), default="yookassa")
    # Задел под рекуррентные списания ЮKassa (ТЗ 6.1): сам автоплатёж вне этапа 1
    payment_method_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    save_payment_method: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Publication(Base):
    """Пост пользователя целиком: одна публикация → несколько задач по группам."""

    __tablename__ = "publications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="subscription", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    package_order_id: Mapped[int | None] = mapped_column(ForeignKey("package_orders.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    contact: Mapped[str] = mapped_column(String(255), default="")
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notified_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PublishJob(Base):
    __tablename__ = "publish_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int | None] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    package_order_id: Mapped[int | None] = mapped_column(ForeignKey("package_orders.id"), nullable=True)
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"))
    text: Mapped[str] = mapped_column(Text)
    contact: Mapped[str] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    delivery: Mapped[str | None] = mapped_column(String(10), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Отметка времени провала нужна дашборду: он показывает ошибки за сегодня,
    # а не за всё время (иначе счётчик копится мёртвым грузом — правка 17.09.2026).
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class SubscriptionPublicationLog(Base):
    __tablename__ = "subscription_publication_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WhitelabelApplicationStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class WhitelabelApplication(Base):
    __tablename__ = "whitelabel_applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    brand_title: Mapped[str] = mapped_column(String(128))
    planned_bot_username: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[WhitelabelApplicationStatus] = mapped_column(
        Enum(WhitelabelApplicationStatus),
        default=WhitelabelApplicationStatus.pending,
        index=True,
    )
    admin_note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WhitelabelPartner(Base):
    __tablename__ = "whitelabel_partners"
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_username: Mapped[str | None] = mapped_column(String(64))
    api_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand_title: Mapped[str | None] = mapped_column(String(128))
    linked_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WhitelistEntry(Base):
    __tablename__ = "whitelist_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    city_keys: Mapped[str] = mapped_column(Text, default="[]")
    chat_ids: Mapped[str] = mapped_column(Text, default="[]")
    # Срок действия записи (ТЗ этапа 2, 6.3): NULL — «навсегда».
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class B2bIntegration(Base):
    __tablename__ = "b2b_integrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatSlotState(Base):
    """Состояние слота группы: тишина после разового поста + последняя публикация."""

    __tablename__ = "chat_publish_locks"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"), unique=True, index=True)
    # конец 20-минутной тишины; в прошлом — группа свободна
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_author_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


# Совместимость со старым именем
ChatPublishLock = ChatSlotState


class OneshotEvent(Base):
    """Факт выхода разового поста в группе — приходит от Hammer/W, нужен для идемпотентности."""

    __tablename__ = "oneshot_events"
    __table_args__ = (
        UniqueConstraint("network", "telegram_chat_id", "message_id", name="uq_oneshot_event"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    network: Mapped[str] = mapped_column(String(20), default="hammer")
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id"), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    silence_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublishEvent(Base):
    """Журнал очереди: публикации, ошибки, срабатывания приоритетов (ТЗ 6.1)."""

    __tablename__ = "publish_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(20), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------
# Этап 2 — административная панель
# --------------------------------------------------------------------------


class AdminRole(str, enum.Enum):
    """Роли доступа к панели (ТЗ этапа 2, 6.11 и 7).

    Администратору доступны все разделы, менеджеру — все, кроме «Управления».
    """

    admin = "admin"
    manager = "manager"


class AdminAccess(Base):
    """Выданный доступ к админ-панели.

    Владельцы из ADMIN_IDS / OWNER_IDS в таблицу не попадают и остаются
    администраторами всегда — иначе панель можно потерять целиком, отозвав
    у себя последнюю роль.
    """

    __tablename__ = "admin_access"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole), default=AdminRole.manager)
    granted_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    """Настройки, которые администратор меняет из панели (ТЗ 6.10, 7).

    Значения из .env остаются дефолтами: строка появляется здесь только
    после того, как настройку меняли руками.
    """

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class TariffCategory(Base):
    """Справочник тарифов публикации по категориям объявлений (ТЗ 6.6).

    `code` — то же, чем размечена обучающая выборка и на что ссылаются уже
    оплаченные подписки, поэтому он неизменен. Переименование тарифа меняет
    только `label`.
    """

    __tablename__ = "tariff_categories"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(100))
    price_per_chat: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AdminAuditLog(Base):
    """Действия администратора и менеджера — для последующей диагностики (ТЗ 7).

    Существующий PublishEvent для этого не годится: у него нет автора.
    """

    __tablename__ = "admin_audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
