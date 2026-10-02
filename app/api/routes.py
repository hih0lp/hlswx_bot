import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.entities import (
    Payment,
    PaymentStatus,
    SubscriptionStatus,
    WhitelabelPartner,
)
from app.services.b2b import integration_for_token, integration_is_live
from app.services.notifications import notify_payment_success
from app.services.payments import mark_payment_succeeded
from app.services.publish import enqueue_subscription_post
from app.services.yookassa import YooKassaService

logger = logging.getLogger("api")
api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    from app.ml.classifier import get_classifier

    clf = get_classifier()
    return {
        "status": "ok",
        "ml_ready": clf.is_ready,
        "ml_meta": clf.meta,
        "ts": datetime.now(UTC).isoformat(),
    }


@api_router.get("/success")
async def payment_success(bot: str | None = None) -> dict:
    import re

    # Клиент клона возвращается в бота клона: его имя приходит в return_url.
    name = bot if bot and re.fullmatch(r"[A-Za-z0-9_]{3,64}", bot) else "HlswxPaySystemBot"
    return {
        "status": "ok",
        "message": f"Оплата обрабатывается. Вернитесь в Telegram-бот @{name}.",
    }


@api_router.get("/payments/yookassa/webhook")
async def yookassa_webhook_info() -> dict:
    return {
        "status": "ok",
        "message": "Это webhook ЮKassa. Принимает только POST-запросы от платёжной системы.",
    }


@api_router.post("/payments/yookassa/webhook")
async def yookassa_webhook(request: Request) -> dict:
    payload = await request.json()
    event = payload.get("event")
    obj = payload.get("object") or {}
    provider_id = obj.get("id")
    if not provider_id:
        return {"status": "ignored"}

    if event not in {"payment.succeeded", "payment.waiting_for_capture"}:
        return {"status": "ignored"}
    if obj.get("status") != "succeeded":
        return {"status": "pending"}

    # Payment notifications do not carry tenant context. Resolve the owning tenant
    # explicitly, then process all related rows within that tenant.
    from app.services.tenant import partner_scope

    async with SessionLocal() as session:
        payment = await session.scalar(
            select(Payment)
            .where(Payment.provider_payment_id == provider_id)
            .execution_options(skip_partner_scope=True)
        )
        if not payment:
            invoice = (obj.get("metadata") or {}).get("invoice_id")
            if invoice:
                payment = await session.scalar(
                    select(Payment)
                    .where(Payment.invoice_id == invoice)
                    .execution_options(skip_partner_scope=True)
                )
        if not payment:
            logger.warning("Payment not found for provider_id=%s", provider_id)
            return {"status": "not_found"}

        partner_id = payment.partner_id
        with partner_scope(partner_id):
            # Re-load under tenant criteria so updates and relationships stay scoped.
            payment = await session.scalar(select(Payment).where(Payment.id == payment.id))
            if not payment:
                return {"status": "not_found"}
            if payment.status == PaymentStatus.succeeded:
                return {"status": "already_processed"}

            from app.services.payments import payment_provider_for_partner
            yk = await payment_provider_for_partner(request.app.state.settings, partner_id)
            remote = await yk.get_payment(provider_id)
            if YooKassaService.payment_status(remote) != "succeeded":
                return {"status": "not_succeeded"}

            remote_method = remote.get("payment_method") or {}
            if payment.save_payment_method and remote_method.get("saved"):
                payment.payment_method_id = remote_method.get("id")
            result = await mark_payment_succeeded(session, payment)
            user_id = payment.user_id
            purpose_id = payment.purpose_id
            topup_amount = payment.amount if payment.purpose == "topup" else None
            logger.info("Payment processed %s -> %s", payment.id, result)

        with partner_scope(partner_id):
            if result == "franchise_activated":
                from app.bot.runtime import _get_main_bot
                partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == purpose_id).execution_options(skip_partner_scope=True))
                if partner:
                    from app.bot.runtime import request_partner_bots_reload, set_partner_billing_state
                    set_partner_billing_state(partner.id, partner.franchise_billing_status, partner.franchise_tier)
                    request_partner_bots_reload()
                    if partner.owner_telegram_id:
                        note = "Оплата франшизы прошла. Автопродление подключено." if partner.franchise_payment_method_id else "Оплата франшизы прошла. Автопродление не включилось: ЮKassa не сохранила способ оплаты."
                        await _get_main_bot().send_message(partner.owner_telegram_id, note)
            elif result == "franchise_awaiting_token":
                partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == purpose_id).execution_options(skip_partner_scope=True))
                if partner:
                    from app.services.franchise_billing import prompt_for_bot_token
                    await prompt_for_bot_token(partner)
            else:
                await notify_payment_success(user_id, result, purpose_id, amount=topup_amount)
    return {"status": "ok"}


