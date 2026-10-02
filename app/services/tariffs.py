"""Справочник тарифов публикации (ТЗ этапа 2, раздел 6.6).

До этапа 2 категории и цены лежали в `app/ml/categories.py` и правились
только правкой кода — причём цены были продублированы ещё в двух местах.
Теперь справочник живёт в БД и редактируется из панели, а `CATEGORIES`
остаётся первичным наполнением и пространством меток для классификатора.

`code` неизменен: им размечена обучающая выборка и на него ссылаются уже
оплаченные подписки. «Переименовать тариф» меняет только `label`.

Цены нужны и синхронному коду (классификатор считает стоимость прямо в
`ClassificationResult`), поэтому таблица целиком лежит в памяти процесса —
её десяток строк, меняется она редко. Кэш сбрасывается при каждой правке.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from app.db.session import SessionLocal
from app.ml.categories import CATEGORIES
from app.models.entities import TariffCategory as TariffRow
from app.services.tenant import current_partner_id

logger = logging.getLogger("tariffs")

CACHE_TTL_SEC = 30

# Категории, которых нет в справочнике заказчика (ТЗ 6.6): в панели и в списке
# тарифов не показываются, но остаются разрешимыми — на них могут ссылаться
# старые подписки и предсказания модели. Цена у обеих совпадает с «Прочим».
INACTIVE_BY_DEFAULT = frozenset({"VERIFICATION", "ONLINE_WORK"})

# Служебные коды: не тарифы, в списке категорий не показываются.
SERVICE_CODES = frozenset({"INCOMPLETE", "REJECT"})

FALLBACK_CODE = "OTHER"


@dataclass(frozen=True)
class Tariff:
    code: str
    label: str
    price_per_chat: int
    blocked: bool = False
    needs_review: bool = False
    active: bool = True
    sort_order: int = 0


_cache: dict[tuple[int, str], Tariff] = {}
_loaded_at: float = 0.0


def _from_row(row: TariffRow) -> Tariff:
    return Tariff(
        code=row.code,
        label=row.label,
        price_per_chat=row.price_per_chat,
        blocked=row.blocked,
        needs_review=row.needs_review,
        active=row.active,
        sort_order=row.sort_order,
    )


def _from_code_defaults(code: str) -> Tariff:
    """Значение из `app/ml/categories.py` — пока таблица не прочитана."""
    cat = CATEGORIES.get(code) or CATEGORIES[FALLBACK_CODE]
    return Tariff(
        code=cat.code,
        label=cat.label,
        price_per_chat=cat.price_per_chat,
        blocked=cat.blocked,
        needs_review=cat.needs_review,
        active=cat.code not in INACTIVE_BY_DEFAULT,
    )


async def refresh() -> None:
    global _cache, _loaded_at
    async with SessionLocal() as session:
        rows = (await session.scalars(
            select(TariffRow).execution_options(skip_partner_scope=True),
        )).all()
    _cache = {(row.partner_id, row.code): _from_row(row) for row in rows}
    _loaded_at = time.monotonic()


async def ensure_loaded() -> None:
    if not _loaded_at or time.monotonic() - _loaded_at > CACHE_TTL_SEC:
        try:
            await refresh()
        except Exception:
            logger.exception("Не удалось прочитать тарифы, остаёмся на значениях из кода")


def get(code: str) -> Tariff:
    """Тариф по коду. Синхронный — его зовёт классификатор.

    Неизвестный код разрешается в «Прочее», как и раньше. Неактивные
    категории тоже возвращаются: на них ссылаются старые подписки, и
    прятать их надо только из списков выбора.
    """
    if _cache:
        scope = current_partner_id()
        return _cache.get((scope, code)) or _cache.get((scope, FALLBACK_CODE)) or _from_code_defaults(code)
    return _from_code_defaults(code)


def all_tariffs(*, only_active: bool = True) -> list[Tariff]:
    """Тарифы для экранов выбора — без служебных кодов."""
    scope = current_partner_id()
    source = (
        [value for (partner_id, _), value in _cache.items() if partner_id == scope]
        if _cache else [_from_code_defaults(c) for c in CATEGORIES]
    )
    if not source:
        source = [_from_code_defaults(c) for c in CATEGORIES]
    items = [t for t in source if t.code not in SERVICE_CODES]
    if only_active:
        items = [t for t in items if t.active]
    return sorted(items, key=lambda t: (t.sort_order, t.price_per_chat, t.code))


async def set_price(code: str, price: int, *, by_telegram_id: int | None = None) -> Tariff:
    """«Изменить тариф». Уже оплаченные подписки не пересчитываются:
    цена снимается в момент покупки и лежит в самой подписке."""
    if price < 0:
        raise ValueError("Стоимость не может быть отрицательной")
    return await _update(code, by_telegram_id=by_telegram_id, price_per_chat=int(price))


async def set_active(code: str, active: bool, *, by_telegram_id: int | None = None) -> Tariff:
    return await _update(code, by_telegram_id=by_telegram_id, active=bool(active))


async def _update(code: str, *, by_telegram_id: int | None, **fields) -> Tariff:
    async with SessionLocal() as session:
        row = await session.scalar(select(TariffRow).where(TariffRow.code == code))
        if row is None:
            raise KeyError(f"нет тарифа {code!r}")
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_by_telegram_id = by_telegram_id
        await session.commit()
    await refresh()
    return get(code)


async def format_subscription_tariffs() -> str:
    """Список тарифов для экрана правил и раздела «Тарифы»."""
    await ensure_loaded()
    lines = ["<b>📅 Тарифы подписки</b> <i>(за 1 чат / 30 дней)</i>"]
    for tariff in all_tariffs():
        price = f"{tariff.price_per_chat:,}".replace(",", " ")
        lines.append(f"• {tariff.label} — <b>{price} ₽</b>")
    return "\n".join(lines)
