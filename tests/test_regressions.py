from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_finalize_uses_callback_from_user() -> None:
    src = (ROOT / "app/handlers/subscription.py").read_text(encoding="utf-8")
    block = src.split("async def _finalize_subscription")[1].split("async def ")[0]
    assert "get_or_create_user(session, from_user)" in block
    assert "get_or_create_user(session, message.from_user)" not in block
    assert "from_user=callback.from_user" in src


def test_package_finalize_uses_callback_from_user() -> None:
    src = (ROOT / "app/handlers/package.py").read_text(encoding="utf-8")
    assert "from_user=callback.from_user" in src
    assert "get_or_create_user(session, from_user)" in src


def test_menu_passes_from_user_to_publish_and_mysubs() -> None:
    src = (ROOT / "app/handlers/menu.py").read_text(encoding="utf-8")
    assert "from_user=callback.from_user" in src
    assert src.count("from_user=callback.from_user") >= 3


def test_admin_wl_delete_revokes_subscription() -> None:
    src = (ROOT / "app/handlers/admin.py").read_text(encoding="utf-8")
    assert "revoke_whitelist_subscription" in src


def test_notify_b2b_only_for_corp() -> None:
    src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
    block = src.split('if result == "subscription_activated":')[1].split("elif result ==")[0]
    assert "PLAN_CORP_B2B" in block
    assert "ensure_integration_for_subscription" in block


def test_no_stagger_in_enqueue() -> None:
    src = (ROOT / "app/services/publish.py").read_text(encoding="utf-8")
    assert "timedelta(seconds=delay)" not in src
    assert "_batch_jobs" in src


def test_critical_files_parse() -> None:
    files = [
        "app/handlers/subscription.py",
        "app/handlers/package.py",
        "app/handlers/publish.py",
        "app/handlers/menu.py",
        "app/handlers/admin.py",
        "app/services/publish.py",
        "app/services/whitelist.py",
        "app/services/payments.py",
    ]
    for rel in files:
        ast.parse((ROOT / rel).read_text(encoding="utf-8"))
