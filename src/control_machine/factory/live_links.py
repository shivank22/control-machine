"""Signed live-desktop links bound to an open Factory session."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings, get_settings

LIVE_TTL_SECONDS = 15 * 60


class FactoryLiveError(ValueError):
    """Raised when a Factory live link cannot be minted or verified."""


@dataclass(frozen=True)
class FactoryLiveToken:
    session_id: int
    expires_at: int
    jti: str
    token: str


class FactoryLiveLinks:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def configured(self) -> bool:
        return bool(self.settings.live_link_secret.strip())

    def public_base_url(self) -> str:
        base = self.settings.public_base_url.strip().rstrip("/")
        if base:
            return base
        return f"http://{self.settings.host}:{self.settings.port}"

    def mint(self, session_id: int) -> tuple[dict[str, str], FactoryLiveToken]:
        if not self.configured():
            raise FactoryLiveError("Live link signing is not configured.")
        expires_at = int(time.time()) + LIVE_TTL_SECONDS
        jti = secrets.token_urlsafe(8)
        sig = _sign(self._secret, f"{session_id}:{expires_at}:{jti}")
        token = _encode(f"{session_id}:{expires_at}:{jti}:{sig}")
        link = FactoryLiveToken(
            session_id=session_id, expires_at=expires_at, jti=jti, token=token
        )
        url = f"{self.public_base_url()}/desktop?t={token}"
        return {
            "url": url,
            "expires_at": _iso(expires_at),
        }, link

    def verify(self, token: str, *, expected_jti: str | None = None) -> FactoryLiveToken:
        if not self.configured():
            raise FactoryLiveError("Live link signing is not configured.")
        payload = _decode(token)
        parts = payload.split(":")
        if len(parts) != 4:
            raise FactoryLiveError("Invalid live token.")
        session_s, exp_s, jti, sig = parts
        expected = _sign(self._secret, f"{session_s}:{exp_s}:{jti}")
        if not hmac.compare_digest(sig, expected):
            raise FactoryLiveError("Invalid live token.")
        try:
            session_id = int(session_s)
            expires_at = int(exp_s)
        except ValueError as exc:
            raise FactoryLiveError("Invalid live token.") from exc
        if expected_jti is not None and not hmac.compare_digest(jti, expected_jti):
            raise FactoryLiveError("This live link has been revoked.")
        if int(time.time()) >= expires_at:
            raise FactoryLiveError("This live link has expired.")
        return FactoryLiveToken(
            session_id=session_id, expires_at=expires_at, jti=jti, token=token
        )

    @property
    def _secret(self) -> str:
        return self.settings.live_link_secret.strip()


def expires_datetime(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return digest.hex()[:32]


def _encode(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + padding).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise FactoryLiveError("Invalid live token.") from exc


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
