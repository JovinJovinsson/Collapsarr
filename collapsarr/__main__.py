"""Console entry point: ``python -m collapsarr`` (and the ``collapsarr`` script).

Starts the ASGI server bound to the configured host/port -- unless invoked in
the native self-update "finish-update" mode (COL-235), in which case it runs
the staged handoff (wait for the old PID, atomically swap the install dir, then
re-exec back into this same entry point *without* the flag, coming up as an
ordinary server) instead of starting the server itself.
"""

from __future__ import annotations

import sys

from .config import get_settings


def main() -> None:
    """Run the Collapsarr API server, or the native self-update handoff (COL-235).

    A finish-update invocation (recognised by
    :func:`~collapsarr.self_update.native.parse_finish_update_argv` on
    ``sys.argv``) is handled *before* any server setup: the handoff process
    waits, swaps, and re-execs, and only the re-exec'd process (which no longer
    carries the flag) falls through to start the server. Any ordinary launch
    parses to ``None`` and proceeds straight to ``uvicorn`` as before.
    """
    from .self_update.native import parse_finish_update_argv, run_finish_update

    finish_args = parse_finish_update_argv(sys.argv[1:])
    if finish_args is not None:
        run_finish_update(finish_args)
        return

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
