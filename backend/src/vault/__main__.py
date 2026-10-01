"""Run the server: ``python -m vault`` (it listens on 127.0.0.1 only, see config)."""

import uvicorn

from vault.core.config import load_settings
from vault.main import create_app


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    # log_config=None keeps our logging setup (uvicorn would replace it).
    uvicorn.run(app, host=settings.server.host, port=settings.server.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    main()