@api_router.get("/api/hwls/oneshot-published")
async def oneshot_published_info() -> dict:
    return {
        "status": "ok",
        "message": (
            "Вебхук для ботов Hammer/W: сообщите о выходе разового поста методом POST, "
            "чтобы основной бот увёл группу в 20-минутную тишину. "
            "См. docs/integration_hammer.md"
        ),
    }


@api_router.post("/api/hwls/oneshot-published")
async def oneshot_published(request: Request) -> JSONResponse:
    """Разовый пост вышел в группе — включаем тишину на 20 минут (ТЗ 6.6)."""
    from app.services.oneshot import register_oneshot_publication

    settings = request.app.state.settings
    secret = (settings.hwls_relay_secret or "").strip()
    key = (request.headers.get("X-HWLS-Key") or "").strip()
    if not secret:
        return JSONResponse({"ok": False, "error": "relay_secret_not_configured"}, status_code=503)
    if key != secret:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    raw_chat_id = payload.get("telegram_chat_id")
    try:
        telegram_chat_id = int(raw_chat_id) if raw_chat_id is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_telegram_chat_id"}, status_code=400)

    telegram_username = payload.get("telegram_username") or payload.get("chat_username")
    if not telegram_chat_id and not telegram_username:
        return JSONResponse({"ok": False, "error": "chat_required"}, status_code=400)

    raw_message_id = payload.get("message_id")
    try:
        message_id = int(raw_message_id) if raw_message_id is not None else None
    except (TypeError, ValueError):
        message_id = None

    published_at = None
    raw_published_at = payload.get("published_at")
    if raw_published_at:
        try:
            published_at = datetime.fromisoformat(str(raw_published_at).replace("Z", "+00:00"))
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid_published_at"}, status_code=400)

    async with SessionLocal() as session:
        result = await register_oneshot_publication(
            session,
            telegram_chat_id=telegram_chat_id,
            telegram_username=telegram_username,
            network=(payload.get("network") or "hammer"),
            message_id=message_id,
            published_at=published_at,
        )

    if not result.get("ok"):
        return JSONResponse(result, status_code=404)
    return JSONResponse(result)


