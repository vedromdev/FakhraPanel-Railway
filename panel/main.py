"""Server entry point: ``python -m panel.main`` serves on 0.0.0.0:$PORT."""

import uvicorn

from panel.app import create_app
from panel.config import get_settings

settings = get_settings()
app = create_app(settings)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)


if __name__ == "__main__":
    main()
