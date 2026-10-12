"""Process entry shim.

The service runs as a normal ASGI process::

    uv run python src/app.py
"""

from app import app, main

__all__ = ["app", "main"]


if __name__ == "__main__":
    main()
