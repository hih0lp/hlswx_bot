from __future__ import annotations

import re

import pymorphy3

_MORPH = pymorphy3.MorphAnalyzer()
_URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.I)
_USERNAME_RE = re.compile(r"@\w+")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{6,}\d")
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002700-\U000027BF"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)
_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def _lemma(token: str) -> str:
    parsed = _MORPH.parse(token)
    if not parsed:
        return token
    return parsed[0].normal_form


def normalize_text(text: str) -> str:
    raw = (text or "").lower().replace("ё", "е")
    raw = _URL_RE.sub(" ", raw)
    raw = _USERNAME_RE.sub(" ", raw)
    raw = _PHONE_RE.sub(" ", raw)
    raw = _EMOJI_RE.sub(" ", raw)
    raw = _NON_WORD_RE.sub(" ", raw)
    raw = _SPACE_RE.sub(" ", raw).strip()
    tokens = []
    for token in raw.split():
        if len(token) < 2:
            continue
        if token.isdigit():
            tokens.append(token)
            continue
        tokens.append(_lemma(token))
    return " ".join(tokens)


def is_incomplete_text(text: str) -> bool:
    cleaned = (text or "").strip()
    if len(cleaned) < 25:
        return True
    alpha = sum(ch.isalpha() for ch in cleaned)
    if alpha < 12:
        return True
    words = re.findall(r"[а-яa-z]{3,}", cleaned.lower())
    return len(words) < 3


FRAUD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"аренд\w*\s+банковск", re.I),
    re.compile(r"банковск\w*\s+карт", re.I),
    re.compile(r"обнал", re.I),
    re.compile(r"крипт\w*\s+арбитраж", re.I),
    re.compile(r"дроп", re.I),
    re.compile(r"сим\s*карт", re.I),
    re.compile(r"перевод\w*\s+денег\s+чуж", re.I),
)

EXTERNAL_LINK_RE = re.compile(r"https?://(?!(?:t\.me|telegram\.me))", re.I)

# Почасовая оплата / смены → шабашки
SHABASHKA_HOURLY_RE = re.compile(
    r"(?:\d{2,4}\s*/\s*\d{1,2})|(?:\d{2,4}\s*руб\.?\s*/\s*ч)|(?:\d{2,4}\s*₽\s*/\s*ч)|"
    r"(?:\d{2,4}\s*руб(?:л(?:ей|я)?)?\s*(?:/|в)\s*час)|"
    r"почасов\w*|оплат\w*\s+в\s+час|руб\s*[/\\]\s*час|срочно\s+\d+\s+грузчик|"
    r"\d+\s*час(?:а|ов)?\s+оплат",
    re.I,
)

# Подсказки из разметки Excel (маркеры колонки B)
CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SHABASHKA", (
        "разгруз", "выгруз", "грузчик", "разнорабоч", "подработ", "шабаш", "подъем", "погруз", "груз ",
        "почасов", "руб/час", "руб/ч", "оплата в час", "срочно", "человек", "450/4", "500/4",
    )),
    ("BUY_SELL", ("куплю", "продам", "скупаю", "покупаю", "продаю", "продаж")),
    ("RENT", ("сдается", "сдаётся", "аренд", "сниму", "квартир", "комнат", "жиль")),
    ("VACANCY", ("вахт", "ваканс", "требуют", "зарплат", "оформлен", "монтажник", "сварщик", "водител")),
    ("REFERRAL", ("реферал", "партнер", "партнёр", "бизнес", "пассивн", "наставник", "сетев")),
    ("GOLD", ("золот", "драгмет", "драгоцен", "лом ", "серебр")),
    ("IP_OOO", (" ип", "ооо", "ип,", "регистрац", "расчетн", "р/с")),
    ("VERIFICATION", ("верификац", "верифик", "подтвержден аккаунт")),
    ("ONLINE_WORK", ("онлайн", "удален", "удалён", "из дома", "дистанц", "авито")),
)


def detect_rule_block(text: str) -> str | None:
    for pattern in FRAUD_PATTERNS:
        if pattern.search(text):
            return "REJECT"
    if EXTERNAL_LINK_RE.search(text):
        return "REJECT"
    return None


def detect_category_hint(text: str) -> str | None:
    """Ключевые слова из обучающего файла — подстраховка при низкой уверенности ML."""
    if SHABASHKA_HOURLY_RE.search(text or ""):
        return "SHABASHKA"
    lowered = (text or "").lower().replace("ё", "е")
    normalized = normalize_text(text)
    haystack = f"{lowered} {normalized}"
    scores: dict[str, int] = {}
    for code, patterns in CATEGORY_HINTS:
        score = sum(1 for p in patterns if p in haystack)
        if score:
            scores[code] = score
    if not scores:
        return None
    best_code, best_score = max(scores.items(), key=lambda x: x[1])
    if best_score < 1:
        return None
    return best_code
