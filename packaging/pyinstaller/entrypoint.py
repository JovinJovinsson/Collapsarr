"""PyInstaller entry point wrapping ``collapsarr.__main__:main`` (COL-216).

PyInstaller's :class:`Analysis` needs a runnable *script* to start tracing
imports from, not a ``module:function`` target the way ``console_scripts``
does -- so this thin wrapper is what ``collapsarr.spec`` points at. It is
deliberately trivial: all real startup logic stays in
``collapsarr.__main__.main`` (imported here, not reimplemented), so this file
never needs to change when that logic does.
"""

from __future__ import annotations

from collapsarr.__main__ import main

if __name__ == "__main__":
    main()
