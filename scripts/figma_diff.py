#!/usr/bin/env python3
"""Сверка макета с текстами бота: что нарисовано, но бот такого не говорит.

Слева — строки экранов из docs/design/figma.json (через `figma_read`, там же
объяснено, как читается кадр), справа — корпус строк, которые бот вообще
способен показать: все строковые литералы и f-строки из app/**.py. Совпадение
ищется по шаблону: подстановки ({city}, {}) и числа считаются любыми
значениями, HTML-разметка снимается.

Отчёт — список кандидатов, а не приговор: бот собирает часть строк по кускам,
поэтому найденное надо вычитывать глазами. Образцы данных (@username, #124,
названия городов и чатов) отсеиваются заранее.

    PYTHONPATH=. ./scripts/figma_diff.py > /tmp/diff.txt
"""

import ast
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from figma_read import frames_of, render  # noqa: E402

NBSP = "\u00a0\u2009\u202f"


# ---------------------------------------------------------------- корпус бота

def literals():
    """Все строки из исходников: константы, литералы, f-строки."""
    out = set()
    for path in glob.glob("app/**/*.py", recursive=True):
        try:
            tree = ast.parse(open(path).read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.add(node.value)
            elif isinstance(node, ast.JoinedStr):
                parts = []
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        parts.append(v.value)
                    else:
                        parts.append("{}")
                out.add("".join(parts))
    return out


TAG = re.compile(r"</?[a-z][a-z0-9]*>")
PLACE = re.compile(r"\{[^{}]*\}")
SPACE = re.compile("[" + NBSP + r"\s]+")
DIGITS = re.compile("\\d[\\d" + NBSP + " ,.]*")
BREAK = re.compile("[\\n\\r\u2028\u2029]+")


def clean(text):
    """Снимает HTML и приводит пробелы — одинаково для обеих сторон."""
    return SPACE.sub(" ", TAG.sub("", text)).strip()


def lines_of(text):
    """Разбивает по переносам до нормализации — clean() их съедает."""
    return [c for c in (clean(p) for p in BREAK.split(text)) if c]


def to_pattern(line):
    """Строка бота -> регулярка: подстановки и числа считаются любыми.

    Шаблон, у которого своего текста почти нет (голая `{}` в f-строке),
    совпал бы с чем угодно и обнулил бы сверку — такие отбрасываем.
    """
    chunks = PLACE.split(line)
    if len("".join(chunks).strip()) < 5 or len(max(chunks, key=len).strip()) < 3:
        return None
    rx = ".*?".join(re.escape(c) for c in chunks)
    rx = DIGITS.sub("\\\\d[\\\\d" + NBSP + " ,.]*", rx)
    return re.compile("^" + rx + "$")


def bot_patterns():
    pats = []
    for raw in literals():
        for part in lines_of(raw):
            if len(part) < 2:
                continue
            try:
                rx = to_pattern(part)
            except re.error:
                continue
            if rx is not None:
                pats.append((part, rx))
    return pats


# ------------------------------------------------------------ сторона макета

def items_of(frame):
    """('btn'|'txt', строка) в порядке чтения.

    Многострочная подпись кнопки разбирается построчно: бот собирает такую
    подпись из кусков, и целиком она в корпусе всё равно не найдётся.
    """
    out = []
    for kind, payload in render(frame):
        if kind == "text":
            out.extend(("txt", clean(line)) for line in payload if line.strip())
            continue
        for label in payload:
            out.extend(("btn", clean(line)) for line in label.split("\n") if line.strip())
    return out


# Образцы данных в макете: имена, id, суммы, города, названия чатов. Они не
# расхождение — бот на их месте подставляет своё.
def noise_filter():
    from app.ml.categories import CITY_CHOICES

    cities = "|".join(re.escape(label) for _, label in CITY_CHOICES)
    return [re.compile(p) for p in (
        r"^@[\w.]+$",
        r"^#\d+$",
        r"^[\d  ,.]+ ?₽?$",
        r"^\d+ / \d+$",
        r"^[📍🌍]? ?(" + cities + r")$",
        r"^\d{2}\.\d{2}\.\d{4}$",
        r"^[\w\s|🔨]+ (" + cities + r")( \| W)?$",
        r"^(" + cities + r") ?· ?",
        r"^[💬📍] ?\d+ (чат|чата|чатов)$",
        r"^#\d+ · @[\w.]+$",
        r"^Имя: ", r"^ID: \d+$", r"^Username: @",
    )]


def main():
    if not os.path.exists("docs/design/figma.json"):
        sys.exit("нет docs/design/figma.json — сначала выгрузите дамп")
    dump = json.load(open("docs/design/figma.json"))

    pats = bot_patterns()
    exact = {p[0] for p in pats}
    noise = noise_filter()
    print(f"корпус бота: {len(pats)} шаблонов", file=sys.stderr)

    report, skipped = [], 0
    frames = frames_of(dump)
    for frame in frames:
        misses, seen = [], set()
        for kind, line in items_of(frame):
            if line in exact or any(rx.match(line) for _, rx in pats):
                continue
            if any(rx.match(line) for rx in noise):
                skipped += 1
                continue
            if (kind, line) in seen:          # в списках строка повторяется
                continue
            seen.add((kind, line))
            misses.append((kind, line))
        if misses:
            report.append((frame["name"], frame["id"], misses))

    total = sum(len(m) for _, _, m in report)
    print(f"отсеяно образцов данных: {skipped}", file=sys.stderr)
    print("# Строки макета, которых нет в боте\n")
    print(f"Экранов с расхождениями: {len(report)} из {len(frames)}, строк: {total}\n")
    for name, nid, misses in report:
        print(f"## {name}  `{nid}`")
        for kind, line in misses:
            print(f"  [{'кнопка' if kind == 'btn' else 'текст '}] {line}")
        print()


if __name__ == "__main__":
    main()
