"""Определение сети чата (Hammer / W / package) для маршрутизации публикаций."""

from __future__ import annotations

# Подписи сетей в интерфейсе — как в макете: «Работа Москва | HAMMER 🔨».
NETWORK_LABELS = {
    "hammer": "HAMMER 🔨",
    "w": "W",
}


def chat_display_title(chat) -> str:
    """Название чата для пользователя: «Вакансии Москва | W»."""
    title = (chat.title or chat.telegram_username or "").strip()
    suffix = NETWORK_LABELS.get((chat.network or "").lower())
    return f"{title} | {suffix}" if suffix else title


# Корни, после которых финальная «w» в username означает сеть W. «rabot», а не
# «rabota»: в перечне заказчика половина групп называется «Podrabotka…», где
# корень обрывается на «rabotk».
_W_ROOTS = ("rabot", "job", "vacanc")

# Группы, по имени которых сеть не читается, — заказчик называет её сам.
# «Вакансии Казань» отнесена к W ответом от 15.09.2026.
_NETWORK_BY_USERNAME = {
    "vacanciekzn": "w",
}


def infer_chat_network(username: str) -> str:
    """hammer | w | package — по username из seed HWLS / Hammer."""
    u = (username or "").strip().lstrip("@").lower()
    if not u:
        return "package"
    known = _NETWORK_BY_USERNAME.get(u)
    if known:
        return known
    if "hammer" in u:
        return "hammer"
    if "_w" in u or (u.endswith("w") and any(root in u for root in _W_ROOTS)):
        return "w"
    return "package"
