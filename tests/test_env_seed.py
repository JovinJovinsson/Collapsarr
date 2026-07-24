"""Tests for environment-seeded UI credentials on headless deploys (COL-53).

Exercises :func:`collapsarr.settings.env_seed.seed_auth_from_env` directly at
the service layer (a schema-initialised session + a ``Settings`` instance
carrying the seed env vars -- the same construction pattern
``test_settings_service.py``'s ``_fresh_session`` helper uses), plus one
end-to-end check via ``create_app``/``TestClient`` that a seeded credential
actually skips the ``/setup`` gate COL-50 wires up (mirroring the
no-credential-yet redirect test in ``test_auth.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory, init_db
from collapsarr.main import create_app
from collapsarr.settings.env_seed import seed_auth_from_env
from collapsarr.settings.models import AUTH_METHOD_BASIC, AUTH_REQUIRED_ENABLED
from collapsarr.settings.service import (
    get_global_settings,
    update_global_settings,
    verify_auth_password,
)

USERNAME = "headless-admin"
PASSWORD = "s3cret-headless-pw"


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure a stray COLLAPSARR_AUTH_* in the ambient environment can't leak
    into a test that relies on Settings() defaulting to unset."""
    for var in (
        "COLLAPSARR_AUTH_USERNAME",
        "COLLAPSARR_AUTH_PASSWORD",
        "COLLAPSARR_AUTH_METHOD",
        "COLLAPSARR_AUTH_REQUIRED",
    ):
        monkeypatch.delenv(var, raising=False)


def _seeded_settings(tmp_path: Path, **overrides: object) -> Settings:
    """A Settings instance backed by a throwaway DB, with auth-seed fields set."""
    db_path = tmp_path / "collapsarr.db"
    return Settings(
        _env_file=None,
        database_path=str(db_path),
        auth_username=USERNAME,
        auth_password=PASSWORD,
        **overrides,  # type: ignore[arg-type]
    )


def _session_for(settings: Settings) -> Session:
    engine = create_engine_from_settings(settings)
    init_db(engine)
    return create_session_factory(engine)()


# ---------------------------------------------------------------------------
# Service-layer seeding behaviour.
# ---------------------------------------------------------------------------


def test_seeds_credential_when_env_vars_set_and_none_exists(tmp_path: Path) -> None:
    settings = _seeded_settings(tmp_path)
    session = _session_for(settings)

    result = seed_auth_from_env(session, settings)

    assert result is not None
    assert result.auth_username == USERNAME
    assert verify_auth_password(session, PASSWORD) is True


def test_seeded_password_is_hashed_not_plaintext(tmp_path: Path) -> None:
    settings = _seeded_settings(tmp_path)
    session = _session_for(settings)

    result = seed_auth_from_env(session, settings)

    assert result is not None
    assert result.auth_password_hash is not None
    assert PASSWORD not in result.auth_password_hash
    # A well-formed PBKDF2 encoded hash (scheme$iterations$salt$digest).
    scheme, iterations, salt_hex, digest_hex = result.auth_password_hash.split("$")
    assert scheme == "pbkdf2_sha512"
    assert int(iterations) > 0
    assert salt_hex and digest_hex


def test_does_nothing_when_env_vars_are_unset(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database_path=str(tmp_path / "collapsarr.db"))
    session = _session_for(settings)

    result = seed_auth_from_env(session, settings)

    assert result is None
    assert get_global_settings(session).auth_username is None


def test_does_not_overwrite_an_existing_credential_on_a_later_boot(tmp_path: Path) -> None:
    """Idempotent, first-boot-only seeding: even with the env vars still set,
    a later boot must not touch a credential that already exists -- including
    one the operator has since changed via /setup or Settings."""
    settings = _seeded_settings(tmp_path)
    session = _session_for(settings)

    # A credential already exists (e.g. set via /setup, then changed by the
    # operator) -- different from what the env vars carry.
    update_global_settings(session, auth_username="someone-else", password="a-different-password")

    result = seed_auth_from_env(session, settings)

    assert result is None
    settings_row = get_global_settings(session)
    assert settings_row.auth_username == "someone-else"
    assert verify_auth_password(session, "a-different-password") is True
    assert verify_auth_password(session, PASSWORD) is False


def test_only_username_set_is_a_startup_error(tmp_path: Path) -> None:
    """Settings itself fails fast on a lone username/password (see
    collapsarr.config._require_auth_seed_pair) rather than silently seeding
    nothing -- a likely typo on a headless deploy should be loud, not silent."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_path=str(tmp_path / "collapsarr.db"),
            auth_username=USERNAME,
        )


def test_method_and_required_honoured_when_present(tmp_path: Path) -> None:
    settings = _seeded_settings(
        tmp_path, auth_method=AUTH_METHOD_BASIC, auth_required=AUTH_REQUIRED_ENABLED
    )
    session = _session_for(settings)

    result = seed_auth_from_env(session, settings)

    assert result is not None
    assert result.auth_method == AUTH_METHOD_BASIC
    assert result.auth_required == AUTH_REQUIRED_ENABLED


def test_method_and_required_fall_back_to_defaults_when_absent(tmp_path: Path) -> None:
    settings = _seeded_settings(tmp_path)
    session = _session_for(settings)

    result = seed_auth_from_env(session, settings)

    assert result is not None
    # GlobalSettings' own documented defaults (COL-49/COL-51): forms, local_bypass.
    assert result.auth_method == "forms"
    assert result.auth_required == "local_bypass"


# ---------------------------------------------------------------------------
# End-to-end: a fresh boot with the env vars set skips the /setup gate.
# ---------------------------------------------------------------------------


def test_fresh_boot_with_env_vars_skips_the_setup_gate(tmp_path: Path) -> None:
    """Mirrors test_auth.py's no-credential redirect-to-/setup test, but with
    the seed env vars set: the gate must NOT redirect to /setup, and the
    seeded credential must actually authenticate."""
    settings = _seeded_settings(tmp_path, auth_required=AUTH_REQUIRED_ENABLED)
    app = create_app(settings=settings)

    with TestClient(app, follow_redirects=False) as client:
        status = client.get("/api/auth/status")
        assert status.status_code == 200
        assert status.json()["needs_setup"] is False

        login = client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        assert login.status_code == 200
        assert login.json()["authenticated"] is True


def test_second_boot_with_env_vars_still_set_does_not_reseed(tmp_path: Path) -> None:
    """A second app boot (simulating a container restart) against the same DB,
    with the env vars still present, must not reset a since-changed credential."""
    settings = _seeded_settings(tmp_path, auth_required=AUTH_REQUIRED_ENABLED)

    # First boot: seeds the credential.
    with TestClient(create_app(settings=settings), follow_redirects=False):
        pass

    # Operator changes the password via the settings service (simulating a
    # change made through the UI) between boots.
    session = _session_for(settings)
    update_global_settings(session, password="changed-after-first-boot")
    session.close()

    # Second boot: env vars are still set, but must not overwrite the change.
    with TestClient(create_app(settings=settings), follow_redirects=False) as client:
        stale_login = client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        assert stale_login.status_code == 401

        current_login = client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": "changed-after-first-boot"},
        )
        assert current_login.status_code == 200
