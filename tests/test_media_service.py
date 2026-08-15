"""Tests for tracked media upsert semantics and the missing-target query (COL-25).

Uses the shared ``session`` fixture (a schema-initialised DB session -- see
``conftest.py``), matching the pattern in ``test_settings_service.py`` and
``test_jobs_history.py``. Stream fixtures are built directly (no ffprobe
involved), matching ``test_downmix_targets.py``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.media.models import MediaTargetStatus, TrackedMediaFile, TrackedMediaTargetStatus
from collapsarr.media.service import (
    get_tracked_media,
    list_files_missing_targets,
    list_target_statuses,
    list_tracked_media,
    list_tracked_media_by_instance,
    record_target_processed,
    upsert_tracked_media,
)

ALL_TARGETS = frozenset(
    {DownmixTarget.STEREO, DownmixTarget.TWO_POINT_ONE, DownmixTarget.FIVE_POINT_ONE}
)


def _stream(
    *,
    index: int = 0,
    channels: int,
    language: str = "eng",
    codec: str = "flac",
    is_default: bool = False,
) -> AudioStreamInfo:
    return AudioStreamInfo(
        index=index,
        codec=codec,
        channels=channels,
        channel_layout=f"{channels}ch",
        language=language,
        is_default=is_default,
    )


# ---------------------------------------------------------------------------
# upsert_tracked_media: creation + status derivation.
# ---------------------------------------------------------------------------


def test_upsert_creates_a_tracked_media_row(session: Session) -> None:
    assert session.scalars(select(TrackedMediaFile)).one_or_none() is None

    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    assert media.id is not None
    assert media.file_path == "/media/movie.mkv"
    assert session.scalars(select(TrackedMediaFile)).one().id == media.id


def test_upsert_marks_qualifying_targets_missing(session: Session) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    statuses = {
        (row.language, row.target): row.status
        for row in list_target_statuses(session, "/media/movie.mkv")
    }

    assert statuses == {
        ("eng", DownmixTarget.STEREO): MediaTargetStatus.MISSING,
        ("eng", DownmixTarget.TWO_POINT_ONE): MediaTargetStatus.MISSING,
        ("eng", DownmixTarget.FIVE_POINT_ONE): MediaTargetStatus.MISSING,
    }


def test_upsert_marks_already_present_target_processed(session: Session) -> None:
    """A stream already at a target's channel count is PROCESSED, not MISSING."""
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8), _stream(channels=2)],  # both eng
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    statuses = {
        (row.language, row.target): row.status
        for row in list_target_statuses(session, "/media/movie.mkv")
    }

    assert statuses[("eng", DownmixTarget.STEREO)] == MediaTargetStatus.PROCESSED
    assert statuses[("eng", DownmixTarget.TWO_POINT_ONE)] == MediaTargetStatus.MISSING
    assert statuses[("eng", DownmixTarget.FIVE_POINT_ONE)] == MediaTargetStatus.MISSING


def test_upsert_marks_stereo_only_source_processed_for_no_upmix_targets(session: Session) -> None:
    """A stereo-only source's higher targets are PROCESSED (never MISSING -- would upmix)."""
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=2)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    statuses = {
        (row.language, row.target): row.status
        for row in list_target_statuses(session, "/media/movie.mkv")
    }

    assert statuses == {
        ("eng", DownmixTarget.STEREO): MediaTargetStatus.PROCESSED,
        ("eng", DownmixTarget.TWO_POINT_ONE): MediaTargetStatus.PROCESSED,
        ("eng", DownmixTarget.FIVE_POINT_ONE): MediaTargetStatus.PROCESSED,
    }


def test_upsert_only_creates_rows_for_enabled_targets(session: Session) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=frozenset({DownmixTarget.STEREO})),
    )

    statuses = list_target_statuses(session, "/media/movie.mkv")

    assert {row.target for row in statuses} == {DownmixTarget.STEREO}


def test_upsert_marks_language_outside_allow_list_excluded(session: Session) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8, language="fre")],
        settings=DownmixSettings(
            enabled_targets=ALL_TARGETS, language_allow_list=frozenset({"eng"})
        ),
    )

    statuses = list_target_statuses(session, "/media/movie.mkv")

    assert all(
        row.status == MediaTargetStatus.EXCLUDED_BY_LANGUAGE_FILTER for row in statuses
    )
    assert {row.language for row in statuses} == {"fre"}


