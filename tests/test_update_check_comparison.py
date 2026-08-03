"""Tests for the pure version-identity comparison (COL-86, beta channel COL-88).

No I/O -- unit tests only, per the ticket's acceptance criteria.
"""

from __future__ import annotations

from collapsarr.update_check.comparison import (
    extract_beta_sha,
    extract_beta_tag_sha,
    is_up_to_date,
    is_up_to_date_beta,
    running_version_tag,
)


def test_running_version_tag_prefixes_with_v() -> None:
    assert running_version_tag("1.2.3") == "v1.2.3"


def test_is_up_to_date_reports_match() -> None:
    assert is_up_to_date("1.2.3", "v1.2.3") is True


def test_is_up_to_date_reports_no_match_on_newer_tag() -> None:
    assert is_up_to_date("1.2.3", "v1.3.0") is False


def test_is_up_to_date_reports_no_match_on_older_tag() -> None:
    # Deliberately not semver-aware: any non-identical tag is "not up to date",
    # even one that's technically older than the running version.
    assert is_up_to_date("1.3.0", "v1.2.3") is False


def test_is_up_to_date_is_exact_string_identity_not_semver() -> None:
    # A tag with build/prerelease metadata never matches a bare version by
    # plain identity -- no semver normalization happens here.
    assert is_up_to_date("1.2.3", "v1.2.3+beta.abc1234") is False


def test_is_up_to_date_reports_false_when_no_tag_is_known_yet() -> None:
    assert is_up_to_date("1.2.3", None) is False


# ---------------------------------------------------------------------------
# Beta channel (COL-88).
# ---------------------------------------------------------------------------


def test_extract_beta_sha_returns_the_embedded_short_sha() -> None:
    assert extract_beta_sha("1.2.3+beta.abc1234") == "abc1234"


def test_extract_beta_sha_returns_none_for_a_stable_version() -> None:
    assert extract_beta_sha("1.2.3") is None


def test_extract_beta_sha_returns_none_for_an_empty_segment() -> None:
    assert extract_beta_sha("1.2.3+beta.") is None


def test_extract_beta_tag_sha_returns_the_suffix() -> None:
    assert extract_beta_tag_sha("beta-abc1234") == "abc1234"


def test_extract_beta_tag_sha_returns_none_for_a_stable_tag() -> None:
    assert extract_beta_tag_sha("v1.2.3") is None


def test_extract_beta_tag_sha_returns_none_for_an_empty_suffix() -> None:
    assert extract_beta_tag_sha("beta-") is None


def test_is_up_to_date_beta_reports_match_on_identical_sha() -> None:
    assert is_up_to_date_beta("1.2.3+beta.abc1234", "beta-abc1234") is True


def test_is_up_to_date_beta_reports_no_match_on_a_different_sha() -> None:
    assert is_up_to_date_beta("1.2.3+beta.abc1234", "beta-def5678") is False


def test_is_up_to_date_beta_reports_false_when_no_tag_is_known_yet() -> None:
    assert is_up_to_date_beta("1.2.3+beta.abc1234", None) is False


def test_is_up_to_date_beta_reports_false_for_a_stable_running_version() -> None:
    # Documented edge case (COL-88's acceptance criteria): a stable build
    # switched to the beta channel has no SHA to compare, so this reports
    # "not up to date" (i.e. an update shows as available) rather than a bug.
    assert is_up_to_date_beta("1.2.3", "beta-abc1234") is False


def test_is_up_to_date_beta_reports_false_for_a_non_beta_tag() -> None:
    assert is_up_to_date_beta("1.2.3+beta.abc1234", "v1.2.3") is False
