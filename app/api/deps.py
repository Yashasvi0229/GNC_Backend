"""
Reusable FastAPI dependencies.

Every route that needs auth pulls its identity from `get_current_admin`.
Callers get a `CurrentAdmin` dataclass — a small, typed identity object —
never a raw JWT dict. This means route handlers can't accidentally read
untrusted claims off a token.

In Phase 2 (multi-user) we'll swap the internals: `get_current_admin`
becomes `get_current_user` and does a DB lookup + role check. The public
interface stays the same, so route handlers don't change.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, status

from app.config import settings
from app.core.constants import UserRole
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_access_token


@dataclass(frozen=True, slots=True)
class CurrentAdmin:
    """Identity of the currently-authenticated admin."""

    email: str
    name: str
    role: str


def _extract_bearer_token(authorization: str | None) -> str:
    """Parse `Authorization: Bearer <token>`. Raises UnauthorizedError if malformed."""
    if not authorization:
        raise UnauthorizedError("Missing Authorization header.")
    parts = authorization.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise UnauthorizedError("Authorization header must be 'Bearer <token>'.")
    token = parts[1].strip()
    if not token:
        raise UnauthorizedError("Empty Bearer token.")
    return token


async def get_current_admin(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> CurrentAdmin:
    """FastAPI dependency — returns the current admin, or 401/403.

    Phase 1: verifies the JWT and returns the admin identity built from
    env-var credentials. The JWT's `sub` claim MUST match `settings.admin_email`
    — this prevents a stolen dev token from being valid in prod, or vice-versa.
    """
    token = _extract_bearer_token(authorization)
    claims = decode_access_token(token)

    # Cross-check: the token's subject must match the currently configured
    # admin. If the admin email is rotated in env, all old tokens die.
    subject = claims.get("sub", "")
    if subject != settings.admin_email:
        raise UnauthorizedError("Token identity does not match current admin.")

    role = claims.get("role", UserRole.USER.value)
    if role != UserRole.ADMIN.value:
        # Phase 1: only Admin exists. Explicit check so Phase 2 doesn't
        # accidentally leak admin-only endpoints to lower roles.
        raise ForbiddenError("Admin role required.")

    return CurrentAdmin(
        email=subject,
        name=claims.get("name", settings.admin_display_name),
        role=role,
    )


# Re-exported for convenience — matches conventional FastAPI usage.
__all__ = ["CurrentAdmin", "get_current_admin"]
