"""Console entry point: ``python -m collapsarr`` (and the ``collapsarr`` script).

Starts the ASGI server bound to the configured host/port.
"""

from __future__ import annotations

from .config import get_settings


def main() -> None:
    """Run the Collapsarr API server using the configured host and port."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "collapsarr.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
