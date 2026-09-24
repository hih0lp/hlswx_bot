from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_ids: str = ""
    owner_ids: str = ""

    postgres_user: str = "hwls"
    postgres_password: str = "hwls"
    postgres_db: str = "hwls"
    database_url: str = "postgresql+asyncpg://hwls:hwls@db:5432/hwls"

    redis_url: str = "redis://redis:6379/0"
    public_url: str = "http://127.0.0.1:8082"
    webhook_port: int = 8082

    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_return_url: str = "http://127.0.0.1:8082/success"
    yookassa_webhook_path: str = "/payments/yookassa/webhook"

    stars_rub_rate: float = 2.0  # 1 ⭐ ≈ N ₽ при пополнении
    ml_api_key: str = ""  # опциональный ключ для внешних WL-ботов
    min_topup_amount: int = 100
    corp_b2b_price_per_chat: int = 3500
    ml_confidence_threshold: float = 0.85
    ml_model_path: str = "models/tariff_classifier.joblib"
    training_data_path: str = "data/training_dataset.xlsx"

    fraud_similarity_threshold: float = 0.90
    # Сверка нового объявления с согласованным при оформлении подписки.
    # Выключена по решению заказчика (17.09.2026): у кого подписка оплачена,
    # объявление засчитывается автоматически. Порог и сама проверка оставлены —
    # включаются переменной окружения без правки кода.
    fraud_check_enabled: bool = False
    queue_subscription_interval_sec: int = 120  # legacy, оставлен для совместимости .env
    queue_package_lock_minutes: int = 20  # legacy-имя тишины, см. queue_silence_minutes

    # Очередь публикаций (ТЗ 6.2–6.5)
    queue_silence_minutes: int = 20  # тишина группы после разового поста
    queue_user_pause_sec: int = 120  # пауза между постами РАЗНЫХ пользователей в группе
    queue_job_ttl_minutes: int = 120  # сколько задача ждёт в очереди до отмены
    direct_publish_fallback: bool = True  # публиковать своим ботом, если релей недоступен
    edit_skips_fraud_check: bool = True  # правка через кнопку «Редактировать» минует антифрод

    # Публикация в группы через Hammer / W Pay (не от имени HLSWX)
    hammer_relay_url: str = "http://172.20.0.1:8000"
    hwls_relay_secret: str = ""
    hammer_relay_enabled: bool = True
    # Ручки /api/hwls/edit и /api/hwls/close ждут реализации на стороне Hammer/W
    hammer_relay_edit_enabled: bool = False

    @property
    def admin_ids_set(self) -> set[int]:
        raw = f"{self.admin_ids},{self.owner_ids}"
        return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
