from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TariffCategory:
    code: str
    label: str
    price_per_chat: int
    blocked: bool = False
    needs_review: bool = False


PRICE_TO_CATEGORY: dict[int, str] = {
    500: "SHABASHKA",
    750: "BUY_SELL",
    1000: "RENT",
    1200: "VACANCY",
    1500: "REFERRAL",
    1800: "GOLD",
    2500: "IP_OOO",
    3000: "VERIFICATION",
    3500: "ONLINE_WORK",
}

# Порядок ключей — это порядок категорий в справочнике ТЗ 6.6: из него
# `_seed_tariffs` берёт `sort_order`, а панель и экран правил выводят список
# ровно в этом порядке. Названия тоже дословно из ТЗ; менять их в обход
# «Переименовать тариф» нельзя без поднятия версии справочника (см. seed).
CATEGORIES: dict[str, TariffCategory] = {
    "SHABASHKA": TariffCategory("SHABASHKA", "Шабашки", 500),
    "VACANCY": TariffCategory("VACANCY", "Вакансии", 1200),
    "RENT": TariffCategory("RENT", "Аренда", 1000),
    "BUY_SELL": TariffCategory("BUY_SELL", "Купля / продажа", 750),
    "GOLD": TariffCategory("GOLD", "Драгметаллы", 1800),
    "IP_OOO": TariffCategory("IP_OOO", "ИП / ООО", 2500),
    "REFERRAL": TariffCategory("REFERRAL", "Рефералки", 1500),
    "OTHER": TariffCategory("OTHER", "Другое", 3500, needs_review=True),
    # Ниже — то, чего в справочнике ТЗ нет. Две категории скрыты из списков
    # выбора (см. tariffs.INACTIVE_BY_DEFAULT), две служебные — не тарифы.
    "VERIFICATION": TariffCategory("VERIFICATION", "Верификация", 3000),
    "ONLINE_WORK": TariffCategory("ONLINE_WORK", "Работа онлайн", 3500),
    "INCOMPLETE": TariffCategory("INCOMPLETE", "Недостаточно информации", 0),
    "REJECT": TariffCategory("REJECT", "Запрещённый контент", 0, blocked=True),
}

# Значки категорий из кадров макета 375:454 и 375:510. Скрытые и служебные
# коды значка не имеют — они в списке тарифов не показываются.
CATEGORY_ICONS: dict[str, str] = {
    "SHABASHKA": "💼",
    "VACANCY": "📋",
    "RENT": "🏠",
    "BUY_SELL": "🛒",
    "GOLD": "💎",
    "IP_OOO": "🏢",
    "REFERRAL": "🔗",
    "OTHER": "📌",
}


def category_icon(code: str) -> str:
    return CATEGORY_ICONS.get(code, "🏷")


PACKAGE_TARIFFS: dict[str, tuple[tuple[int, bool, str], ...]] = {
    "msk": ((1600, False, "Пост во все чаты"), (2500, True, "Пост + закреп")),
    "spb": ((500, False, "Пост во все чаты"), (800, True, "Пост + закреп")),
    "nn": ((300, False, "Пост во все чаты"), (500, True, "Пост + закреп")),
    "smr": ((250, False, "Пост во все чаты"), (450, True, "Пост + закреп")),
}

# Пакеты только в этих городах (как в hammer-bot)
PACKAGE_CITY_KEYS = frozenset(PACKAGE_TARIFFS.keys())

CITY_CHOICES: tuple[tuple[str, str], ...] = (
    ("msk", "Москва"),
    ("spb", "Санкт-Петербург"),
    ("nn", "Нижний Новгород"),
    ("smr", "Самара"),
    ("yar", "Ярославль"),
    ("nsk", "Новосибирск"),
    # Добавлены по присланному перечню групп от 14.09.2026.
    ("ekb", "Екатеринбург"),
    ("kzn", "Казань"),
    ("kld", "Калининград"),
    ("chel", "Челябинск"),
)


def category_from_price(price: float | int) -> str:
    return PRICE_TO_CATEGORY.get(int(price), "OTHER")


def format_package_tariffs() -> str:
    city_labels = dict(CITY_CHOICES)
    lines = ["<b>📦 Тарифы пакетов</b> <i>(разово, все чаты города)</i>"]
    for key in ("msk", "spb", "nn", "smr"):
        label = city_labels.get(key, key)
        tariffs = PACKAGE_TARIFFS[key]
        post = next(p for p, pin, _ in tariffs if not pin)
        pin = next(p for p, pinned, _ in tariffs if pinned)
        lines.append(f"• {label}: пост <b>{post:,} ₽</b> · закреп <b>{pin:,} ₽</b>")
    return "\n".join(lines)


def get_category(code: str) -> TariffCategory:
    return CATEGORIES.get(code, CATEGORIES["OTHER"])
