"""Tests for reverse-proxy trust resolution (COL-112).

``resolve_client_address``/``resolve_scheme`` (collapsarr.auth.trust) are a
foundation module -- nothing in the request path calls them yet (that is
COL-113/COL-114's job). These tests drive them directly through a minimal
app exposing both functions' results, using the same
``TestClient(client=(host, port))`` trick ``tests/test_auth.py`` uses to
control the direct connection peer the ASGI server reports.

``Settings`` validation of ``COLLAPSARR_TRUSTED_PROXIES`` itself is covered
in ``tests/test_config.py`` (prior art for this config field), not here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from collapsarr.auth.trust import parse_trusted_proxies, resolve_client_address, resolve_scheme
from collapsarr.config import Settings

TRUSTED_PROXY = "10.0.0.1"
UNTRUSTED_PEER = "8.8.8.8"  # a real, globally-routable address (Google Public DNS)
FORWARDED_CLIENT = "203.0.113.7"  # TEST-NET-3 (RFC 5737) -- never a real client


def _settings(trusted_proxies: str = "") -> Settings:
    return Settings(_env_file=None, trusted_proxies=trusted_proxies)


def _build_app(settings: Settings) -> FastAPI:
    """A minimal app exposing both functions' results for a request.

    Deliberately bypasses ``collapsarr.main.create_app`` (no DB, no
    migrations, no lifespan) -- this module has no behavior of its own to
    exercise end-to-end yet, only the two resolution functions.
    """
    app = FastAPI()
    app.state.settings = settings

    @app.get("/resolved")
    async def resolved(request: Request) -> dict[str, str | None]:
        return {
            "address": resolve_client_address(request),
            "scheme": resolve_scheme(request),
        }

    return app


@contextmanager
def _client_for_peer(settings: Settings, host: str) -> Iterator[TestClient]:
    """A TestClient whose ASGI scope reports ``host`` as the direct peer."""
    app = _build_app(settings)
    with TestClient(app, client=(host, 51234)) as test_client:
        yield test_client


# --- parse_trusted_proxies ----------------------------------------------------


def test_parse_trusted_proxies_default_empty_string_is_no_trust() -> None:
    assert parse_trusted_proxies("") == []


def test_parse_trusted_proxies_parses_bare_ips_and_cidrs() -> None:
    networks = parse_trusted_proxies("10.0.0.1, 192.168.0.0/24 ,::1")
    assert [str(network) for network in networks] == [
        "10.0.0.1/32",
        "192.168.0.0/24",
        "::1/128",
    ]


def test_parse_trusted_proxies_skips_blank_entries() -> None:
    assert parse_trusted_proxies(" , 10.0.0.1 ,, ") == parse_trusted_proxies("10.0.0.1")


def test_parse_trusted_proxies_rejects_an_unparseable_entry() -> None:
    with pytest.raises(ValueError, match="not a valid IP address or CIDR block"):
        parse_trusted_proxies("not-an-ip")


# --- resolve_client_address ---------------------------------------------------


def test_resolve_client_address_returns_direct_peer_when_untrusted() -> None:
    """An untrusted peer's X-Forwarded-For is never consulted -- forgeable."""
    settings = _settings(trusted_proxies="")
    with _client_for_peer(settings, UNTRUSTED_PEER) as client:
        response = client.get("/resolved", headers={"X-Forwarded-For": FORWARDED_CLIENT})
        assert response.json()["address"] == UNTRUSTED_PEER


def test_resolve_client_address_returns_direct_peer_when_untrusted_and_no_header() -> None:
    settings = _settings(trusted_proxies="")
    with _client_for_peer(settings, UNTRUSTED_PEER) as client:
        response = client.get("/resolved")
        assert response.json()["address"] == UNTRUSTED_PEER


def test_resolve_client_address_returns_rightmost_forwarded_entry_when_trusted() -> None:
    """Single-hop trust: only the rightmost (nearest-to-Collapsarr) entry counts."""
    settings = _settings(trusted_proxies=f"{TRUSTED_PROXY}/32")
    with _client_for_peer(settings, TRUSTED_PROXY) as client:
        response = client.get(
            "/resolved",
            headers={"X-Forwarded-For": f"203.0.113.1, 198.51.100.2, {FORWARDED_CLIENT}"},
        )
        assert response.json()["address"] == FORWARDED_CLIENT


def test_resolve_client_address_trust_is_cidr_based() -> None:
    settings = _settings(trusted_proxies="10.0.0.0/8")
    with _client_for_peer(settings, "10.1.2.3") as client:
        response = client.get("/resolved", headers={"X-Forwarded-For": FORWARDED_CLIENT})
        assert response.json()["address"] == FORWARDED_CLIENT


def test_resolve_client_address_falls_back_to_peer_when_trusted_but_no_header() -> None:
    settings = _settings(trusted_proxies=TRUSTED_PROXY)
    with _client_for_peer(settings, TRUSTED_PROXY) as client:
        response = client.get("/resolved")
        assert response.json()["address"] == TRUSTED_PROXY


# --- resolve_scheme ------------------------------------------------------------


def test_resolve_scheme_returns_direct_scheme_when_untrusted() -> None:
    """An untrusted peer's X-Forwarded-Proto is never consulted -- forgeable."""
    settings = _settings(trusted_proxies="")
    with _client_for_peer(settings, UNTRUSTED_PEER) as client:
        response = client.get("/resolved", headers={"X-Forwarded-Proto": "https"})
        assert response.json()["scheme"] == "http"


def test_resolve_scheme_returns_forwarded_proto_when_trusted() -> None:
    settings = _settings(trusted_proxies=TRUSTED_PROXY)
    with _client_for_peer(settings, TRUSTED_PROXY) as client:
        response = client.get("/resolved", headers={"X-Forwarded-Proto": "https"})
        assert response.json()["scheme"] == "https"


def test_resolve_scheme_falls_back_to_direct_scheme_when_trusted_but_no_header() -> None:
    settings = _settings(trusted_proxies=TRUSTED_PROXY)
    with _client_for_peer(settings, TRUSTED_PROXY) as client:
        response = client.get("/resolved")
        assert response.json()["scheme"] == "http"


def test_resolve_scheme_uses_rightmost_forwarded_proto_when_trusted() -> None:
    """Same nearest-hop convention as X-Forwarded-For -- the rightmost entry
    is the value the trusted peer itself attached."""
    settings = _settings(trusted_proxies=TRUSTED_PROXY)
    with _client_for_peer(settings, TRUSTED_PROXY) as client:
        response = client.get("/resolved", headers={"X-Forwarded-Proto": "http, https"})
        assert response.json()["scheme"] == "https"
