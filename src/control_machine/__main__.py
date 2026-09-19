"""Run the dashboard: `uv run control-machine`."""

from __future__ import annotations

import uvicorn

from .config import get_settings
from .tracing import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    # One worker on purpose: the run manager, browser pool and event bus are
    # process-local, so a second worker would stream events nobody can see.
    # log_config=None keeps the file handlers we just installed.
    uvicorn.run(
        "control_machine.web.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level=settings.log_level.lower(),
        log_config=None,
    )


if __name__ == "__main__":
    main()