@api_router.post("/api/b2b/publish/{token}")
async def b2b_publish(token: str, request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    text = (payload.get("text") or "").strip()
    contact = (payload.get("contact") or "").strip()
    photo_url = (payload.get("photo_url") or payload.get("photo") or "").strip() or None
    if not text:
        return JSONResponse({"ok": False, "error": "text_required"}, status_code=400)

    # Token lookup is a capability resolution: it yields exactly one tenant,
    # after which all subscription, limit and queue work is strictly scoped.
    from app.services.tenant import partner_scope
    from app.models.entities import B2bIntegration, Subscription
    from app.services.volume_limits import check_volume_allowed

    async with SessionLocal() as session:
        integration = await integration_for_token(session, token)
        if not integration:
            return JSONResponse({"ok": False, "error": "subscription_inactive"}, status_code=403)
        tenant_id = integration.partner_id
        integration_id = integration.id
        with partner_scope(tenant_id):
            integration = await session.scalar(
                select(B2bIntegration).where(B2bIntegration.id == integration_id)
            )
            if not integration or not await integration_is_live(session, integration):
                return JSONResponse({"ok": False, "error": "subscription_inactive"}, status_code=403)
            if not integration.subscription_id:
                return JSONResponse({"ok": False, "error": "no_subscription"}, status_code=403)
            sub = await session.scalar(select(Subscription).where(Subscription.id == integration.subscription_id))
            if not sub:
                return JSONResponse({"ok": False, "error": "no_subscription"}, status_code=403)
            allowed, _ = await check_volume_allowed(session, sub.id, sub.posts_volume or "low")
            if not allowed:
                return JSONResponse({"ok": False, "error": "daily_limit_exceeded"}, status_code=429)
            count = await enqueue_subscription_post(
                session, integration.subscription_id, text, contact or "—", photo_url=photo_url,
            )
            if count == 0:
                return JSONResponse({"ok": False, "error": "whitelist_or_scope_denied"}, status_code=403)

    return JSONResponse({"ok": True, "queued_chats": count, "photo": bool(photo_url)})


@api_router.post("/api/wl/publish")
async def wl_publish(request: Request) -> JSONResponse:
    api_key = (request.headers.get("X-API-Key") or request.headers.get("Authorization") or "").strip()
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    if not api_key:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    text = (payload.get("text") or "").strip()
    contact = (payload.get("contact") or "").strip()
    photo_url = (payload.get("photo_url") or payload.get("photo") or "").strip() or None
    if not text:
        return JSONResponse({"ok": False, "error": "text_required"}, status_code=400)

    async with SessionLocal() as session:
        partner = await session.scalar(
            select(WhitelabelPartner).where(
                WhitelabelPartner.api_key == api_key,
                WhitelabelPartner.active.is_(True),
            ),
        )
    if not partner:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    from app.models.entities import Subscription, User
    from app.services.b2b import get_active_corp_subscription
    from app.services.tenant import partner_scope
    from app.services.volume_limits import check_volume_allowed

    with partner_scope(partner.id, partner.owner_telegram_id, getattr(partner, "franchise_tier", "")):
        async with SessionLocal() as session:
            user = None
            if partner.owner_telegram_id:
                user = await session.scalar(
                    select(User).where(User.telegram_id == partner.owner_telegram_id),
                )
            if not user:
                return JSONResponse({"ok": False, "error": "partner_user_not_linked"}, status_code=403)

            sub = await get_active_corp_subscription(session, user.id)
            if not sub:
                sub = await session.scalar(
                    select(Subscription)
                    .where(
                        Subscription.user_id == user.id,
                        Subscription.status == SubscriptionStatus.active,
                    )
                    .order_by(Subscription.id.desc()),
                )
            if not sub:
                return JSONResponse({"ok": False, "error": "subscription_inactive"}, status_code=403)

            allowed, _ = await check_volume_allowed(session, sub.id, sub.posts_volume or "low")
            if not allowed:
                return JSONResponse({"ok": False, "error": "daily_limit_exceeded"}, status_code=429)

            count = await enqueue_subscription_post(
                session, sub.id, text, contact or "—", photo_url=photo_url,
            )
            if count == 0:
                return JSONResponse({"ok": False, "error": "whitelist_or_scope_denied"}, status_code=403)

    return JSONResponse({"ok": True, "queued_chats": count, "partner": partner.bot_username, "photo": bool(photo_url)})


@api_router.post("/api/ml/classify")
async def ml_classify_api(request: Request) -> JSONResponse:
    from app.ml.classifier import get_classifier

    settings = request.app.state.settings
    api_key = (request.headers.get("X-API-Key") or request.headers.get("Authorization") or "").strip()
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()

    authorized = False
    if settings.ml_api_key and api_key == settings.ml_api_key:
        authorized = True
    elif api_key:
        async with SessionLocal() as session:
            partner = await session.scalar(
                select(WhitelabelPartner).where(
                    WhitelabelPartner.api_key == api_key,
                    WhitelabelPartner.active.is_(True),
                ),
            )
            authorized = partner is not None
    if not authorized:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text_required"}, status_code=400)

    result = get_classifier().classify(text)
    return JSONResponse(
        {
            "ok": True,
            "category_code": result.category_code,
            "label": result.label,
            "price_per_chat": result.price_per_chat,
            "confidence": result.confidence,
            "blocked": result.blocked,
            "needs_review": result.needs_review,
        },
    )
