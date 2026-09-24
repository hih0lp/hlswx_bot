"""Экраны панели против кадров макета — построчно, вместе с пустыми строками.

Единственный тест, который сверяется с макетом программно: остальные держат
ожидаемый текст руками и расходятся с Figma молча. Сверка идёт по дампу
`docs/design/figma.json` — он в .gitignore (≈3 МБ), поэтому без него тест
пропускается, а не падает.

Пары «кадр → константа» не ведём отдельно: номера кадров уже стоят
комментариями над константами в `app/core/admin_texts.py`, оттуда их и берёт
`scripts/figma_screens.py`. Значит таблица не протухнет — она и есть
комментарии.

Сверяется **скелет** экрана: где текст, где пустая строка, сколько строк.
Слова не сверяем — в боте на их месте подстановки, а образцы данных в макете
всё равно свои.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DUMP = ROOT / "docs" / "design" / "figma.json"
sys.path.insert(0, str(ROOT / "scripts"))

pytestmark = pytest.mark.skipif(
    not DUMP.exists(),
    reason="нет docs/design/figma.json — выгрузите дамп макета (scripts/figma_fetch_images.py)",
)

# Осознанные отступления: кадр и константа расходятся, и это правильно.
# Список — не свалка, а запись решений; тест ниже следит, чтобы он не оброс
# лишним и чтобы устаревшие записи из него убирали.
KNOWN = {
    # --- константа описывает кусок экрана, а не весь кадр ---
    ("PANEL_ATTENTION", "337:233"): "блок «Требует внимания», а кадр — весь экран",
    ("PAYMENTS_PROCESSING", "340:353"): "{lines} разворачивается в список платежей",
    ("PAYMENTS_AWAITING", "340:385"): "{lines} разворачивается в список подписок",
    ("PAYMENTS_ALL", "340:567"): "{lines} разворачивается в список платежей",
    ("PUBS_LIST_SUMMARY", "340:751"): "общий кусок трёх списков, кадр — весь экран",
    ("PUBS_LIST_SUMMARY", "340:892"): "общий кусок трёх списков, кадр — весь экран",
    ("PUBS_LIST_SUMMARY", "340:1024"): "общий кусок трёх списков, кадр — весь экран",
    ("PUB_CARD", "340:863"): "{status_block} разворачивается в три строки",
    ("PUB_CARD", "340:982"): "{status_block} разворачивается в две строки",
    ("PUB_CARD", "340:1079"): "{status_block} разворачивается в две строки",
    ("TARIFFS", "375:454"): "{lines} разворачивается в восемь тарифов",
    ("TARIFFS", "375:618"): "{lines} разворачивается в тариф и приглашение к вводу",
    ("TARIFFS", "375:674"): "{lines} разворачивается в «было → стало»",
    # --- номер кадра в комментарии указывает на соседний экран ---
    ("PUB_EDIT_ASK", "340:982"): "кадр — карточка публикации, правка открывается кнопкой с неё",
    ("WL_PICK_CITIES", "338:537"): "кадр — шаг выбора чатов, городам отвечает 338:501",
    # --- бот показывает больше, чем нарисовано, и это решено оставить ---
    ("CITY_CARD", "368:506"): "строка «Статус»: тумблера города в макете нет, отключать чем-то надо",
    ("WL_ADD_ASK", "338:475"): "подсказка про /start: без неё неясно, почему запись не сработала",
    ("PARTNER_INTEGRATION", "344:1478"): "строка «Режим»: показывает, есть ли у партнёра токен бота",
    ("CATEGORY_PICK", "349:381"): "у кадра только заголовок, номер объявления бот дописывает сам",
    # --- один и тот же экран нарисован в макете дважды ---
    # Карточка записи нарисована и в `338:661`, и в `349:1219`. Во втором есть
    # заголовок «⭐ Белый список» и подписи строк («🌍 Города · Все города»), в
    # первом охват идёт подряд и без подписей. До 20.09.2026 бот следовал
    # `349:1219` как более полному; заказчик прислал скрин `338:661` — теперь
    # точное совпадение там, а исключение переехало на `349:1219`.
    ("WL_CARD", "349:1219"): "второй рисунок той же карточки; бот следует 338:661",
}

# До 18.09.2026 здесь стояли ещё три записи: «PANEL / 337:208» и
# «WHITELIST / 338:453» (в кадрах не хватало строк «⏱ В очереди» и
# «🚫 Истекших») и «CATEGORIES_REVIEW / 349:354» (не было счётчиков выборки).
# Плагин их дорисовал, расхождения исчезли — тест ниже об этом и сообщил.


def _pairs():
    import app.core.admin_texts as admin_texts
    from figma_read import frames_of
    from figma_screens import bot_lines, design_lines, mapping, shape

    frames = {f["id"]: f for f in frames_of(json.loads(DUMP.read_text()))}
    out = []
    for ids, name in mapping(str(ROOT / "app" / "core" / "admin_texts.py")):
        value = getattr(admin_texts, name, None)
        if not isinstance(value, str) or "\n" not in value:
            continue                       # подписи кнопок сверять нечего
        for frame_id in ids:
            frame = frames.get(frame_id)
            if frame is None:
                continue
            out.append((name, frame_id, shape(design_lines(frame)), shape(bot_lines(value))))
    return out


PAIRS = _pairs() if DUMP.exists() else []


def test_mapping_is_not_empty():
    """Страховка: если разбор комментариев сломается, тест ниже станет пустым."""
    assert len(PAIRS) >= 30, f"разобрано всего {len(PAIRS)} пар «кадр → константа»"


@pytest.mark.parametrize(
    ("name", "frame_id", "design", "bot"),
    PAIRS,
    ids=[f"{n}-{f}" for n, f, _d, _b in PAIRS],
)
def test_screen_matches_frame(name, frame_id, design, bot):
    if (name, frame_id) in KNOWN:
        pytest.skip(KNOWN[(name, frame_id)])
    assert design == bot, (
        f"{name} разошлась с кадром {frame_id}: в макете {design.count('_')} строк "
        f"и {design.count(' ')} пустых, в боте {bot.count('_')} и {bot.count(' ')}. "
        f"Подробности: PYTHONPATH=. ./scripts/figma_screens.py -n {name}"
    )


def test_known_exceptions_are_still_needed():
    """Расхождение исчезло — уберите запись из KNOWN, иначе список зарастёт."""
    stale = [
        key for key in KNOWN
        if any(n == key[0] and f == key[1] and d == b for n, f, d, b in PAIRS)
    ]
    assert not stale, f"эти расхождения исчезли, уберите их из KNOWN: {stale}"
