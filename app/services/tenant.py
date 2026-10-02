"""Request-local tenant context for the main bot and franchise bots."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

partner_id_var: ContextVar[int] = ContextVar("partner_id", default=0)
partner_owner_var: ContextVar[int | None] = ContextVar("partner_owner_id", default=None)
partner_tier_var: ContextVar[str] = ContextVar("partner_tier", default="")


def current_partner_id() -> int:
    return partner_id_var.get()


def current_partner_owner_id() -> int | None:
    return partner_owner_var.get()


def current_partner_tier() -> str:
    return partner_tier_var.get()


def enter_partner_scope(partner_id: int = 0, owner_id: int | None = None, tier: str = "") -> tuple[Token, Token, Token]:
    return (
        partner_id_var.set(int(partner_id or 0)),
        partner_owner_var.set(owner_id),
        partner_tier_var.set((tier or "").lower()),
    )


def leave_partner_scope(tokens: tuple[Token, Token, Token]) -> None:
    partner_id_var.reset(tokens[0])
    partner_owner_var.reset(tokens[1])
    partner_tier_var.reset(tokens[2])


@contextmanager
def partner_scope(partner_id: int = 0, owner_id: int | None = None, tier: str = "") -> Iterator[None]:
    tokens = enter_partner_scope(partner_id, owner_id, tier)
    try:
        yield
    finally:
        leave_partner_scope(tokens)
