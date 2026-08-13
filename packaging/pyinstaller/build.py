"""Run the PyInstaller ``--onedir`` build for the installed collapsarr wheel.

Shared entry point every platform's native-build CI job invokes identically
(COL-216 Linux amd64/arm64; COL-220 macOS; COL-221 Windows): each job just
installs the frontend-bundled wheel + ``pyinstaller`` into its Python
environment, then runs::

    python packaging/pyinstaller/build.py

No OS-specific branching lives here -- PyInstaller and ``collapsarr.spec``
(which this wraps) handle per-platform packaging differences themselves. The
output lands in ``packaging/pyinstaller/dist/collapsarr/`` (the ``--onedir``
folder containing the executable and its ``_internal`` support files) unless
overridden.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
SPEC_FILE = PACKAGING_DIR / "collapsarr.spec"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist-dir",
        default=str(PACKAGING_DIR / "dist"),
        help="Where PyInstaller writes the --onedir output (default: packaging/pyinstaller/dist).",
    )
    parser.add_argument(
        "--work-dir",
        default=str(PACKAGING_DIR / "build"),
        help="PyInstaller's scratch work directory (default: packaging/pyinstaller/build).",
    )
    args = parser.parse_args()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--noconfirm",
        "--clean",
        "--distpath",
        args.dist_dir,
        "--workpath",
        args.work_dir,
    ]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
