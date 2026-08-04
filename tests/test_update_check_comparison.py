"""Tests for the pure version-identity comparison (COL-86, beta channel COL-88, COL-96).

No I/O -- unit tests only, per the ticket's acceptance criteria.
"""

from __future__ import annotations

from collapsarr.update_check.comparison import (
    is_up_to_date,
    is_up_to_date_beta,
    public_version,
    running_beta_version_tag,
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
    assert is_up_to_date("1.2.3", "v1.2.3+beta") is False


def test_is_up_to_date_reports_false_when_no_tag_is_known_yet() -> None:
    assert is_up_to_date("1.2.3", None) is False


# ---------------------------------------------------------------------------
# Beta channel (COL-88, orderable scheme COL-96).
# ---------------------------------------------------------------------------


def test_public_version_strips_the_local_segment() -> None:
    assert public_version("0.2.1.0007+beta") == "0.2.1.0007"


def test_public_version_leaves_a_bare_version_unchanged() -> None:
    assert public_version("0.2.1.0007") == "0.2.1.0007"


def test_running_beta_version_tag_prefixes_and_strips_local_segment() -> None:
    assert running_beta_version_tag("0.2.1.0007+beta") == "beta-v0.2.1.0007"


def test_is_up_to_date_beta_reports_match_on_identical_version() -> None:
    assert is_up_to_date_beta("0.2.1.0007+beta", "beta-v0.2.1.0007") is True


def test_is_up_to_date_beta_reports_no_match_on_a_newer_build() -> None:
    assert is_up_to_date_beta("0.2.1.0007+beta", "beta-v0.2.1.0008") is False


def test_is_up_to_date_beta_reports_false_when_no_tag_is_known_yet() -> None:
    assert is_up_to_date_beta("0.2.1.0007+beta", None) is False


def test_is_up_to_date_beta_reports_false_for_a_stable_running_version() -> None:
    # Documented edge case (COL-88's acceptance criteria, carried forward by
    # COL-96): a stable build switched to the beta channel has no `+beta`
    # marker, so this reports "not up to date" (an update shows as available)
    # rather than a bug.
    assert is_up_to_date_beta("0.2.1", "beta-v0.2.1.0007") is False


def test_is_up_to_date_beta_reports_false_for_a_non_beta_tag() -> None:
    assert is_up_to_date_beta("0.2.1.0007+beta", "v0.2.1") is False
