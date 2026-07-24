"""Tests for the PBKDF2 password-hashing primitive (COL-49).

Exercises :mod:`collapsarr.settings.passwords` directly -- the hashing surface
the settings service builds on. Focus: the encoded form carries salt +
iteration count, correct/incorrect verification, tunable work factor
(old hashes keep verifying), and malformed input failing closed.
"""

from __future__ import annotations

from collapsarr.settings import passwords


def test_hash_encodes_scheme_iterations_salt_and_digest() -> None:
    encoded = passwords.hash_password("hunter2")

    scheme, iterations, salt_hex, digest_hex = encoded.split("$")
    assert scheme == "pbkdf2_sha512"
    assert int(iterations) == passwords.ITERATIONS
    # 16-byte salt -> 32 hex chars; SHA-512 digest -> 128 hex chars.
    assert len(salt_hex) == passwords.SALT_BYTES * 2
    assert len(digest_hex) == 128


def test_hash_never_contains_the_plaintext() -> None:
    encoded = passwords.hash_password("super-secret-value")

    assert "super-secret-value" not in encoded


def test_correct_password_verifies() -> None:
    encoded = passwords.hash_password("correct password")

    assert passwords.verify_password("correct password", encoded) is True


def test_wrong_password_does_not_verify() -> None:
    encoded = passwords.hash_password("correct password")

    assert passwords.verify_password("wrong password", encoded) is False


def test_each_hash_uses_a_distinct_salt() -> None:
    first = passwords.hash_password("same")
    second = passwords.hash_password("same")

    assert first != second
    assert passwords.verify_password("same", first) is True
    assert passwords.verify_password("same", second) is True


def test_hash_written_at_a_different_work_factor_still_verifies() -> None:
    """The iteration count is read from the stored hash, so bumping the default
    later does not invalidate credentials written under an older factor."""
    salt = b"0123456789abcdef"
    weaker = 1000
    digest = passwords._pbkdf2("legacy", salt, weaker)
    encoded = f"pbkdf2_sha512${weaker}${salt.hex()}${digest.hex()}"

    assert passwords.verify_password("legacy", encoded) is True
    assert passwords.verify_password("nope", encoded) is False


def test_malformed_encoded_values_fail_closed() -> None:
    for bad in ["", "not-a-hash", "pbkdf2_sha512$notint$aa$bb", "scrypt$1$aa$bb", "$$$"]:
        assert passwords.verify_password("whatever", bad) is False
