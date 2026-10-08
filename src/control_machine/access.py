"""Factory workspace session and the checks that run on every call.

The bearer is the person's Factory session. It is not the bot token and not the
live-desktop link. The token is signed and expiring. It is not a table column.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Settings, get_settings

ONLINE_WINDOW = timedelta(seconds=30)
OPEN_SESSION_STATUSES = frozenset({"running", "paused"})
TOOL_NAMES = frozenset({"browser", "clicks", "sap"})


class SessionError(ValueError):
    """Raised when a Factory session cannot be issued or verified."""


@dataclass(frozen=True)
class FactorySession:
    expires_at: int
    jti: str
    token: str

    @property
    def expired(self) -> bool:
        return int(time.time()) >= self.expires_at


class FactorySessions:
    """Issue and check the workspace bearer. Revocation lives in this process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._revoked: set[str] = set()

    def configured(self) -> bool:
        return bool(self._access_key) and bool(self._secret)

    def login(self, key: str) -> dict[str, str]:
        if not self.configured() or not _same(key, self._access_key):
            raise SessionError("Workspace key was not accepted.")
        return self._public(self._issue())

    def verify(self, token: str) -> FactorySession:
        if not self._secret:
            raise SessionError("Session is invalid or expired.")
        payload = _decode_token(token)
        parts = payload.split(":")
        if len(parts) != 4 or parts[0] != "v1":
            raise SessionError("Session is invalid or expired.")
        _, exp_s, jti, sig = parts
        expected = _sign(self._secret, f"v1:{exp_s}:{jti}")
        if not hmac.compare_digest(sig, expected):
            raise SessionError("Session is invalid or expired.")
        try:
            expires_at = int(exp_s)
        except ValueError as exc:
            raise SessionError("Session is invalid or expired.") from exc
        if jti in self._revoked or int(time.time()) >= expires_at:
            raise SessionError("Session is invalid or expired.")
        return FactorySession(expires_at=expires_at, jti=jti, token=token)

    def revoke(self, token: str) -> None:
        session = self.verify(token)
        self._revoked.add(session.jti)

    def _issue(self) -> FactorySession:
        expires_at = int(time.time()) + self.settings.factory_session_ttl_seconds
        jti = secrets.token_urlsafe(8)
        sig = _sign(self._secret, f"v1:{expires_at}:{jti}")
        token = _encode_token(f"v1:{expires_at}:{jti}:{sig}")
        return FactorySession(expires_at=expires_at, jti=jti, token=token)

    def _public(self, session: FactorySession) -> dict[str, str]:
        return {"token": session.token, "expires_at": expires_iso(session.expires_at)}

    @property
    def _access_key(self) -> str:
        return self.settings.factory_access_key.strip()

    @property
    def _secret(self) -> str:
        return self.settings.factory_session_secret.strip()


def computer_online(last_seen_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Online means last_seen_at is within 30 seconds."""
    if last_seen_at is None:
        return False
    seen = last_seen_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - seen <= ONLINE_WINDOW


def in_workspace(*, computer_id: int, workspace_computer_ids: set[int]) -> bool:
    """An agent is visible when its computer is in this workspace."""
    return computer_id in workspace_computer_ids


def session_is_open(status: str) -> bool:
    """A running or paused session is the one open intent on that computer."""
    return status in OPEN_SESSION_STATUSES


def new_session_block(*, online: bool, open_session: bool) -> str | None:
    """Why a new session must not start, or None when it may.

    A chat message while the computer is offline does not call this. That post
    is still saved, and no session row is inserted.
    """
    if not online:
        return "Computer is offline."
    if open_session:
        return "Computer already has an open session."
    return None


def tool_name_ok(name: str) -> bool:
    return name in TOOL_NAMES


def public_bot(*, bot_id: int, username: str | None, chat_id: int | None) -> dict[str, object]:
    """The agent response. The bot token is not a field."""
    return {
        "id": bot_id,
        "username": username,
        "connected": chat_id is not None,
    }


def _same(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(
        hashlib.sha256(left.encode()).digest(),
        hashlib.sha256(right.encode()).digest(),
    )


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return digest.hex()[:32]


def _encode_token(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_token(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + padding).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionError("Session is invalid or expired.") from exc


def expires_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
