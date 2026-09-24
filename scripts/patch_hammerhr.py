"""One-shot: add @HammerHR to ADMIN_IDS in /opt/hwls-bot/.env on server."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ENV_PATH = Path("/opt/hwls-bot/.env")


def main() -> None:
    if not ENV_PATH.exists():
        print("No .env at", ENV_PATH)
        sys.exit(1)
    env = ENV_PATH.read_text(encoding="utf-8")
    token_m = re.search(r"^BOT_TOKEN=(.+)$", env, re.M)
    if not token_m:
        print("BOT_TOKEN missing")
        sys.exit(1)
    token = token_m.group(1).strip()
    url = f"https://api.telegram.org/bot{token}/getChat?chat_id=@HammerHR"
    data = json.loads(urllib.request.urlopen(url, timeout=20).read())
    if not data.get("ok"):
        print("HammerHR lookup failed:", data.get("description", data))
        print("Hint: @HammerHR должен написать /start боту или передайте числовой ID в ADMIN_IDS")
        return
    hr_id = str((data.get("result") or {}).get("id") or "")
    if not hr_id:
        print("HammerHR not found:", data)
        return
    adm = re.search(r"^ADMIN_IDS=(.+)$", env, re.M)
    cur = adm.group(1).strip() if adm else ""
    ids = [x.strip() for x in cur.split(",") if x.strip()]
    if hr_id in ids:
        print("HammerHR already admin:", hr_id)
        return
    new = ",".join([*ids, hr_id])
    env = re.sub(r"^ADMIN_IDS=.*$", f"ADMIN_IDS={new}", env, flags=re.M)
    ENV_PATH.write_text(env, encoding="utf-8")
    print("Added HammerHR", hr_id)
    subprocess.run(["docker", "compose", "restart", "app"], cwd="/opt/hwls-bot", check=False)


if __name__ == "__main__":
    main()
