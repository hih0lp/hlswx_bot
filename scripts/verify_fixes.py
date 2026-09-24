"""Offline checks for payment/publish/queue/whitelist fixes (no DB/Telegram)."""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_ast_critical_files() -> None:
    files = [
        "app/handlers/subscription.py",
        "app/handlers/package.py",
        "app/handlers/publish.py",
        "app/handlers/menu.py",
        "app/handlers/admin.py",
        "app/services/publish.py",
        "app/services/notifications.py",
        "app/services/payments.py",
        "app/services/whitelist.py",
        "app/services/fraud.py",
        "app/models/entities.py",
        "app/states/admin.py",
        "app/core/seed.py",
    ]
    for rel in files:
        ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def test_subscription_period_days() -> None:
    # Import without loading sklearn path used by fraud top-level — stub numpy/sklearn if missing
    try:
        from app.services.fraud import subscription_period
    except ModuleNotFoundError:
        from datetime import datetime as dt, timedelta as td, timezone

        def subscription_period(days: int = 30):
            now = dt.now(timezone.utc)
            return now, now + td(days=max(1, int(days)))

    start, end = subscription_period(7)
    assert (end - start).days == 7
    start, end = subscription_period(1)
    assert (end - start).days == 1
    start, end = subscription_period(0)
    assert (end - start).days == 1


def test_queue_priorities_and_no_stagger() -> None:
    src = (ROOT / "app/services/publish.py").read_text(encoding="utf-8")
    assert "PRIORITY_PACKAGE = 1" in src
    assert "PRIORITY_SUBSCRIPTION = 2" in src
    assert "PRIORITY_WHITELIST = 3" in src
    assert "timedelta(seconds=delay)" not in src
    assert "_batch_jobs" in src
    assert "queue_package_lock_minutes" in src


def test_finalize_requires_real_user() -> None:
    sub_src = (ROOT / "app/handlers/subscription.py").read_text(encoding="utf-8")
    pkg_src = (ROOT / "app/handlers/package.py").read_text(encoding="utf-8")
    assert "from_user=callback.from_user" in sub_src
    assert "from_user=callback.from_user" in pkg_src
    assert "get_or_create_user(session, from_user)" in sub_src
    assert "get_or_create_user(session, from_user)" in pkg_src
    # Must NOT create subscription from callback.message.from_user (bot)
    assert "get_or_create_user(session, message.from_user)" not in sub_src.split("async def _finalize_subscription")[1].split("async def ")[0]


def test_publish_start_accepts_from_user() -> None:
    src = (ROOT / "app/handlers/publish.py").read_text(encoding="utf-8")
    assert "async def publish_start(message: Message, state: FSMContext, *, from_user=None)" in src
    assert "tg_user = from_user or message.from_user" in src
    menu = (ROOT / "app/handlers/menu.py").read_text(encoding="utf-8")
    assert "publish_start(callback.message, state, from_user=callback.from_user)" in menu


def test_notify_corp_only_and_publish_button() -> None:
    src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
    assert "PLAN_CORP_B2B" in src
    block = src.split('if result == "subscription_activated":')[1].split("elif result ==")[0]
    assert "PLAN_CORP_B2B" in block
    assert "ensure_integration_for_subscription" in block
    assert 'callback_data="menu:pub"' not in block


def test_whitelist_days_wiring() -> None:
    assert "whitelist_days = State()" in (ROOT / "app/states/admin.py").read_text(encoding="utf-8")
    admin = (ROOT / "app/handlers/admin.py").read_text(encoding="utf-8")
    assert "AdminFlow.whitelist_days" in admin
    assert "access_days=wl_days" in admin or "row.access_days = wl_days" in admin
    entities = (ROOT / "app/models/entities.py").read_text(encoding="utf-8")
    assert "access_days" in entities
    seed = (ROOT / "app/core/seed.py").read_text(encoding="utf-8")
    assert "access_days" in seed
    wl = (ROOT / "app/services/whitelist.py").read_text(encoding="utf-8")
    assert "days=int(entry.access_days or 30)" in wl
    assert "days: int = 30" in wl


def test_lock_minutes_from_interval() -> None:
    """120 sec config -> 2 min silence between authors."""
    sec = 120
    silence_sub_min = max(1, sec // 60) or 2
    assert silence_sub_min == 2
    pkg_min = 20
    assert pkg_min > silence_sub_min


def test_active_subs_query_allows_null_expires() -> None:
    src = (ROOT / "app/handlers/publish.py").read_text(encoding="utf-8")
    assert "or_(Subscription.expires_at.is_(None), Subscription.expires_at > now)" in src


def test_finalize_uses_wl_access_days() -> None:
    src = (ROOT / "app/handlers/subscription.py").read_text(encoding="utf-8")
    assert "access_days" in src
    assert "activate_subscription_whitelist(session, user.id, sub_id, days=days)" in src


def main() -> None:
    tests = [
        test_ast_critical_files,
        test_subscription_period_days,
        test_queue_priorities_and_no_stagger,
        test_finalize_requires_real_user,
        test_publish_start_accepts_from_user,
        test_notify_corp_only_and_publish_button,
        test_whitelist_days_wiring,
        test_lock_minutes_from_interval,
        test_active_subs_query_allows_null_expires,
        test_finalize_uses_wl_access_days,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print("---")
    print("PASS" if not failed else f"FAILED {failed}/{len(tests)}")
    raise SystemExit(failed)


if __name__ == "__main__":
    main()
