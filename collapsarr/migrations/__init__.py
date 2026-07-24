"""Alembic migration package for Collapsarr (COL-57).

Ships *inside* the installed package so the migration environment (``env.py``),
the revision template (``script.py.mako``), and the versioned migrations under
``versions/`` are all present in the built wheel / Docker image — not just in a
source checkout. This module also builds the runtime Alembic
:class:`~alembic.config.Config` programmatically from :class:`Settings`, so the
app never reads an ``alembic.ini`` at runtime (the repo-root ``alembic.ini`` is
an authoring-only convenience for ``alembic revision --autogenerate`` /
``alembic history``).

SQLite is the only supported and tested backend; see ``README.md`` in this
directory for the migration-authoring workflow.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from collapsarr.config import Settings

#: Absolute path to this migrations directory. Doubles as Alembic's
#: ``script_location`` at runtime — resolved from ``__file__`` so it points at
#: the *installed* location inside the wheel, not a repo-relative path.
MIGRATIONS_DIR = Path(__file__).resolve().parent


def build_alembic_config(settings: Settings) -> Config:
    """Build a runtime Alembic :class:`~alembic.config.Config` from ``settings``.

    No ``alembic.ini`` is read: ``script_location`` is pinned to this packaged
    directory and ``sqlalchemy.url`` comes from
    :attr:`Settings.sqlalchemy_url`. This is the Config the application (COL-58
    onward) will hand to ``alembic.command.upgrade(config, "head")``.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url)
    return config


__all__ = ["MIGRATIONS_DIR", "build_alembic_config"]