def test_upsert_multi_language_tracks_evaluated_independently(session: Session) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[
            _stream(index=0, channels=8, language="eng"),
            _stream(index=1, channels=2, language="jpn"),
        ],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    statuses = {
        (row.language, row.target): row.status
        for row in list_target_statuses(session, "/media/movie.mkv")
    }

    assert statuses[("eng", DownmixTarget.STEREO)] == MediaTargetStatus.MISSING
    assert statuses[("jpn", DownmixTarget.STEREO)] == MediaTargetStatus.PROCESSED


# ---------------------------------------------------------------------------
# upsert_tracked_media: current default audio track snapshot (COL-154).
# ---------------------------------------------------------------------------


def test_upsert_records_current_default_track_from_the_is_default_stream(
    session: Session,
) -> None:
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[
            _stream(index=0, channels=2, language="eng", is_default=False),
            _stream(index=1, channels=6, language="dan", is_default=True),
        ],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    assert media.current_default_language == "dan"
    assert media.current_default_channel_layout == "6ch"


def test_upsert_leaves_current_default_track_unset_when_no_stream_is_default(
    session: Session,
) -> None:
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(index=0, channels=2), _stream(index=1, channels=6)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    assert media.current_default_language is None
    assert media.current_default_channel_layout is None


def test_upsert_current_default_track_unset_for_a_file_with_no_audio_streams(
    session: Session,
) -> None:
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    assert media.current_default_language is None
    assert media.current_default_channel_layout is None


def test_upsert_current_default_track_prefers_lowest_index_on_multiple_defaults(
    session: Session,
) -> None:
    """Malformed ffprobe metadata reporting >1 default stream: lowest index wins."""
    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[
            _stream(index=0, channels=2, language="eng", is_default=True),
            _stream(index=1, channels=6, language="dan", is_default=True),
        ],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    assert media.current_default_language == "eng"
    assert media.current_default_channel_layout == "2ch"


def test_upsert_refreshes_current_default_track_across_rescans(session: Session) -> None:
    """A later probe with a different current default overwrites the earlier snapshot."""
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(index=0, channels=2, language="eng", is_default=True)],
        settings=settings,
    )

    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(index=0, channels=6, language="dan", is_default=True)],
        settings=settings,
    )

    assert media.current_default_language == "dan"
    assert media.current_default_channel_layout == "6ch"


def test_upsert_clears_current_default_track_when_a_rescan_finds_no_default(
    session: Session,
) -> None:
    """A rescan that no longer reports a default stream clears a previous snapshot too."""
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(index=0, channels=2, language="eng", is_default=True)],
        settings=settings,
    )

    media = upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(index=0, channels=2, language="eng", is_default=False)],
        settings=settings,
    )

    assert media.current_default_language is None
    assert media.current_default_channel_layout is None


# ---------------------------------------------------------------------------
# upsert_tracked_media: Library-node bridge ids (COL-101).
# ---------------------------------------------------------------------------


def test_upsert_persists_sonarr_episode_id_and_instance_id(session: Session) -> None:
    media = upsert_tracked_media(
        session,
        file_path="/media/tv/pilot.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
        instance_id=7,
        sonarr_episode_id=101,
    )

    assert media.instance_id == 7
    assert media.sonarr_episode_id == 101
    assert media.radarr_movie_id is None


def test_upsert_persists_radarr_movie_id(session: Session) -> None:
    media = upsert_tracked_media(
        session,
        file_path="/media/movies/interstellar.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
        instance_id=9,
        radarr_movie_id=1,
    )

    assert media.instance_id == 9
    assert media.radarr_movie_id == 1
    assert media.sonarr_episode_id is None


def test_upsert_without_ids_leaves_a_previously_established_linkage_alone(
    session: Session,
) -> None:
    """A later id-less call (e.g. a manual trigger) doesn't clobber a known linkage."""
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/tv/pilot.mkv",
        streams=[_stream(channels=8)],
        settings=settings,
        instance_id=7,
        sonarr_episode_id=101,
    )

    media = upsert_tracked_media(
        session, file_path="/media/tv/pilot.mkv", streams=[_stream(channels=8)], settings=settings
    )

    assert media.instance_id == 7
    assert media.sonarr_episode_id == 101


