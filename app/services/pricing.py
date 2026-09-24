"""Доплаты за объём публикаций в день."""

from __future__ import annotations

POSTS_VOLUME_LOW = "low"
POSTS_VOLUME_MID = "mid"
POSTS_VOLUME_UNLIMITED = "unlimited"

PLAN_STANDARD = "standard"
PLAN_CORP_B2B = "corp_b2b"
CORP_B2B_CATEGORY = "CORP_B2B"

# Полные формулировки — из макета Figma (фрейм «Количество публикаций»).
# Идут в тексты экранов и уведомлений: «📍 Москва · 💬 3 чата · 📊 До 3
# публикаций в день».
#
# «в день», а не «в сутки», — по решению заказчика от 14.09.2026. Тогда же
# третий вариант перестал быть безлимитным: лимит 30 публикаций в день
# действует для всех, см. `volume_limits.daily_limit_for_volume`.
POSTS_VOLUME_LABELS: dict[str, str] = {
    POSTS_VOLUME_LOW: "До 3 публикаций в день",
    # «От 4 до 10» с ценой не влезало в кнопку и обрезалось — оставлен верхний предел.
    POSTS_VOLUME_MID: "До 10 публикаций в день",
    POSTS_VOLUME_UNLIMITED: "До 30 публикаций в день",
}

# Короткие — основа подписи кнопки (ТЗ 6.8, критерий приёмки 8.8).
POSTS_VOLUME_SHORT: dict[str, str] = {
    POSTS_VOLUME_LOW: "До 3 в день",
    POSTS_VOLUME_MID: "До 10 в день",
    POSTS_VOLUME_UNLIMITED: "До 30 в день",
}


def posts_volume_button_label(volume: str, price_per_chat: int | None) -> str:
    """Подпись кнопки выбора объёма: «До 3 в день — 3 500 ₽/чат».

    Заказчик просит держать объём и цену в одной подписи, как в макете.
    Буквальная формулировка макета («До 3 публикаций в день — 3 500 ₽ / чат»)
    даёт 38 знаков при ёмкости полноширинной кнопки в 24–28 и обрезается
    многоточием, поэтому объём берётся короткий, а пробелы вокруг слеша
    убраны: так подпись укладывается в 25–26 знаков и видна целиком.

    В корпоративном тарифе цена от объёма не зависит и приходит пустой —
    тогда в кнопке остаётся один объём.
    """
    short = POSTS_VOLUME_SHORT.get(volume, volume)
    if price_per_chat is None:
        return short
    return f"{short} — {price_per_chat:,} ₽/чат".replace(",", " ")


def is_light_category(category_code: str) -> bool:
    return category_code in {"SHABASHKA", "VACANCY"}


def posts_volume_surcharge_per_chat(category_code: str, volume: str) -> int:
    if volume == POSTS_VOLUME_LOW:
        return 0
    light = is_light_category(category_code)
    if volume == POSTS_VOLUME_MID:
        return 200 if light else 500
    if volume == POSTS_VOLUME_UNLIMITED:
        return 300 if light else 1000
    return 0


def apply_posts_volume(base_price_per_chat: int, category_code: str, volume: str) -> int:
    return base_price_per_chat + posts_volume_surcharge_per_chat(category_code, volume)


def corp_b2b_price_per_chat() -> int:
    from app.config import get_settings

    return get_settings().corp_b2b_price_per_chat
