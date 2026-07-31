"""
Gmail service — OAuth 2.0 authorization-code flow against Google.

Flow overview:
    1. Admin clicks "Connect Gmail" on frontend.
    2. Frontend calls POST /api/gmail/connect (admin auth).
    3. This service builds a Google consent URL with a signed `state` token.
    4. Frontend redirects the browser to that URL.
    5. Admin picks the GNC Gmail account, approves the scopes.
    6. Google redirects the browser to our callback with `?code=xxx&state=yyy`.
    7. Callback handler calls `exchange_code_for_tokens()`:
         a. Verifies the `state` JWT (CSRF protection).
         b. POSTs to Google's token endpoint to exchange `code` → tokens.
         c. GETs userinfo to know which Gmail account was picked.
         d. Encrypts the refresh_token with Fernet.
         e. UPSERTs the singleton `gmail_connection` row.
    8. Callback handler redirects the browser back to the frontend settings page.

Google's scopes we ask for:
    * openid, email, profile — so we can identify which account connected.
    * https://www.googleapis.com/auth/gmail.readonly — read-only Gmail access.

We force `access_type=offline` + `prompt=consent` so Google always returns
a refresh_token (without these, subsequent authorizations return only an
access_token, and we'd need re-consent every hour).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ExternalServiceError,
    UnauthorizedError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_state_token,
    decode_state_token,
    fernet_encrypt,
)
from app.models.gmail_connection import GmailConnection
from app.repositories import gmail_connection_repo
from app.schemas.gmail import GmailStatusResponse

log = get_logger(__name__)

# ---- Google endpoints (public constants — unlikely to change) ----
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Scopes — space-separated per OAuth 2 convention.
GMAIL_SCOPES = " ".join([
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
])

# Purpose string embedded in the state token — must match on both sides.
STATE_PURPOSE = "gmail_connect"

# HTTP client tuning: Google's token endpoint responds in well under a
# second normally; 15 s is a generous ceiling that also survives transient
# network hiccups from Render's egress.
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


# ---------------------------------------------------------------------------
# Step 1 — build the consent URL
# ---------------------------------------------------------------------------
def build_authorization_url(admin_email: str) -> str:
    """Construct the Google OAuth consent URL for the given admin.

    Raises `ConflictError` if Google credentials aren't configured on the
    server (missing GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI).
    """
    if not settings.google_client_id or not settings.google_redirect_uri:
        raise ConflictError(
            "Google OAuth is not configured on the server. "
            "Set GOOGLE_CLIENT_ID and GOOGLE_REDIRECT_URI env vars."
        )

    state = create_state_token(subject=admin_email, purpose=STATE_PURPOSE)

    params = {
        "response_type": "code",
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "scope": GMAIL_SCOPES,
        # `offline` = give us a refresh_token; `consent` = always show the
        # consent screen so Google re-issues a refresh_token every time
        # (needed if the operator has to reconnect after rotating secrets).
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Step 2 — exchange the auth code for tokens
# ---------------------------------------------------------------------------
async def exchange_code_for_tokens(
    code: str,
    state: str,
    session: AsyncSession,
) -> GmailConnection:
    """Called by the OAuth callback.

    Verifies the state token, exchanges the auth code for tokens with
    Google, fetches user info, encrypts the refresh token, and UPSERTs
    the singleton row.
    """
    # ---- 1. CSRF: verify the state JWT we issued in build_authorization_url ----
    if not code:
        raise BadRequestError("Missing `code` query parameter from Google.")
    if not state:
        raise BadRequestError("Missing `state` query parameter from Google.")

    state_claims = decode_state_token(state, expected_purpose=STATE_PURPOSE)
    admin_email = state_claims["sub"]

    # ---- 2. Exchange code → tokens ----
    if not settings.google_client_secret:
        raise ConflictError("GOOGLE_CLIENT_SECRET not configured on server.")

    token_response = await _post_token_exchange(code)
    refresh_token = token_response.get("refresh_token")
    access_token = token_response.get("access_token")
    expires_in = token_response.get("expires_in", 3600)
    scope = token_response.get("scope", GMAIL_SCOPES)

    if not refresh_token:
        # This happens if the user has previously granted consent and
        # Google decided not to re-issue a refresh token. Our
        # `prompt=consent` should prevent this, but be defensive.
        raise ExternalServiceError(
            "Google did not return a refresh token. "
            "Revoke previous access at https://myaccount.google.com/permissions "
            "and try connecting again."
        )

    # ---- 3. Ask Google who just authenticated ----
    userinfo = await _fetch_userinfo(access_token)
    google_email = userinfo.get("email")
    google_display_name = userinfo.get("name")
    google_user_id = userinfo.get("sub")

    if not google_email:
        raise ExternalServiceError("Google userinfo response missing `email`.")

    # ---- 4. Optional domain lock ----
    allowed_domain = (settings.google_allowed_domain or "").strip().lower()
    if allowed_domain and not google_email.lower().endswith("@" + allowed_domain):
        raise UnauthorizedError(
            f"Only accounts from @{allowed_domain} may be connected."
        )

    # ---- 5. Encrypt + persist ----
    now = datetime.now(UTC)
    row = await gmail_connection_repo.upsert_singleton(
        session,
        email=google_email,
        display_name=google_display_name,
        google_user_id=google_user_id,
        refresh_token_encrypted=fernet_encrypt(refresh_token),
        access_token_encrypted=fernet_encrypt(access_token) if access_token else None,
        access_token_expiry=now + timedelta(seconds=int(expires_in)),
        scopes=scope,
        is_connected=True,
        connected_by_admin_email=admin_email,
    )
    await session.commit()

    log.info(
        "gmail_connected",
        gmail_email=google_email,
        connected_by=admin_email,
    )
    return row


# ---------------------------------------------------------------------------
# Status / disconnect
# ---------------------------------------------------------------------------
async def get_status(session: AsyncSession) -> GmailStatusResponse:
    """Return the current Gmail connection state (or `connected=False`)."""
    row = await gmail_connection_repo.get_singleton(session)
    if row is None or not row.is_connected:
        return GmailStatusResponse(connected=False)
    return GmailStatusResponse(
        connected=True,
        email=row.email,
        display_name=row.display_name,
        scopes=row.scopes,
        last_sync_at=row.last_sync_at,
        connected_by_admin_email=row.connected_by_admin_email,
        connected_at=row.created_at,
    )


async def disconnect(session: AsyncSession) -> bool:
    """Remove the singleton row. Returns True if a connection existed."""
    removed = await gmail_connection_repo.delete_singleton(session)
    if removed:
        await session.commit()
        log.info("gmail_disconnected")
    return removed


# ---------------------------------------------------------------------------
# Private HTTP helpers
# ---------------------------------------------------------------------------
async def _post_token_exchange(code: str) -> dict[str, Any]:
    """POST to Google's token endpoint, return the JSON body.
    Raises ExternalServiceError with the Google error if it fails."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.post(GOOGLE_TOKEN_URL, data=payload)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Google token endpoint unreachable: {exc}") from exc

    if resp.status_code != 200:
        # Google returns {"error": "...", "error_description": "..."}
        detail = _safe_json(resp)
        log.warning("google_token_exchange_failed", status=resp.status_code, body=detail)
        raise ExternalServiceError(
            "Google rejected the authorization code: "
            f"{detail.get('error_description') or detail.get('error') or resp.text}"
        )
    return resp.json()


async def _fetch_userinfo(access_token: str) -> dict[str, Any]:
    """GET userinfo with the freshly-issued access_token."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Google userinfo endpoint unreachable: {exc}") from exc

    if resp.status_code != 200:
        detail = _safe_json(resp)
        raise ExternalServiceError(
            f"Google userinfo call failed: {detail.get('error') or resp.text}"
        )
    return resp.json()


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError:
        return {}
