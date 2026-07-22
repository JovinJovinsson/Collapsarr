"""Bundle the pre-built frontend (frontend/dist) into the standard wheel only
(COL-40). Registered under the wheel target only — see pyproject.toml.

Editable installs (`pip install -e`, used by CI test jobs and local dev)
never need the frontend bundled — requiring frontend/dist to exist for those
would break `pip install -e ".[dev]"` on a checkout that hasn't run
`npm run build` yet. Standard wheel builds (the Dockerfile, `python -m
build`, the release pipeline) still fail loudly if frontend/dist is missing,
since that indicates a real packaging bug.

Scoped to the wheel target specifically (not top-level/all-targets) because
hatchling reserves any path handed to `force_include`, which would otherwise
silently prune frontend/dist out of the sdist's *natural* inclusion (already
handled there by the `artifacts` glob in pyproject.toml) without actually
force-including it under the sdist's file layout.
"""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if version != "standard":
            return
        dist = Path(self.root) / "frontend" / "dist"
        if not dist.is_dir():
            msg = f"{dist} not found — run `npm run build` in frontend/ before building the wheel."
            raise FileNotFoundError(msg)
        build_data.setdefault("force_include", {})["frontend/dist"] = "collapsarr/static"
