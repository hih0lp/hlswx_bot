#!/usr/bin/env python3
"""Текстовая выгрузка макета из локального дампа docs/design/figma.json.

Для каждого фрейма страницы выписывает то, что увидит пользователь: текст
сообщения с отступами и клавиатуру рядами. Такой формат, в отличие от PNG,
можно грепать и диффать с текстами бота.

Чтение кадра живёт в `figma_read` — там же объяснено, почему горизонтальные
ряды, скругления и зазоры разбираются именно так.

    ./scripts/figma_export_text.py                    # вся страница Flows
    ./scripts/figma_export_text.py --page Banner
    ./scripts/figma_export_text.py -n Очередь Профиль
    ./scripts/figma_export_text.py --buttons          # только подписи кнопок

Выход по умолчанию — docs/design/flows.md.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from figma_read import buttons_of, frames_of, render  # noqa: E402

DUMP = "docs/design/figma.json"
OUT = "docs/design/flows.md"


def screen(frame) -> list[str]:
    """Кадр -> строки экрана. Кнопки — в скобках, ряд в одну строку."""
    lines = []
    for kind, payload in render(frame):
        if kind == "text":
            lines.extend(payload)
            continue
        # многострочная подпись показывается со стрелкой переноса, иначе
        # ряд кнопок в отчёте не отличить от нескольких кнопок подряд
        labels = [f"[{label.replace(chr(10), ' ↵ ')}]" for label in payload]
        lines.append("    " + " | ".join(labels))
    return lines


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--page", default="Flows", help="подстрока имени страницы")
    p.add_argument("-n", "--names", nargs="+", help="только фреймы с этими подстроками")
    p.add_argument("--buttons", action="store_true", help="только подписи кнопок, списком")
    p.add_argument("-o", "--out", default=OUT, help=f"куда писать (по умолчанию {OUT})")
    args = p.parse_args()

    if not os.path.exists(DUMP):
        sys.exit(f"нет {DUMP} — сначала выгрузите /v1/files/<FILE_KEY>")
    with open(DUMP) as f:
        dump = json.load(f)

    try:
        frames = frames_of(dump, args.page)
    except StopIteration:
        pages = ", ".join(p["name"] for p in dump["document"]["children"])
        sys.exit("страницы не нашлось. Есть: " + pages)

    if args.names:
        frames = [f for f in frames
                  if any(n.lower() in f["name"].lower() for n in args.names)]
    if not frames:
        sys.exit("под фильтр не попал ни один фрейм")

    if args.buttons:
        seen: dict[str, list[str]] = {}
        for frame in frames:
            for label in buttons_of(frame):
                seen.setdefault(label, []).append(frame["name"])
        for label in sorted(seen):
            where = ", ".join(sorted(set(seen[label])))
            print(f"{label.replace(chr(10), ' ↵ ')}\t({where})")
        return

    doc = [f"# Экраны макета — страница «{args.page}»",
           "",
           f"Файл «{dump['name']}», версия от {dump['lastModified']}.",
           f"Выгружено скриптом `scripts/figma_export_text.py`, {len(frames)} фреймов.",
           "Кнопки показаны рядами в квадратных скобках, `↵` — перенос внутри подписи.",
           ""]
    for frame in frames:
        doc.append(f"## {frame['name']}  `{frame['id']}`")
        doc.append("")
        doc.append("```")
        doc.extend(screen(frame))
        doc.append("```")
        doc.append("")

    with open(args.out, "w") as f:
        f.write("\n".join(doc))
    print(f"{len(frames)} фреймов -> {args.out}")


if __name__ == "__main__":
    main()
