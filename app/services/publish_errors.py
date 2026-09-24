"""Классификация ошибок публикации (ТЗ 6.7).

Исходов у отправки три, а не два. Ошибки прав и доступа не лечатся повтором —
по ним сразу отменяем публикацию в эту группу и зовём администратора. Сетевые и
временные — ретраим. Отдельно стоит третий случай: запрос ушёл, а ответа мы не
дождались. Пост при этом чаще всего уже в группе, и повтор кладёт туда дубль,
поэтому такие исходы не ретраятся и не считаются провалом.
"""

from __future__ import annotations

# Признаки безнадёжных ошибок: бот забанен, кикнут, нет прав, чата нет
_PERMANENT_MARKERS = (
    "bot was blocked",
    "bot was kicked",
    "bot is not a member",
    "chat not found",
    "chat_not_configured",
    "peer_id_invalid",
    "user is deactivated",
    "not enough rights",
    "have no rights",
    "need administrator rights",
    "chat_admin_required",
    "chat_write_forbidden",
    "topic_closed",
    "group chat was upgraded",
    "forbidden:",
    "unauthorized",
)

# Признаки оборванного ответа: отправка ушла, результат неизвестен.
#
# «request timeout error» — это aiogram: так выглядит его TelegramNetworkError
# и у нас, и на стороне релея, откуда текст приезжает в поле error. Разрыв уже
# после отправки запроса Telegram почти всегда означает, что сообщение в группе:
# сам вызов дошёл, не дошёл ответ о нём.
#
# Обрыва соединения здесь нет намеренно: он случается и до того, как запрос
# приняли, поэтому «connection reset» остаётся обычной сетевой ошибкой с повтором.
_UNCERTAIN_MARKERS = (
    "relay_timeout",
    "request timeout error",
    "read timeout",
    "readtimeout",
    "readerror",
    "remoteprotocolerror",
    "server disconnected",
    "http_502",
    "http_504",
)

_HUMAN_REASONS = {
    "queue_timeout": "пост ждал в очереди дольше допустимого срока и был отменён",
    "chat_not_configured": "у чата не заполнен telegram_chat_id",
    "bot was kicked": "бот удалён из группы",
    "bot was blocked": "бот заблокирован",
    # Раньше здесь было «группа не найдена или удалена», и администратор шёл
    # искать пропавшую группу. Telegram отвечает так же, когда группа на месте,
    # а бот-публикатор в ней не состоит, — на деле это и происходило.
    "chat not found": "бот не добавлен в группу (или группа удалена)",
    "not enough rights": "у бота нет прав на публикацию",
    "have no rights": "у бота нет прав на публикацию",
    "need administrator rights": "боту нужны права администратора",
    "chat_admin_required": "боту нужны права администратора",
    "chat_write_forbidden": "в группе запрещена отправка сообщений",
    "relay_disabled": "релей публикации выключен",
    "relay_secret_not_configured": "не задан секрет релея",
    "relay_unreachable": "релей недоступен",
    "relay_timeout": "группа не ответила вовремя",
}

UNCERTAIN_REASON = "ответ от Telegram не пришёл — пост, скорее всего, уже в группе"

# Статус задачи с неизвестным исходом. Не «failed»: пост, вероятнее всего, вышел,
# и считать публикацию провальной из-за оборванного ответа нельзя.
STATUS_UNCONFIRMED = "unconfirmed"


def is_uncertain(error: str) -> bool:
    """True — связь оборвалась после отправки, исход неизвестен.

    Повторять такую задачу нельзя: если пост всё-таки вышел, повтор положит в
    группу второй такой же.
    """
    low = (error or "").lower()
    return any(marker in low for marker in _UNCERTAIN_MARKERS)


def is_permanent(error: str) -> bool:
    """True — повторять бессмысленно, нужна реакция администратора."""
    low = (error or "").lower()
    if is_uncertain(low):
        return False
    return any(marker in low for marker in _PERMANENT_MARKERS)


def human_reason(error: str) -> str:
    """Короткое объяснение для админа вместо сырого текста ошибки."""
    low = (error or "").lower()
    if is_uncertain(low):
        return UNCERTAIN_REASON
    for marker, reason in _HUMAN_REASONS.items():
        if marker in low:
            return reason
    return (error or "неизвестная ошибка")[:200]
