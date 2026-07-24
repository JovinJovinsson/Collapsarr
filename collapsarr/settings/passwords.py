"""Password hashing primitive for the single UI credential (COL-49).

Collapsarr authenticates a **single** operator credential (Radarr-style -- no
multi-user), whose hash lives on the persisted
:class:`~collapsarr.settings.models.GlobalSettings` row. This module is the
whole hashing surface the settings service builds on; nothing outside it should
touch the raw digest.

Hashing uses PBKDF2-HMAC-SHA512 from the standard library
(:func:`hashlib.pbkdf2_hmac`) with a per-credential, cryptographically-random
salt (:func:`secrets.token_bytes`) and a fixed, high iteration count. The
encoded string bundles **algorithm + iteration count + salt + digest** together
(a ``$``-delimited, Django-style form), so:

* the work factor can be raised later without a schema change -- a new hash
  simply carries the new iteration count, and
* verification reads the iteration count *from the stored hash*, so credentials
  written under an older factor keep verifying after the default is bumped.

Verification is constant-time (:func:`hmac.compare_digest`) to avoid leaking
digest bytes through comparison timing. Plaintext is never stored, logged, or
returned; only the encoded hash leaves this module.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

ALGORITHM = "sha512"
"""PBKDF2 underlying HMAC hash -- SHA-512, per the ticket."""

ITERATIONS = 210_000
"""Fixed PBKDF2 iteration count (work factor).

210k matches the OWASP 2023 guidance for PBKDF2-HMAC-SHA512. Stored *inside*
each hash (see :func:`hash_password`), so raising this only affects newly
written credentials -- existing hashes verify against their own recorded count.
"""

SALT_BYTES = 16
"""Per-credential random salt length (128 bits)."""

_SCHEME = "pbkdf2_sha512"
"""Identifier prefix of the encoded hash, distinguishing the scheme in storage."""

_FIELD_SEPARATOR = "$"
"""Delimiter between the encoded hash's scheme/iterations/salt/digest fields."""


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    """Derive the raw PBKDF2-HMAC-SHA512 digest for ``password``."""
    return hashlib.pbkdf2_hmac(ALGORITHM, password.encode("utf-8"), salt, iterations)


def hash_password(password: str) -> str:
    """Return an encoded PBKDF2 hash of ``password``, safe to persist.

    Mints a fresh random salt and derives the digest at the current
    :data:`ITERATIONS` work factor, then encodes
    ``pbkdf2_sha512$<iterations>$<salt_hex>$<digest_hex>`` so the salt and work
    factor travel with the hash. Two calls with the same password return
    different strings (distinct salts); use :func:`verify_password` to check a
    candidate, never a plain string comparison.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _pbkdf2(password, salt, ITERATIONS)
    return _FIELD_SEPARATOR.join((_SCHEME, str(ITERATIONS), salt.hex(), digest.hex()))


def verify_password(password: str, encoded: str) -> bool:
    """Return whether ``password`` matches the ``encoded`` hash, in constant time.

    Parses the iteration count and salt out of ``encoded`` (so a hash written
    under an older work factor still verifies), re-derives the digest, and
    compares it to the stored digest with :func:`hmac.compare_digest`. Any
    malformed or unrecognised ``encoded`` value returns ``False`` rather than
    raising, so a corrupt stored hash fails closed.
    """
    try:
        scheme, iterations_text, salt_hex, digest_hex = encoded.split(_FIELD_SEPARATOR)
    except (AttributeError, ValueError):
        return False

    if scheme != _SCHEME:
        return False

    try:
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False

    if iterations < 1:
        return False

    candidate = _pbkdf2(password, salt, iterations)
    return hmac.compare_digest(candidate, expected)
