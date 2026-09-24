"""Мелкие форматтеры для текстов интерфейса (склонения, даты)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone


# Время в панели показываем московское (заказчик 21.09.2026): сервер живёт в UTC,
# и «пауза до 14:28» выглядела временем в прошлом, когда на часах уже 17:28.
# Москва — круглый год UTC+3 (переходов на летнее время нет), поэтому обходимся
# без базы часовых поясов.
DISPLAY_TZ = timezone(timedelta(hours=3), "МСК")


def to_local(value: datetime | None) -> datetime | None:
    """Момент в московском времени; наивное значение считаем UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(DISPLAY_TZ)


def plural(count: int, one: str, few: str, many: str) -> str:
    tail_100 = count % 100
    tail_10 = count % 10
    if 11 <= tail_100 <= 14:
        return many
    if tail_10 == 1:
        return one
    if 2 <= tail_10 <= 4:
        return few
    return many


def chats_label(count: int) -> str:
    """«1 чат» / «2 чата» / «5 чатов» — как в макете."""
    return f"{count} {plural(count, 'чат', 'чата', 'чатов')}"


def minutes_label(count: int) -> str:
    """«1 минута» / «2 минуты» / «5 минут» — как в кадрах 341:1248 и 344:1284."""
    return f"{count} {plural(count, 'минута', 'минуты', 'минут')}"


def subscriptions_label(count: int) -> str:
    return f"{count} {plural(count, 'подписка', 'подписки', 'подписок')}"


def format_date(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


# Подпись кнопки меряется не в символах, а в знакоместах: значок занимает два,
# буква и стрелка — одно. Разница решающая: «⭐ Белый список» помещается,
# «➕ Добавить город» обрезается, хотя длина у них почти одинаковая.
_ARROWS = range(0x2190, 0x2200)

# Сколько знакомест держит кнопка в полный ряд на телефоне.
LABEL_LIMIT = 28


def label_width(text: str) -> int:
    width = 0
    for char in text:
        code = ord(char)
        if code == 0xFE0F:      # селектор эмодзи-начертания, своей ширины нет
            continue
        width += 2 if code >= 0x2300 and code not in _ARROWS else 1
    return width


def fit_label(text: str, limit: int = LABEL_LIMIT) -> str:
    """Обрезает подпись до `limit` знакомест, добавляя многоточие.

    Названия чатов и городов приходят из базы и бывают длиннее кнопки
    («Работа Санкт-Петербург | HAMMER 🔨»). Обрезать самим лучше, чем отдать
    это клиенту Telegram: он режет молча и в непредсказуемом месте.
    """
    if label_width(text) <= limit:
        return text
    out = ""
    for char in text:
        if label_width(out + char) > limit - 1:
            break
        out += char
    return out.rstrip() + "…"


def short_moment(value: datetime | None) -> str:
    """«06.09, 11:42» — формат строки списка (кадры 340:892 и 340:567).

    Год в списке не пишем: он там не помещался и всё равно одинаковый у всех
    строк; полную дату показывает карточка.
    """
    return f"{to_local(value):%d.%m, %H:%M}" if value else "—"


def until_label(value: datetime | None, *, now: datetime | None = None) -> str:
    """«через 2 мин» — как в кадрах 341:1112 и 340:863.

    Для очереди важно «сколько ждать», а не «когда именно»: пользователь
    сравнивает строки между собой, а не с часами.
    """
    if value is None:
        return "—"
    now = now or datetime.now(value.tzinfo)
    minutes = round((value - now).total_seconds() / 60)
    if minutes <= 0:
        return "сейчас"
    if minutes < 60:
        return f"через {minutes} мин"
    hours = minutes // 60
    return f"через {hours} {plural(hours, 'час', 'часа', 'часов')}"


def eta_label(value: datetime | None, *, now: datetime | None = None) -> str:
    """То же, что `until_label`, но с заглавной — для подписи кнопки.

    В макете строка списка очереди звучит «⏱ Следующее сообщение: через 2 мин»,
    но это 35 знакомест при 28 доступных. В списке очереди «следующее» и так
    подразумевается, поэтому в кнопке остаётся «⏱ Через 2 мин».
    """
    label = until_label(value, now=now)
    return label[:1].upper() + label[1:]


def moment_label(value: datetime | None, *, now: datetime | None = None) -> str:
    """«Сегодня, 20:42» или «05.09.2026, 18:42» — как в кадрах 349:434 и 344:1478.

    Сегодняшнее время подписано словом: на экране, который остаётся в чате,
    «Сегодня» читается быстрее даты, а вчерашняя запись всё равно получит
    полную дату и не притворится свежей.
    """
    if value is None:
        return "—"
    value = to_local(value)
    now = to_local(now or datetime.now(UTC))
    if value.date() == now.date():
        return f"Сегодня, {value:%H:%M}"
    return f"{value:%d.%m.%Y, %H:%M}"
