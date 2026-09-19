"""Signed live-VNC sessions.

Telegram and the dashboard mint a URL here; the viewer at ``/live/...`` reverse-proxies
the slot's noVNC. Tokens are task-scoped and revocable so a public tunnel is not an
open remote desktop.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings

COOKIE_NAME = "cm_live"
VIEWER_QUERY = "autoconnect=1&resize=scale&reconnect=1&show_dot=1"


@dataclass(frozen=True)
class LiveSession:
    task_id: int
    slot_id: int
    expires_at: int
    jti: str
    token: str

    @property
    def expired(self) -> bool:
        return int(time.time()) >= self.expires_at


class LiveError(ValueError):
    """Raised when a live session cannot be minted or verified."""


class LiveSessions:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._sessions: dict[int, LiveSession] = {}
        self._by_jti: dict[str, int] = {}
        self._revoked_jtis: set[str] = set()

    def public_base_url(self) -> str:
        base = self.settings.public_base_url.strip().rstrip("/")
        if base:
            return base
        return f"http://{self.settings.host}:{self.settings.port}"

    def mint(self, task_id: int, slot_id: int) -> dict[str, object]:
        existing = self._sessions.get(task_id)
        if (
            existing is not None
            and not existing.expired
            and existing.slot_id == slot_id
        ):
            return self._public(existing)
        if existing is not None:
            self.revoke(task_id)
        session = self._issue(task_id, slot_id)
        self._sessions[task_id] = session
        self._by_jti[session.jti] = task_id
        return self._public(session)

    def inspect(self, task_id: int) -> dict[str, object] | None:
        session = self._sessions.get(task_id)
        if session is None or session.expired:
            if session is not None:
                self.revoke(task_id)
            return None
        return self._public(session)

    def revoke(self, task_id: int) -> bool:
        session = self._sessions.pop(task_id, None)
        if session is None:
            return False
        self._by_jti.pop(session.jti, None)
        self._revoked_jtis.add(session.jti)
        return True

    def verify_token(self, token: str, *, slot_id: int | None = None) -> LiveSession:
        payload = _decode_token(token)
        parts = payload.split(":")
        if len(parts) != 5:
            raise LiveError("Malformed live token.")
        task_s, slot_s, exp_s, jti, sig = parts
        expected = _sign(self._secret, f"{task_s}:{slot_s}:{exp_s}:{jti}")
        if not hmac.compare_digest(sig, expected):
            raise LiveError("Invalid live token.")
        try:
            task_id = int(task_s)
            parsed_slot = int(slot_s)
            expires_at = int(exp_s)
        except ValueError as exc:
            raise LiveError("Malformed live token.") from exc
        if slot_id is not None and parsed_slot != slot_id:
            raise LiveError("Live token does not match this browser slot.")
        if jti in self._revoked_jtis:
            raise LiveError("This live link has been revoked. Send /watch in Telegram for a new one.")
        if int(time.time()) >= expires_at:
            self._revoked_jtis.add(jti)
            stored = self._sessions.get(task_id)
            if stored is not None and stored.jti == jti:
                self.revoke(task_id)
            raise LiveError("This live link has expired. Send /watch in Telegram for a new one.")
        stored = self._sessions.get(task_id)
        if stored is not None and stored.jti == jti:
            return stored
        session = LiveSession(
            task_id=task_id,
            slot_id=parsed_slot,
            expires_at=expires_at,
            jti=jti,
            token=token,
        )
        previous = self._sessions.get(task_id)
        if previous is not None and previous.jti != jti:
            self._by_jti.pop(previous.jti, None)
        self._sessions[task_id] = session
        self._by_jti[jti] = task_id
        return session

    def viewer_path(self, task_id: int, token: str) -> str:
        return f"/live/{task_id}?t={token}"

    def desktop_upstream(self) -> tuple[str, int]:
        host = self.settings.desktop_vnc_host.strip() or "127.0.0.1"
        return host, int(self.settings.desktop_vnc_port)

    def slot_upstream(self, slot_id: int) -> tuple[str, int]:
        if slot_id < 1 or slot_id > self.settings.browser_slot_count:
            raise LiveError(f"Unknown browser slot {slot_id}.")
        port = self.settings.browser_novnc_base_port + slot_id - 1
        return self.settings.browser_host, port

    def cookie_secure(self) -> bool:
        return self.public_base_url().startswith("https://")

    def _issue(self, task_id: int, slot_id: int) -> LiveSession:
        expires_at = int(time.time()) + self.settings.live_link_ttl_seconds
        jti = secrets.token_urlsafe(8)
        sig = _sign(self._secret, f"{task_id}:{slot_id}:{expires_at}:{jti}")
        token = _encode_token(f"{task_id}:{slot_id}:{expires_at}:{jti}:{sig}")
        return LiveSession(
            task_id=task_id,
            slot_id=slot_id,
            expires_at=expires_at,
            jti=jti,
            token=token,
        )

    def _public(self, session: LiveSession) -> dict[str, object]:
        return {
            "task_id": session.task_id,
            "slot_id": session.slot_id,
            "expires_at": session.expires_at,
            "url": f"{self.public_base_url()}{self.viewer_path(session.task_id, session.token)}",
        }

    @property
    def _secret(self) -> str:
        secret = self.settings.live_link_secret.strip()
        if secret:
            return secret
        token = self.settings.telegram_bot_token.strip()
        if token:
            return token
        return "dev-live-link-secret"


async def open_live_session(
    task_id: int,
    *,
    runner: Any,
    db: Any,
    sessions: LiveSessions,
) -> dict[str, object]:
    """Mint a signed viewer URL for this Mac. A Docker Chrome slot is optional."""
    task = await db.get_task(task_id) if task_id else None
    slot = runner.slot_for_task(task) if task else None
    slot_id = int(slot.id) if slot is not None else 0
    if task is not None:
        await runner.pause_for_live(int(task["id"]))
        return sessions.mint(int(task["id"]), slot_id)
    return sessions.mint(0, slot_id)


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
        raise LiveError("Malformed live token.") from exc
