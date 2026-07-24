"""HTTP endpoints for the Forms auth flow (COL-50).

A thin ``/api/auth`` router over :mod:`collapsarr.settings.service` (the
credential store, COL-49) and :mod:`collapsarr.auth.session` (the signed-cookie
session):

* ``GET  /api/auth/status`` -- whether first-run setup is still needed and
  whether the caller is logged in; the SPA reads this to decide what to render.
* ``POST /api/auth/setup``  -- first-run only: create the single credential and
  log the new operator straight in. Rejected once a credential exists.
* ``POST /api/auth/login``  -- verify the credential and open a session; the
  ``remember`` flag selects a long-lived vs browser-session cookie.
* ``POST /api/auth/logout`` -- clear the session.

``status``/``setup``/``login`` are reachable without a session (the enforcement
middleware opens them); ``logout`` requires one. Only the Forms flow is wired
as an ``/api/auth`` endpoint here -- Basic auth (COL-52) doesn't need one: it
authenticates directly in :mod:`collapsarr.auth.enforcement` against the same
credential this module manages, challenging with ``WWW-Authenticate`` instead
of a JSON login call. ``AuthStatus.auth_method`` (added by COL-52) reports
which method is active so the frontend (the Login page) can adapt --
"remember me" is a Forms-only concept. ``setup`` leaves ``auth_required`` and
``auth_method`` untouched (it does not force either), so the modes the row
already carries -- ``local_bypass``/``forms`` by default (COL-51/COL-52) --
survive completing first-run setup; switch them from Settings.

``change-password`` and ``logout-everywhere`` (COL-55) let the Settings page
manage the credential in-app, without editing the database directly: the
former verifies the current password before re-hashing a new one over the
same PBKDF2 core (COL-49); the latter rotates ``session_secret``, which
invalidates every signed-cookie session -- on this browser and any other --
since :class:`~collapsarr.auth.session.SessionMiddleware` signs and verifies
cookies against that value. Neither is in the enforcement middleware's
``OPEN_API_PATHS`` (unlike ``setup``/``login``/``status``), so both require an
existing session or the API key, same as ``logout``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_session
from ..settings.models import AUTH_METHOD_FORMS
from ..settings.routes import AuthMethodMode
from ..settings.service import (
    get_global_settings,
    rotate_session_secret,
    update_global_settings,
    verify_auth_password,
)
from .session import is_authenticated, log_in, log_out, sync_cached_secret

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --- schemas -----------------------------------------------------------------


class AuthStatus(BaseModel):
    """Whether the install still needs setup, whether this caller is in, and
    which auth method (``forms``|``basic``, COL-52) is active."""

    needs_setup: bool
    authenticated: bool
    auth_method: AuthMethodMode


class SetupRequest(BaseModel):
    """First-run credential: the single operator username + password."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginRequest(BaseModel):
    """A login attempt; ``remember`` opts into a long-lived cookie."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    remember: bool = False


class ChangePasswordRequest(BaseModel):
    """A password-change attempt (COL-55): the current password (verified
    before the change is applied) and the new one to set."""

    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


# --- endpoints ---------------------------------------------------------------


@router.get("/status", response_model=AuthStatus)
def auth_status(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    """Report first-run and session state for the client to branch on."""
    settings = get_global_settings(session)
    return AuthStatus(
        needs_setup=settings.auth_username is None,
        authenticated=is_authenticated(request),
        auth_method=settings.auth_method,
    )


@router.post("/setup", response_model=AuthStatus)
def setup(
    body: SetupRequest, request: Request, session: Session = Depends(get_session)
) -> AuthStatus:
    """Create the single credential on first run and log the operator in.

    Rejected with ``409`` once a credential already exists, so the first-run
    gate can only ever be closed once. Deliberately does not pass
    ``auth_required``: whatever required-mode the row already carries
    (``local_bypass`` by default, COL-51) is left as-is, so completing setup
    never silently promotes a fresh install to ``enabled``.
    """
    settings = get_global_settings(session)
    if settings.auth_username is not None:
        raise HTTPException(status_code=409, detail="Setup has already been completed.")

    update_global_settings(
        session,
        auth_username=body.username,
        password=body.password,
        auth_method=AUTH_METHOD_FORMS,
    )
    log_in(request, body.username, remember=False)
    return AuthStatus(needs_setup=False, authenticated=True, auth_method=AUTH_METHOD_FORMS)


@router.post("/login", response_model=AuthStatus)
def login(
    body: LoginRequest, request: Request, session: Session = Depends(get_session)
) -> AuthStatus:
    """Authenticate the credential and open a session cookie.

    ``409`` before any credential exists (setup must run first); ``401`` on a
    bad username or password.
    """
    settings = get_global_settings(session)
    if settings.auth_username is None:
        raise HTTPException(
            status_code=409, detail="No credential configured; complete setup first."
        )
    if body.username != settings.auth_username or not verify_auth_password(
        session, body.password
    ):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    log_in(request, body.username, remember=body.remember)
    return AuthStatus(needs_setup=False, authenticated=True, auth_method=settings.auth_method)


@router.post("/logout", response_model=AuthStatus)
def logout(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    """Clear the session; the response discards the cookie."""
    log_out(request)
    settings = get_global_settings(session)
    return AuthStatus(
        needs_setup=settings.auth_username is None,
        authenticated=False,
        auth_method=settings.auth_method,
    )


@router.post("/change-password", response_model=AuthStatus)
def change_password(
    body: ChangePasswordRequest, request: Request, session: Session = Depends(get_session)
) -> AuthStatus:
    """Rotate the operator's password from Settings (COL-55).

    Requires the *current* password to verify (via
    :func:`~collapsarr.settings.service.verify_auth_password`) before applying
    the change -- ``401`` on a mismatch, so knowing a session or the API key
    alone isn't enough to take over the credential. ``409`` before any
    credential exists (mirrors ``login``/``setup``: there is nothing to
    change yet). The new password is re-hashed through the same PBKDF2 core
    (:func:`~collapsarr.settings.service.update_global_settings`'s
    ``password`` kwarg); the plaintext is never persisted. Does not touch
    ``session_secret`` -- this browser's (and any other open) session stays
    valid; only ``logout-everywhere`` below invalidates sessions.
    """
    settings = get_global_settings(session)
    if settings.auth_username is None:
        raise HTTPException(
            status_code=409, detail="No credential configured; complete setup first."
        )
    if not verify_auth_password(session, body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    update_global_settings(session, password=body.new_password)
    return AuthStatus(
        needs_setup=False,
        authenticated=is_authenticated(request),
        auth_method=settings.auth_method,
    )


@router.post("/logout-everywhere", response_model=AuthStatus)
def logout_everywhere(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    """Rotate ``session_secret``, invalidating every signed-cookie session --
    including this one (COL-55).

    :func:`~collapsarr.settings.service.rotate_session_secret` mints and
    persists a fresh secret; :func:`~collapsarr.auth.session.sync_cached_secret`
    then pushes it into this process's ``app.state`` cache so the invalidation
    takes effect immediately rather than after a restart (see that function's
    docstring). Every cookie signed under the old secret -- on this browser
    and any other -- fails to unsign on its next request and is treated as
    logged out; this response also proactively clears the current browser's
    cookie via :func:`~collapsarr.auth.session.log_out` rather than leaving it
    to fail lazily on the next request.
    """
    settings = rotate_session_secret(session)
    assert settings.session_secret is not None  # rotate_session_secret always sets one
    sync_cached_secret(request.app, settings.session_secret)
    log_out(request)
    return AuthStatus(needs_setup=False, authenticated=False, auth_method=settings.auth_method)
