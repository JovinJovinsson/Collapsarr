# PyInstaller --onedir build recipe for Collapsarr (COL-216).
#
# This is the *shared* recipe COL-220 (macOS) and COL-221 (Windows) reuse
# as-is: nothing in this file is Linux-specific. Each platform's CI job
# installs the frontend-bundled wheel (the same one `build-wheel` in
# release.yml produces) into its Python environment, then runs
#
#     pyinstaller packaging/pyinstaller/collapsarr.spec
#
# (or `python packaging/pyinstaller/build.py`, which wraps the same
# invocation). PyInstaller resolves per-platform packaging differences
# (executable extension, binary format, etc.) itself.
#
# Data files are collected from the *installed* collapsarr package location
# (via importlib.metadata -- see _collapsarr_package_dir's docstring for why
# not importlib.util.find_spec), never a repo-relative path -- this spec
# works identically whether collapsarr was installed into a throwaway venv,
# a CI runner's site-packages, or a platform-specific virtualenv on a macOS
# or Windows runner. It deliberately requires the *standard* wheel (built via
# `python -m build` after `npm run build`), not an editable install: an
# editable install has no bundled `collapsarr/static` (see hatch_build.py),
# so PyInstaller would happily produce a binary with no frontend and no
# useful error until someone opened a browser to it.
#
# `collapsarr/migrations` needs to ship as *data*, not be relied on to appear
# via PyInstaller's static import analysis: Alembic's ScriptDirectory loads
# env.py and every versions/*.py file straight off disk at runtime
# (importlib.util.spec_from_file_location, not a Python `import` statement),
# which PyInstaller's modulegraph cannot see. Onedir mode keeps the bundle's
# files on a real filesystem next to the executable, so Alembic can list and
# load them exactly as it does from an installed wheel.

from __future__ import annotations

from importlib import metadata
from pathlib import Path

# PyInstaller conventionally relies on `Analysis`/`PYZ`/`EXE`/`COLLECT` being
# injected into a spec file's globals when PyInstaller execs it, rather than
# importing them explicitly -- which leaves the file un-lintable (every use
# reads as an undefined name to ruff/mypy). Importing them here instead makes
# this file ordinary, statically-analyzable Python (see
# packaging/pyinstaller/README notes in pyproject.toml's mypy override for
# why `PyInstaller.*` itself is exempted from import resolution rather than
# added as a real runtime/dev dependency) while behaving identically: these
# are the exact same classes PyInstaller would have injected.
from PyInstaller.building.api import COLLECT
from PyInstaller.building.build_main import EXE, PYZ, Analysis
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# `SPECPATH` has no importable equivalent -- PyInstaller injects it into the
# spec's exec globals as the directory this .spec file lives in (regardless
# of the caller's cwd), computed only at build time from the spec file's own
# path. Unlike Analysis/PYZ/EXE/COLLECT above, there is nothing to import.
SPEC_DIR = Path(SPECPATH)  # type: ignore[name-defined]  # noqa: F821
ENTRYPOINT = SPEC_DIR / "entrypoint.py"


def _collapsarr_package_dir() -> Path:
    """Locate the *installed* ``collapsarr`` package's directory on disk.

    Deliberately uses ``importlib.metadata`` (keyed on the installed
    distribution's ``.dist-info``) rather than ``importlib.util.find_spec``
    (keyed on ``sys.path`` package-name resolution): PyInstaller is invoked
    from the repo root in CI, and ``python -m PyInstaller`` prepends the
    current directory to ``sys.path`` -- which would make
    ``find_spec("collapsarr")`` resolve to this checkout's *source* tree
    (no bundled ``collapsarr/static``, no installed wheel) instead of the
    actually-installed frontend-bundled wheel sitting in site-packages. A
    bare source checkout has no ``collapsarr-*.dist-info``, so
    ``importlib.metadata`` can't make that mistake -- it only ever resolves
    to a real installed distribution.
    """
    try:
        dist = metadata.distribution("collapsarr")
    except metadata.PackageNotFoundError:
        raise SystemExit(
            "collapsarr is not installed in this environment -- install the "
            "frontend-bundled wheel (python -m build, after npm run build) "
            "before running PyInstaller. See release.yml's "
            "native-build-linux job."
        ) from None
    init_file = dist.locate_file("collapsarr/__init__.py")
    return Path(str(init_file)).resolve().parent


PACKAGE_DIR = _collapsarr_package_dir()
STATIC_DIR = PACKAGE_DIR / "static"
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"

if not STATIC_DIR.is_dir():
    raise SystemExit(
        f"{STATIC_DIR} not found -- the installed collapsarr wheel must be "
        "the frontend-bundled standard wheel, not an editable/source "
        "install (see hatch_build.py)."
    )
if not MIGRATIONS_DIR.is_dir():
    raise SystemExit(f"{MIGRATIONS_DIR} not found -- collapsarr installation looks incomplete.")

datas = [
    (str(STATIC_DIR), "collapsarr/static"),
    (str(MIGRATIONS_DIR), "collapsarr/migrations"),
]

# uvicorn/alembic both resolve some submodules dynamically at runtime
# (uvicorn's "auto" loop/http-protocol selection, alembic's command plugins),
# which PyInstaller's static import graph can't see from the entry point
# alone. collect_submodules pulls in every submodule of each package so the
# frozen binary has whichever implementation gets selected at runtime,
# regardless of which one that turns out to be on a given platform.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("alembic")
    + [
        # Reached only via the string "collapsarr.main:app" that
        # collapsarr.__main__.main() hands to uvicorn.run() -- a dynamic
        # (importlib) import PyInstaller's static analysis can't follow, so
        # it must be named explicitly. Once included, collapsarr.main's own
        # top-level imports (arr/, jobs/, health/, ... routers) are ordinary
        # static imports, so PyInstaller's normal analysis bundles the rest
        # of the application automatically from here.
        "collapsarr.main",
    ]
)

a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="collapsarr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="collapsarr",
)
