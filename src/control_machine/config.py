"""Runtime configuration, loaded from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = PROJECT_ROOT / "runs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://postgres:postgres@127.0.0.1:5442/control_machine?sslmode=disable"

    # openai | ollama | auto (openai when OPENAI_API_KEY is set, else ollama)
    llm_provider: str = "auto"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    openai_base_url: str = ""

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma4:latest"

    # TypeSafe Jev chooses each browser action. Empty disables browser_drive.
    typesafe_api_key: str = ""

    browser_slot_count: int = Field(default=2, ge=1)
    browser_cdp_base_port: int = 9231
    browser_novnc_base_port: int = 6081
    browser_host: str = "127.0.0.1"
    max_parallel_runs: int = Field(default=2, ge=1)
    browser_session_idle_seconds: int = Field(default=900, ge=30)

    host: str = "127.0.0.1"
    port: int = 8100
    public_base_url: str = ""
    live_link_secret: str = ""
    live_link_ttl_seconds: int = Field(default=86400, ge=60)

    telegram_bot_token: str = ""
    telegram_allowlist: str = ""
    telegram_notify_chat_id: str = ""

    max_steps: int = 40
    run_timeout_seconds: int = 600

    # Host filesystem. Empty root means the current user's home directory.
    filesystem_root: str = ""
    filesystem_deny: str = ""
    filesystem_read_max_bytes: int = Field(default=256_000, ge=1024)
    filesystem_write_max_bytes: int = Field(default=1_000_000, ge=1024)
    filesystem_download_max_bytes: int = Field(default=20_000_000, ge=1024)

    # This Mac's Screen Sharing / VNC (System Settings → Sharing).
    desktop_vnc_host: str = "127.0.0.1"
    desktop_vnc_port: int = Field(default=5900, ge=1, le=65535)

    # Comma-separated domain suffixes that need no approval. Empty means "all allowed".
    domain_allowlist: str = ""

    # Snapshot budget. Small local models drown in long accessibility trees.
    snapshot_max_chars: int = 6000
    # Screenshots are downscaled before they reach the model.
    screenshot_max_width: int = 900
    screenshot_quality: int = 70

    # Local traces and errors. Empty log_dir means <project>/logs.
    log_dir: str = ""
    log_level: str = "INFO"

    @property
    def allowed_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.domain_allowlist.split(",") if d.strip()]

    @property
    def telegram_user_ids(self) -> set[int]:
        ids: set[int] = set()
        for part in self.telegram_allowlist.split(","):
            part = part.strip()
            if part:
                ids.add(int(part))
        return ids

    @property
    def notify_chat_id(self) -> int | None:
        raw = self.telegram_notify_chat_id.strip()
        return int(raw) if raw else None

    @property
    def resolved_llm_provider(self) -> str:
        provider = self.llm_provider.strip().lower()
        if provider in {"openai", "ollama"}:
            return provider
        return "openai" if self.openai_api_key.strip() else "ollama"

    @property
    def llm_model(self) -> str:
        if self.resolved_llm_provider == "openai":
            return self.openai_model
        return self.ollama_model

    @property
    def llm_configured(self) -> bool:
        if self.resolved_llm_provider == "openai":
            return bool(self.openai_api_key.strip())
        return True

    @property
    def runs_dir(self) -> Path:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        return RUNS_DIR

    @property
    def log_path(self) -> Path:
        raw = self.log_dir.strip()
        path = Path(raw).expanduser() if raw else PROJECT_ROOT / "logs"
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def traces_dir(self) -> Path:
        dest = self.log_path / "traces"
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    @property
    def effective_parallel_runs(self) -> int:
        return max(1, min(self.max_parallel_runs, self.browser_slot_count))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