def test_upsert_ids_default_to_none_when_never_given(session: Session) -> None:
    media = upsert_tracked_media(
        session,
        file_path="/media/tv/pilot.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    assert media.instance_id is None
    assert media.sonarr_episode_id is None
    assert media.radarr_movie_id is None


def test_list_tracked_media_by_instance_returns_only_that_instances_rows(
    session: Session,
) -> None:
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session,
        file_path="/media/tv/pilot.mkv",
        streams=[_stream(channels=8)],
        settings=settings,
        instance_id=7,
        sonarr_episode_id=101,
    )
    upsert_tracked_media(
        session,
        file_path="/media/movies/interstellar.mkv",
        streams=[_stream(channels=8)],
        settings=settings,
        instance_id=9,
        radarr_movie_id=1,
    )
    upsert_tracked_media(
        session, file_path="/media/untracked.mkv", streams=[_stream(channels=8)], settings=settings
    )

    result = list_tracked_media_by_instance(session, 7)

    assert [media.file_path for media in result] == ["/media/tv/pilot.mkv"]


def test_list_tracked_media_by_instance_returns_empty_for_an_unknown_instance(
    session: Session,
) -> None:
    assert list_tracked_media_by_instance(session, 999) == []


# ---------------------------------------------------------------------------
# upsert_tracked_media: upsert-in-place semantics (no duplicates).
# ---------------------------------------------------------------------------


def test_upsert_does_not_duplicate_the_media_row_on_a_second_call(session: Session) -> None:
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    first = upsert_tracked_media(
        session, file_path="/media/movie.mkv", streams=[_stream(channels=8)], settings=settings
    )
    second = upsert_tracked_media(
        session, file_path="/media/movie.mkv", streams=[_stream(channels=8)], settings=settings
    )

    assert first.id == second.id
    assert session.scalars(select(TrackedMediaFile)).all() == [first]


def test_upsert_does_not_duplicate_status_rows_across_rescans(session: Session) -> None:
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session, file_path="/media/movie.mkv", streams=[_stream(channels=8)], settings=settings
    )
    upsert_tracked_media(
        session, file_path="/media/movie.mkv", streams=[_stream(channels=8)], settings=settings
    )

    statuses = list_target_statuses(session, "/media/movie.mkv")

    assert len(statuses) == 3  # stereo, 2.1, 5.1 -- one row each, not six


def test_upsert_rescan_flips_missing_to_processed_once_target_exists(session: Session) -> None:
    """Rescanning after a target's track has actually been added flips MISSING -> PROCESSED."""
    settings = DownmixSettings(enabled_targets=frozenset({DownmixTarget.STEREO}))
    upsert_tracked_media(
        session, file_path="/media/movie.mkv", streams=[_stream(channels=8)], settings=settings
    )
    assert list_target_statuses(session, "/media/movie.mkv")[0].status == MediaTargetStatus.MISSING

    # Simulate a completed downmix job: the file now also has a 2ch stream.
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8), _stream(channels=2)],
        settings=settings,
    )

    statuses = list_target_statuses(session, "/media/movie.mkv")
    assert len(statuses) == 1
    assert statuses[0].status == MediaTargetStatus.PROCESSED


def test_upsert_two_different_files_are_tracked_independently(session: Session) -> None:
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session, file_path="/media/a.mkv", streams=[_stream(channels=8)], settings=settings
    )
    upsert_tracked_media(
        session, file_path="/media/b.mkv", streams=[_stream(channels=2)], settings=settings
    )

    assert {media.file_path for media in list_tracked_media(session)} == {
        "/media/a.mkv",
        "/media/b.mkv",
    }


# ---------------------------------------------------------------------------
# record_target_processed: the job-completion write path.
# ---------------------------------------------------------------------------


def test_record_target_processed_flips_an_existing_missing_row(session: Session) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=frozenset({DownmixTarget.STEREO})),
    )

    row = record_target_processed(
        session, file_path="/media/movie.mkv", language="eng", target=DownmixTarget.STEREO
    )

    assert row.status == MediaTargetStatus.PROCESSED
    assert (
        list_target_statuses(session, "/media/movie.mkv")[0].status
        == MediaTargetStatus.PROCESSED
    )


def test_record_target_processed_does_not_duplicate_the_row(session: Session) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=frozenset({DownmixTarget.STEREO})),
    )

    record_target_processed(
        session, file_path="/media/movie.mkv", language="eng", target=DownmixTarget.STEREO
    )

    assert len(list_target_statuses(session, "/media/movie.mkv")) == 1


