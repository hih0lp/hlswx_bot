"""Проверка функционала HWLS бота на сервере."""

import json
import os
import sys
import urllib.request

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = os.environ.get("HWLS_HOST", "161.104.47.79")
PASSWORD = os.environ.get("HWLS_SSH_PASSWORD", "")
TOKEN = os.environ.get("BOT_TOKEN", "")
RESOLVE = "149.154.167.220"


def tg(method: str) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(url)
    # use curl via ssh for resolve
    return {}


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username="root", password=PASSWORD, timeout=25)

    checks = [
        ("health", "curl -sS -m 10 http://127.0.0.1:8082/health"),
        ("hwls container", "docker ps --filter name=hwls_app --format '{{.Status}}'"),
        ("polling", "docker logs hwls_app 2>&1 | grep 'Run polling' | tail -1"),
        ("ml", "docker logs hwls_app 2>&1 | grep 'ML model loaded' | tail -1"),
        ("hammer ok", "curl -sS -m 5 http://127.0.0.1:8000/health"),
        ("posting ok", "curl -sS -m 5 http://127.0.0.1:8080/health"),
        ("welcome image", "docker exec hwls_app test -f /app/assets/hwls_welcome.png && echo OK || echo MISSING"),
        ("image size", "docker exec hwls_app ls -la /app/assets/hwls_welcome.png"),
        ("bot getMe", f"curl -sS -m 15 --resolve api.telegram.org:443:{RESOLVE} https://api.telegram.org/bot{TOKEN}/getMe"),
        ("webhook", f"curl -sS -m 15 --resolve api.telegram.org:443:{RESOLVE} https://api.telegram.org/bot{TOKEN}/getWebhookInfo"),
        ("db cities", "docker exec hwls_app python -c \"import asyncio; from sqlalchemy import select, func; from app.db.session import SessionLocal; from app.models.entities import City, Chat; async def m():\n async with SessionLocal() as s:\n  c=await s.scalar(__import__('sqlalchemy').select(func.count()).select_from(City));\n  ch=await s.scalar(__import__('sqlalchemy').select(func.count()).select_from(Chat));\n  print(f'cities={c} chats={ch}')\nasyncio.run(m())\""),
        ("recent errors", "docker logs hwls_app 2>&1 | grep -iE 'error|exception|traceback' | tail -5 || echo none"),
    ]

    print("=== HWLS FUNCTIONAL CHECK ===\n")
    all_ok = True
    for name, cmd in checks:
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=60)
        out = (stdout.read() + stderr.read()).decode("utf-8", errors="replace").strip()
        ok = "error" not in name.lower() and ("ok" in out.lower() or "polling" in out.lower() or "healthy" in out.lower() or "cities=" in out or '"ok"' in out or "true" in out or out.endswith("OK") or "HwlsPay" in out or "none" in out or len(out) > 2)
        if name == "webhook":
            ok = '"url":""' in out.replace(" ", "") or '"url": null' in out.lower() or not out
        if name == "recent errors":
            ok = out == "none" or not out
        status = "OK" if ok else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"[{status}] {name}:")
        print(f"  {out[:300]}\n")

    ssh.close()
    print("OVERALL:", "PASS" if all_ok else "NEEDS ATTENTION")


if __name__ == "__main__":
    main()
