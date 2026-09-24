#!/usr/bin/env python3
"""Выгрузка картинок из Figma-файла HLSWX Bot.

Работает по локальному дампу docs/design/figma.json (оттуда берутся id нод
и imageRef'ы), поэтому запускать надо там, где есть доступ к api.figma.com.

    export FIGMA_TOKEN=...   # Settings -> Security -> Personal access tokens
    export FILE_KEY=...      # кусок из URL: figma.com/design/<FILE_KEY>/<имя>

    ./scripts/figma_fetch_images.py fills            # растровые заливки
    ./scripts/figma_fetch_images.py frames           # рендер экранов Flows в PNG
    ./scripts/figma_fetch_images.py frames -n Home Профиль
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DUMP = "docs/design/figma.json"
OUT_FILLS = "docs/design/fills"
OUT_FRAMES = "docs/design/png"

# сигнатура -> расширение; заливки бывают не только png
MAGIC = [
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),
    (b"<svg", ".svg"),
    (b"<?xml", ".svg"),
]


CURL = shutil.which("curl")


def api(url, token):
    """Запрос к api.figma.com. Через urllib, а не curl: токен в argv видно в ps."""
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Figma API {e.code}: {e.read(500).decode('utf-8', 'replace')}")


# Системный OpenSSL 3.6.3 на части соединений к S3 роняет handshake с
# error:0A0003E7 "invalid session id" -- одинаково и в python, и в curl.
# Ошибка плавающая (у имени S3 много IP), поэтому лечится повторами.
# --no-sessionid отключает переиспользование session id на стороне curl.
# Подобрать другие флаги можно через FIGMA_CURL_OPTS, например:
#     FIGMA_CURL_OPTS="--tlsv1.2 --tls-max 1.2" ./scripts/figma_fetch_images.py frames
CURL_OPTS = os.environ.get("FIGMA_CURL_OPTS", "--no-sessionid").split()


def fetch_to_file(url, path, tries=6):
    """Качает url в path. Ссылки от Figma подписанные, заголовки не нужны."""
    for attempt in range(1, tries + 1):
        try:
            if CURL:
                r = subprocess.run(
                    [CURL, "-sS", "--fail", "--location", "--connect-timeout", "20",
                     "--max-time", "180", *CURL_OPTS, "-o", path, url],
                    capture_output=True, text=True)
                if r.returncode:
                    err = OSError(f"curl {r.returncode}: {r.stderr.strip()[:200]}")
                    # 22 = HTTP 4xx, обычно протухшая ссылка; повторы не помогут
                    err.fatal = r.returncode == 22
                    raise err
            else:
                with urllib.request.urlopen(url, timeout=180) as resp, \
                        open(path, "wb") as f:
                    shutil.copyfileobj(resp, f)
            return
        except Exception as e:
            if os.path.exists(path):
                os.unlink(path)                 # curl мог оставить огрызок
            if getattr(e, "fatal", False) or attempt == tries:
                print(f"  сдался, URL: {url[:120]}", file=sys.stderr)
                raise
            time.sleep(1.5 * attempt)


def download(url, path_noext):
    """Скачивает url, расширение определяет по сигнатуре. Возвращает путь."""
    done = [p for p in glob.glob(glob.escape(path_noext) + ".*")
            if not p.endswith(".part") and os.path.getsize(p)]
    if done:
        return done[0] + " (уже есть)"

    tmp = path_noext + ".part"
    fetch_to_file(url, tmp)
    with open(tmp, "rb") as f:
        head = f.read(8)
    ext = next((e for sig, e in MAGIC if head.startswith(sig)), ".bin")
    path = path_noext + ext
    os.replace(tmp, path)        # имя появляется только у целого файла
    return path


def load_dump():
    if not os.path.exists(DUMP):
        sys.exit(f"нет {DUMP} — сначала выгрузите /v1/files/<FILE_KEY>")
    with open(DUMP) as f:
        return json.load(f)


def safe_name(name):
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()[:60]


def cmd_fills(args, token, key):
    """Пиксели растровых заливок. В дампе лежит только imageRef, не картинка."""
    doc = load_dump()["document"]
    refs = set()

    def walk(n):
        for fill in n.get("fills") or []:
            if fill.get("type") == "IMAGE" and fill.get("imageRef"):
                refs.add(fill["imageRef"])
        for c in n.get("children", []):
            walk(c)

    walk(doc)
    print(f"в дампе {len(refs)} уникальных imageRef")

    images = api(f"https://api.figma.com/v1/files/{key}/images", token)["meta"]["images"]
    os.makedirs(OUT_FILLS, exist_ok=True)

    got, failed = 0, []
    for ref in sorted(refs):
        url = images.get(ref)
        if not url:
            print("  нет url:", ref)
            failed.append(ref)
            continue
        try:
            print("ok", download(url, os.path.join(OUT_FILLS, ref)))
            got += 1
        except Exception as e:
            print(f"  не скачался {ref}: {e}")
            failed.append(ref)

    print(f"\nскачано {got} из {len(refs)} -> {OUT_FILLS}/")
    if failed:
        print(f"не вышло: {len(failed)} — перезапустите, готовое пропустится")
        sys.exit(1)


def cmd_frames(args, token, key):
    """Рендер фреймов страницы в PNG/SVG."""
    pages = load_dump()["document"]["children"]
    page = next((p for p in pages if args.page in p["name"]), None)
    if page is None:
        sys.exit("страницы не нашлось. Есть: " + ", ".join(p["name"] for p in pages))

    frames = [c for c in page["children"] if c["type"] == "FRAME"]
    if args.names:
        frames = [f for f in frames if any(n.lower() in f["name"].lower() for n in args.names)]
    if not frames:
        sys.exit("под фильтр не попал ни один фрейм")

    os.makedirs(OUT_FRAMES, exist_ok=True)
    print(f"страница {page['name']!r}: рендерим {len(frames)} фреймов, "
          f"{args.format} x{args.scale}")

    got, failed = 0, []
    for i in range(0, len(frames), args.batch):
        chunk = frames[i:i + args.batch]
        params = {"ids": ",".join(f["id"] for f in chunk), "format": args.format}
        if args.format in ("png", "jpg"):
            params["scale"] = args.scale
        res = api(f"https://api.figma.com/v1/images/{key}?{urllib.parse.urlencode(params)}",
                  token)
        if res.get("err"):
            sys.exit(f"ошибка рендера: {res['err']}")
        for fr in chunk:
            url = res["images"].get(fr["id"])
            if not url:
                # обычно означает, что фрейм слишком большой для текущего scale
                print("  не отрендерился:", fr["name"])
                failed.append(fr["name"])
                continue
            stem = f"{fr['id'].replace(':', '-')}_{safe_name(fr['name'])}"
            try:
                print("ok", download(url, os.path.join(OUT_FRAMES, stem)))
                got += 1
            except Exception as e:
                # один сбойный handshake не повод ронять весь прогон
                print(f"  не скачался {fr['name']}: {e}")
                failed.append(fr["name"])

    print(f"\nскачано {got} из {len(frames)} -> {OUT_FRAMES}/")
    if failed:
        print(f"не вышло: {len(failed)} — {', '.join(failed[:8])}"
              f"{'...' if len(failed) > 8 else ''}")
        print("перезапустите: скачанное пропустится. Если фрейм не отрендерился"
              " вовсе — он слишком большой, попробуйте --scale 1")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fills", help="растровые заливки (imageRef -> файл)")

    f = sub.add_parser("frames", help="рендер фреймов страницы")
    f.add_argument("--page", default="Flows", help="подстрока имени страницы (по умолчанию Flows)")
    f.add_argument("-n", "--names", nargs="+", help="брать только фреймы с этими подстроками в имени")
    f.add_argument("--format", default="png", choices=["png", "jpg", "svg", "pdf"])
    f.add_argument("--scale", type=float, default=2, help="1..4, только png/jpg")
    f.add_argument("--batch", type=int, default=25, help="сколько нод на один запрос")

    args = p.parse_args()

    token, key = os.environ.get("FIGMA_TOKEN"), os.environ.get("FILE_KEY")
    if not token or not key:
        sys.exit("нужны переменные окружения FIGMA_TOKEN и FILE_KEY")

    {"fills": cmd_fills, "frames": cmd_frames}[args.cmd](args, token, key)


if __name__ == "__main__":
    main()
