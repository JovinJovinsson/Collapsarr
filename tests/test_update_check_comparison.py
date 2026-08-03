"""Tests for the pure version-identity comparison (COL-86).

No I/O -- unit tests only, per the ticket's acceptance criteria.
"""

from __future__ import annotations

from collapsarr.update_check.comparison import is_up_to_date, running_version_tag


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