def test_record_target_processed_creates_media_and_row_if_absent(session: Session) -> None:
    """Safe to call even if the file was never scanned first (defensive get-or-create)."""
    assert get_tracked_media(session, "/media/movie.mkv") is None

    row = record_target_processed(
        session, file_path="/media/movie.mkv", language="eng", target=DownmixTarget.STEREO
    )

    assert row.status == MediaTargetStatus.PROCESSED
    media = get_tracked_media(session, "/media/movie.mkv")
    assert media is not None
    assert media.file_path == "/media/movie.mkv"


# ---------------------------------------------------------------------------
# list_files_missing_targets.
# ---------------------------------------------------------------------------


def test_list_files_missing_targets_returns_files_with_a_missing_row(session: Session) -> None:
    settings = DownmixSettings(enabled_targets=ALL_TARGETS)
    upsert_tracked_media(
        session, file_path="/media/missing.mkv", streams=[_stream(channels=8)], settings=settings
    )
    upsert_tracked_media(
        session,
        file_path="/media/complete.mkv",
        streams=[_stream(channels=2)],
        settings=settings,
    )

    result = list_files_missing_targets(session, enabled_targets=ALL_TARGETS)

    assert [media.file_path for media in result] == ["/media/missing.mkv"]


def test_list_files_missing_targets_excludes_files_with_no_missing_rows(session: Session) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/complete.mkv",
        streams=[_stream(channels=2)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    assert list_files_missing_targets(session, enabled_targets=ALL_TARGETS) == []


def test_list_files_missing_targets_a_file_with_multiple_missing_rows_appears_once(
    session: Session,
) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    result = list_files_missing_targets(session, enabled_targets=ALL_TARGETS)

    assert [media.file_path for media in result] == ["/media/movie.mkv"]


def test_list_files_missing_targets_filters_by_the_given_enabled_targets(
    session: Session,
) -> None:
    """A stale MISSING row for a target outside enabled_targets doesn't count."""
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8)],
        settings=DownmixSettings(enabled_targets=ALL_TARGETS),
    )

    # Only Stereo is "currently enabled" from the caller's point of view; the
    # 2.1/5.1 MISSING rows exist but must not be considered.
    result = list_files_missing_targets(
        session, enabled_targets=frozenset({DownmixTarget.STEREO})
    )

    assert [media.file_path for media in result] == ["/media/movie.mkv"]

    # Now with a target that has no missing status at all for the file.
    upsert_tracked_media(
        session,
        file_path="/media/other.mkv",
        streams=[_stream(channels=2)],
        settings=DownmixSettings(enabled_targets=frozenset({DownmixTarget.STEREO})),
    )
    result = list_files_missing_targets(
        session, enabled_targets=frozenset({DownmixTarget.STEREO})
    )
    assert "/media/other.mkv" not in [media.file_path for media in result]


def test_list_files_missing_targets_ignores_excluded_by_language_filter_rows(
    session: Session,
) -> None:
    upsert_tracked_media(
        session,
        file_path="/media/movie.mkv",
        streams=[_stream(channels=8, language="fre")],
        settings=DownmixSettings(
            enabled_targets=ALL_TARGETS, language_allow_list=frozenset({"eng"})
        ),
    )

    assert list_files_missing_targets(session, enabled_targets=ALL_TARGETS) == []


def test_list_files_missing_targets_returns_empty_when_nothing_tracked(session: Session) -> None:
    assert list_files_missing_targets(session, enabled_targets=ALL_TARGETS) == []


# ---------------------------------------------------------------------------
# Public re-exports.
# ---------------------------------------------------------------------------


def test_media_package_importable_from_package_root() -> None:
    """Sanity check the public re-exports from collapsarr.media."""
    from collapsarr.media import MediaTargetStatus as ReexportedStatus
    from collapsarr.media import TrackedMediaFile as ReexportedFile
    from collapsarr.media import TrackedMediaTargetStatus as ReexportedTargetStatus
    from collapsarr.media import list_files_missing_targets as reexported_missing
    from collapsarr.media import list_tracked_media_by_instance as reexported_by_instance
    from collapsarr.media import upsert_tracked_media as reexported_upsert

    assert ReexportedFile is TrackedMediaFile
    assert ReexportedTargetStatus is TrackedMediaTargetStatus
    assert ReexportedStatus is MediaTargetStatus
    assert reexported_upsert is upsert_tracked_media
    assert reexported_missing is list_files_missing_targets
    assert reexported_by_instance is list_tracked_media_by_instance
