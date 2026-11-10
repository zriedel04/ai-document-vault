"""Run the server: ``python -m vault`` (it listens on 127.0.0.1 only, see config)."""

from typing import Final

import uvicorn

from vault.core.config import load_settings
from vault.main import create_app

# Once asked to stop (``POST /api/shutdown``, or Ctrl+C), how long uvicorn lets open requests
# finish before it closes them; the lifespan's shutdown, which gives running jobs
# ``vault.main.WORKER_STOP_SECONDS``, follows. vault.ps1 waits longer than both together
# ($script:StopSeconds, kept above them by a test) before it ends the process instead.
SHUTDOWN_GRACE_SECONDS: Final = 5


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    # log_config=None keeps our logging setup (uvicorn would replace it). access_log=False because
    # the access log writes the request path and query, which can carry private text.
    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )
    server = uvicorn.Server(config)

    def request_shutdown() -> None:
        # What uvicorn's own signal handler does; its main loop sees it within a tenth of a second.
        server.should_exit = True

    app.state.request_shutdown = request_shutdown
    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
