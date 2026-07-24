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
middleware opens them); ``logout`` requires one. Only the Forms method is
wired here -- Basic auth (COL-52) is a separate ticket. ``setup`` leaves
``auth_required`` untouched (it does not force ``enabled``), so the
required-mode the row already carries -- ``local_bypass`` by default (COL-51)
-- survives completing first-run setup; switch it from Settings.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_session
from ..settings.models import AUTH_METHOD_FORMS
from ..settings.service import (
    get_global_settings,
    update_global_settings,
    verify_auth_password,
)
from .session import is_authenticated, log_in, log_out

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --- schemas -----------------------------------------------------------------


class AuthStatus(BaseModel):
    """Whether the install still needs setup, and whether this caller is in."""

    needs_setup: bool
    authenticated: bool


class SetupRequest(BaseModel):
    """First-run credential: the single operator username + password."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginRequest(BaseModel):
    """A login attempt; ``remember`` opts into a long-lived cookie."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    remember: bool = False


# --- endpoints ---------------------------------------------------------------


@router.get("/status", response_model=AuthStatus)
def auth_status(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    """Report first-run and session state for the client to branch on."""
    settings = get_global_settings(session)
    return AuthStatus(
        needs_setup=settings.auth_username is None,
        authenticated=is_authenticated(request),
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
    return AuthStatus(needs_setup=False, authenticated=True)


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
    return AuthStatus(needs_setup=False, authenticated=True)


@router.post("/logout", response_model=AuthStatus)
def logout(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    """Clear the session; the response discards the cookie."""
    log_out(request)
    settings = get_global_settings(session)
    return AuthStatus(needs_setup=settings.auth_username is None, authenticated=False)
