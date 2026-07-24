"""Authentication surface: first-run setup, Forms login, session enforcement.

The full auth model (COL-50): a single operator credential (COL-49) gates the
whole UI behind a signed-cookie session, while ``/api`` still accepts the
Sonarr/Radarr-style API key for webhooks and tooling. See the submodules:

* :mod:`~collapsarr.auth.session` -- the signed-cookie session middleware and
  its login/logout helpers.
* :mod:`~collapsarr.auth.enforcement` -- the request-gating middleware that
  supersedes the old opt-in ``api_key_middleware``.
* :mod:`~collapsarr.auth.routes` -- the ``/api/auth`` endpoints.
"""

from __future__ import annotations

from .enforcement import enforce_auth_middleware
from .routes import router as auth_router
from .session import SessionMiddleware

__all__ = ["SessionMiddleware", "auth_router", "enforce_auth_middleware"]
