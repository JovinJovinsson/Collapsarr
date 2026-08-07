"""Reverse-proxy trust resolution (COL-112).

Foundation module: parses ``COLLAPSARR_TRUSTED_PROXIES`` into an IP/CIDR
allowlist and exposes two functions that decide, per request, whether a
client-suppliable header may be trusted in place of the value the ASGI
server itself observed:

* :func:`resolve_client_address` -- the real client address, honouring
  ``X-Forwarded-For`` only when the direct peer is an allow-listed proxy.
* :func:`resolve_scheme` -- the real request scheme, honouring
  ``X-Forwarded-Proto`` the same way.

Trust is single-hop only: when the direct peer is on the allowlist, the
*rightmost* ``X-Forwarded-For`` entry (the hop closest to Collapsarr) is
taken as the real client, and ``X-Forwarded-Proto`` is taken as-is. There is
no support for walking back through a multi-proxy chain -- an install behind
more than one hop of reverse proxy must terminate/normalise those headers
before they reach Collapsarr.

This module makes **no behavior change on its own** -- nothing in the
request path calls it yet. It exists to unblock two consuming slices:
``local_bypass`` classification (:func:`collapsarr.auth.enforcement._client_is_local`,
COL-113) and the session cookie's ``Secure`` flag
(:func:`collapsarr.auth.session._is_secure`, COL-114). Both of those read the
direct peer/scheme unconditionally today; neither is touched here.

Both header-suppliable values are only trusted when the *direct TCP peer* --
the address the ASGI server actually accepted the connection from, never a
header -- is in the ``COLLAPSARR_TRUSTED_PROXIES`` allowlist. A caller cannot
forge its way onto the allowlist by sending a header: the header is only
consulted after the connection's own peer address has already qualified.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import Request

from ..config import Settings

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

X_FORWARDED_FOR = "x-forwarded-for"
X_FORWARDED_PROTO = "x-forwarded-proto"

_DEFAULT_SCHEME = "http"


def parse_trusted_proxies(raw: str) -> list[IpNetwork]:
    """Parse ``COLLAPSARR_TRUSTED_PROXIES`` into an IP/CIDR allowlist.

    ``raw`` is the comma-separated setting value. Blank entries (including
    an entirely empty string, the default) are skipped, so an empty setting
    parses to an empty allowlist -- no peer is trusted. A bare IP address is
    treated as a single-address network (``/32`` or ``/128``); a CIDR block
    is used as given.

    Raises :class:`ValueError` for any entry that is neither a valid IP
    address nor a valid CIDR block. :meth:`collapsarr.config.Settings.
    _validate_trusted_proxies` calls this at construction time so a
    malformed value fails fast at startup -- this function itself has no
    knowledge of pydantic and is safe to call standalone (e.g. from tests).
    """
    networks: list[IpNetwork] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError as exc:
            raise ValueError(
                f"COLLAPSARR_TRUSTED_PROXIES entry {candidate!r} is not a "
                "valid IP address or CIDR block."
            ) from exc
    return networks


@lru_cache(maxsize=32)
def _cached_networks(raw: str) -> tuple[IpNetwork, ...]:
    """Memoised :func:`parse_trusted_proxies`, keyed on the raw setting value.

    ``COLLAPSARR_TRUSTED_PROXIES`` is read once at startup and never changes
    for the life of a :class:`~collapsarr.config.Settings` instance, so
    re-parsing it on every request would be pure waste. The cache is keyed
    on the raw string (not the ``Settings`` instance) so it stays valid
    across the many separately-constructed ``Settings`` instances tests
    create with the same value.
    """
    return tuple(parse_trusted_proxies(raw))


def _peer_host(request: Request) -> str | None:
    """The literal address the ASGI server accepted this connection from."""
    client = request.client
    return client.host if client is not None else None


def _is_trusted_peer(request: Request) -> bool:
    """Whether the request's direct TCP peer is on the trusted-proxy allowlist.

    Reads only ``request.client`` -- never a header -- so this check itself
    cannot be spoofed; only a peer that actually qualifies gets its
    forwarded headers consulted at all.
    """
    host = _peer_host(request)
    if host is None:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP (seen in some non-network test harnesses) --
        # treat conservatively as untrusted.
        return False
    settings: Settings = request.app.state.settings
    networks = _cached_networks(settings.trusted_proxies)
    return any(address in network for network in networks)


def resolve_client_address(request: Request) -> str | None:
    """The real client address for ``request``.

    Returns the direct peer's address, unconditionally, unless that peer is
    on the ``COLLAPSARR_TRUSTED_PROXIES`` allowlist -- in which case the
    rightmost ``X-Forwarded-For`` entry (the hop nearest Collapsarr, i.e.
    the trusted proxy's own view of its immediate client) is returned
    instead. ``None`` only when there is no direct peer to fall back to
    (mirrors ``request.client`` being ``None``).

    An untrusted peer's ``X-Forwarded-For`` header, however present or
    well-formed, is never consulted -- it is exactly as forgeable as any
    other client-supplied header.
    """
    peer_host = _peer_host(request)
    if not _is_trusted_peer(request):
        return peer_host
    forwarded = request.headers.get(X_FORWARDED_FOR)
    if forwarded:
        rightmost = forwarded.rsplit(",", 1)[-1].strip()
        if rightmost:
            return rightmost
    return peer_host


def resolve_scheme(request: Request) -> str:
    """The real request scheme for ``request``.

    Returns the direct ASGI scheme, unconditionally, unless the direct peer
    is on the ``COLLAPSARR_TRUSTED_PROXIES`` allowlist -- in which case
    ``X-Forwarded-Proto`` is returned instead. If a trusted peer sends more
    than one comma-separated value, the rightmost is used -- the same
    nearest-hop convention as :func:`resolve_client_address`'s
    ``X-Forwarded-For`` handling (the value the trusted peer itself
    attached, not whatever an untrusted upstream hop may have claimed).
    """
    direct_scheme = str(request.scope.get("scheme", _DEFAULT_SCHEME))
    if not _is_trusted_peer(request):
        return direct_scheme
    forwarded = request.headers.get(X_FORWARDED_PROTO)
    if forwarded:
        candidate = forwarded.rsplit(",", 1)[-1].strip()
        if candidate:
            return candidate
    return direct_scheme
