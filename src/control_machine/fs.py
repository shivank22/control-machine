"""Host-filesystem jail and Deep Agents FilesystemBackend.

This Mac's home is mounted at ``/home`` for the built-in ``ls`` / ``read_file``
tools. Scratch files stay in LangGraph state; ``/memories/`` stays in Postgres.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from deepagents import FilesystemPermission
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import FileInfo, GlobResult, GrepMatch, GrepResult, LsResult

from .config import PROJECT_ROOT, Settings

# Virtual mount for this Mac's home. Tools see /home/Documents, not /Users/...
HOME_MOUNT = "/home"

_DEFAULT_DIR_DENY = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".config/gcloud",
    "Library/Keychains",
    "Library/Cookies",
    "Library/Mail",
    "Library/Accounts",
    "Library/IdentityServices",
)

_DEFAULT_NAME_DENY = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        ".netrc",
        ".pgpass",
        "credentials.json",
        "service-account.json",
    }
)

_DEFAULT_SUFFIX_DENY = frozenset({".pem", ".p12", ".pfx", ".key", ".ovpn"})


class FsError(ValueError):
    """Raised when a path is outside the jail or otherwise unusable."""


def filesystem_root(settings: Settings) -> Path:
    raw = settings.filesystem_root.strip()
    root = Path(raw).expanduser() if raw else Path.home()
    return root.resolve()


def inbox_dir(settings: Settings) -> Path:
    path = filesystem_root(settings) / "Downloads" / "control-machine"
    path.mkdir(parents=True, exist_ok=True)
    return path


def extra_deny_prefixes(settings: Settings) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in settings.filesystem_deny.split(",")
        if part.strip()
    )


def host_path_from_virtual(raw: str, *, root: Path, extra_deny: tuple[str, ...] = ()) -> Path | None:
    """Map a Deep Agents path to a real jail path, or None if it is not under /home."""
    text = (raw or "").strip()
    if not text:
        raise FsError("Path is required.")
    if text == HOME_MOUNT or text == f"{HOME_MOUNT}/":
        rel = "."
    elif text.startswith(f"{HOME_MOUNT}/"):
        rel = text[len(HOME_MOUNT) + 1 :]
    elif text.startswith("/"):
        return None
    else:
        rel = text
    return resolve_user_path(rel, root=root, extra_deny=extra_deny)


def host_path_exists(raw: str, settings: Settings) -> bool:
    """True when a virtual or relative path is an existing file inside the jail."""
    try:
        target = host_path_from_virtual(
            raw,
            root=filesystem_root(settings),
            extra_deny=extra_deny_prefixes(settings),
        )
    except FsError:
        return False
    return target is not None and target.exists()


def filesystem_permissions(settings: Settings) -> list[FilesystemPermission]:
    """Deny Deep Agents file tools on secret locations under /home."""
    paths: list[str] = []
    for prefix in (*_DEFAULT_DIR_DENY, *extra_deny_prefixes(settings)):
        mounted = f"{HOME_MOUNT}/{prefix.strip('/')}"
        paths.extend((mounted, f"{mounted}/**"))
    for name in sorted(_DEFAULT_NAME_DENY):
        paths.extend((f"{HOME_MOUNT}/{name}", f"{HOME_MOUNT}/**/{name}"))
    for suffix in sorted(_DEFAULT_SUFFIX_DENY):
        paths.extend((f"{HOME_MOUNT}/*{suffix}", f"{HOME_MOUNT}/**/*{suffix}"))
    return [
        FilesystemPermission(
            operations=["read", "write"],
            paths=paths,
            mode="deny",
        )
    ]


def build_agent_backend(settings: Settings) -> CompositeBackend:
    """State for scratch files, Postgres for /memories/, disk jail for /home/."""
    max_mb = max(1, (settings.filesystem_read_max_bytes + 1_048_575) // 1_048_576)
    host = JailedFilesystemBackend(
        root_dir=filesystem_root(settings),
        virtual_mode=True,
        extra_deny=extra_deny_prefixes(settings),
        max_file_size_mb=max_mb,
    )
    return CompositeBackend(
        default=StateBackend(),
        routes={
            f"{HOME_MOUNT}/": host,
            "/memories/": StoreBackend(namespace=lambda _rt: ("memories",)),
        },
    )


class JailedFilesystemBackend(FilesystemBackend):
    """FilesystemBackend that also blocks the control-machine secret deny-list."""

    def __init__(self, *args: object, extra_deny: tuple[str, ...] = (), **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._extra_deny = extra_deny

    def _resolve_path(self, key: str) -> Path:
        full = super()._resolve_path(key)
        if _is_denied(full, root=self.cwd, extra_deny=self._extra_deny):
            raise PermissionError("That path is blocked.")
        return full

    def ls(self, path: str) -> LsResult:
        result = super().ls(path)
        if not result.entries:
            return result
        return replace(
            result,
            entries=[info for info in result.entries if not self._blocked_info(info)],
        )

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:  # type: ignore[override]
        result = super().glob(pattern, path)
        if not result.matches:
            return result
        return replace(
            result,
            matches=[info for info in result.matches if not self._blocked_info(info)],
        )

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        result = super().grep(
            pattern,
            path,
            glob,
            max_count=max_count,
            context_lines=context_lines,
        )
        if not result.matches:
            return result
        return replace(
            result,
            matches=[match for match in result.matches if not self._blocked_grep(match)],
        )

    def _blocked_info(self, info: FileInfo) -> bool:
        path = info.get("path") if isinstance(info, dict) else getattr(info, "path", "")
        return self._blocked(str(path or ""))

    def _blocked_grep(self, match: GrepMatch) -> bool:
        path = match.get("path") if isinstance(match, dict) else getattr(match, "path", "")
        return self._blocked(str(path or ""))

    def _blocked(self, path: str) -> bool:
        if not path:
            return False
        try:
            self._resolve_path(path)
        except (OSError, ValueError):
            return True
        return False


def resolve_user_path(raw: str, *, root: Path, extra_deny: tuple[str, ...] = ()) -> Path:
    """Resolve ``raw`` against ``root`` and reject escapes and secrets."""
    text = (raw or "").strip()
    if not text:
        raise FsError("Path is required.")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FsError("That path is outside the allowed filesystem root.") from exc
    if _is_denied(resolved, root=root, extra_deny=extra_deny):
        raise FsError("That path is blocked.")
    return resolved


def _is_denied(path: Path, *, root: Path, extra_deny: tuple[str, ...]) -> bool:
    if path == (PROJECT_ROOT / ".env").resolve():
        return True
    name = path.name.lower()
    if name in {n.lower() for n in _DEFAULT_NAME_DENY}:
        return True
    if path.suffix.lower() in _DEFAULT_SUFFIX_DENY:
        return True
    rel = path.relative_to(root).as_posix()
    prefixes = [item.rstrip("/") for item in _DEFAULT_DIR_DENY]
    prefixes.extend(item.rstrip("/") for item in extra_deny)
    for prefix in prefixes:
        if rel == prefix or rel.startswith(f"{prefix}/"):
            return True
    return False
