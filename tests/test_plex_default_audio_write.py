"""Tests for the direct Plex API write path for Default Audio Track (COL-245).

Every case drives :func:`~collapsarr.plex.default_audio_write.
apply_default_audio_via_plex` with an ``httpx.MockTransport`` -- no live
network call is made, mirroring ``tests/test_jobs_plex_analyze.py``'s pattern
-- asserting both the requests actually made (GET -> resolve -> PUT ->
GET-verify) and the resulting :class:`~collapsarr.plex.default_audio_write.
PlexDefaultAudioResult`'s outcome/success/error.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.orm import Session

from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.targets import DownmixTarget
from collapsarr.plex.default_audio_write import (
    PlexDefaultAudioOutcome,
    PlexDefaultAudioResult,
    apply_default_audio_via_plex,
)
from collapsarr.plex.models import PlexLibraryItem

BASE_URL = "http://plex.local:32400"
TOKEN = "plex-token"
FILE_PATH = "/media/movie.mkv"
RATING_KEY = "12345"

PREFERENCE = DefaultAudioPreference(language="eng", channel_tier=DownmixTarget.FIVE_POINT_ONE)


def _mapped(session: Session, *, file_path: str = FILE_PATH, rating_key: str = RATING_KEY) -> None:
    session.add(PlexLibraryItem(file_path=file_path, rating_key=rating_key, section_key="1"))
    session.commit()


def _stream_entry(
    *, stream_id: str, channels: int = 6, language_code: str = "eng", selected: bool = False
) -> dict[str, object]:
    return {
        "id": stream_id,
        "streamType": 2,
        "channels": channels,
        "languageCode": language_code,
        "selected": selected,
    }


def _metadata_payload(streams: list[dict[str, object]]) -> dict[str, object]:
    return {
        "MediaContainer": {
            "Metadata": [
                {
                    "ratingKey": RATING_KEY,
                    "Media": [{"id": 1, "Part": [{"id": 1, "Stream": streams}]}],
                }
            ]
        }
    }


# Pre-write: two eng streams, the 2.0 one currently selected. `PREFERENCE`
# (eng, 5.1) resolves to the 6-channel stream (id "2") as the winner.
_PRE_WRITE_STREAMS = [
    _stream_entry(stream_id="1", channels=2, selected=True),
    _stream_entry(stream_id="2", channels=6, selected=False),
]
_WINNER_STREAM_ID = "2"


def _sequenced_get_transport(
    *, get_payloads: list[object], put_status: int = 200
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport returning ``get_payloads`` in order across successive GETs.

    The last payload repeats for any GET beyond the list's length -- keeps
    tests that only care about the first GET's shape terse. Every PUT gets
    ``put_status`` (no body, matching Plex's own empty-body accept-and-queue
    response).
    """
    seen: list[httpx.Request] = []
    call_count = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "PUT":
            return httpx.Response(put_status)
        index = min(call_count["get"], len(get_payloads) - 1)
        call_count["get"] += 1
        return httpx.Response(200, json=get_payloads[index])

    return httpx.MockTransport(handler), seen


# ---------------------------------------------------------------------------
# Success: GET -> resolve -> PUT -> GET-verify, verify confirms selection.
# ---------------------------------------------------------------------------


def test_success_performs_get_resolve_put_get_verify_and_reports_success(
    session: Session,
) -> None:
    _mapped(session)
    pre_payload = _metadata_payload(_PRE_WRITE_STREAMS)
    verify_payload = _metadata_payload(
        [
            _stream_entry(stream_id="1", channels=2, selected=False),
            _stream_entry(stream_id="2", channels=6, selected=True),
        ]
    )
    transport, seen = _sequenced_get_transport(get_payloads=[pre_payload, verify_payload])

    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result == PlexDefaultAudioResult(
        outcome=PlexDefaultAudioOutcome.SUCCESS,
        success=True,
        detail=result.detail,
        rating_key=RATING_KEY,
        stream_id=_WINNER_STREAM_ID,
    )

    get_requests = [r for r in seen if r.method == "GET"]
    put_requests = [r for r in seen if r.method == "PUT"]
    assert [r.url.path for r in get_requests] == [
        f"/library/metadata/{RATING_KEY}",
        f"/library/metadata/{RATING_KEY}",
    ]
    assert len(put_requests) == 1
    assert put_requests[0].url.path == f"/library/metadata/{RATING_KEY}"
    assert put_requests[0].url.params["audioStreamID"] == _WINNER_STREAM_ID
    for request in seen:
        assert request.headers["X-Plex-Token"] == TOKEN
    # PUT happens strictly after the first GET and strictly before the second.
    assert seen.index(get_requests[0]) < seen.index(put_requests[0]) < seen.index(get_requests[1])


# ---------------------------------------------------------------------------
# Verification mismatch: PUT succeeds, but the verifying GET doesn't confirm it.
# ---------------------------------------------------------------------------


def test_verification_mismatch_fails_with_a_specific_error(session: Session) -> None:
    _mapped(session)
    pre_payload = _metadata_payload(_PRE_WRITE_STREAMS)
    # Verify GET still reports the *old* selection -- the write didn't take.
    transport, seen = _sequenced_get_transport(get_payloads=[pre_payload, pre_payload])

    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result.outcome is PlexDefaultAudioOutcome.VERIFY_MISMATCH
    assert result.success is False
    assert result.rating_key == RATING_KEY
    assert result.stream_id == _WINNER_STREAM_ID
    assert "did not take effect" in result.detail
    assert _WINNER_STREAM_ID in result.detail

    put_requests = [r for r in seen if r.method == "PUT"]
    get_requests = [r for r in seen if r.method == "GET"]
    assert len(put_requests) == 1  # the write was still attempted
    assert len(get_requests) == 2  # ...and verified, even though it failed


# ---------------------------------------------------------------------------
# ratingKey miss: cache and live-query fallback both fail -- a hard failure,
# no Plex API call at all.
# ---------------------------------------------------------------------------


def test_rating_key_miss_fails_with_a_specific_distinct_error_and_makes_no_plex_call(
    session: Session,
) -> None:
    """File is neither in the mapping-table cache nor tracked (no live-fallback
    scope to query by) -- resolution misses on both fronts."""
    transport, seen = _sequenced_get_transport(get_payloads=[{"MediaContainer": {}}])

    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result.outcome is PlexDefaultAudioOutcome.RATING_KEY_UNRESOLVED
    assert result.success is False
    assert result.rating_key is None
    assert result.stream_id is None
    assert "Could not resolve a Plex ratingKey" in result.detail
    assert seen == []  # no Plex API call at all -- no silent no-op, but no wasted call either


def test_rating_key_miss_and_verification_mismatch_errors_are_distinguishable(
    session: Session,
) -> None:
    """Both are failures, but AC requires the two error messages be distinct."""
    miss_transport, _ = _sequenced_get_transport(get_payloads=[{"MediaContainer": {}}])
    miss_result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=miss_transport
    )

    _mapped(session)
    pre_payload = _metadata_payload(_PRE_WRITE_STREAMS)
    mismatch_transport, _ = _sequenced_get_transport(get_payloads=[pre_payload, pre_payload])
    mismatch_result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=mismatch_transport
    )

    assert miss_result.outcome is not mismatch_result.outcome
    assert miss_result.detail != mismatch_result.detail


# ---------------------------------------------------------------------------
# Stream not yet ingested (COL-251): a downmix-triggered call passes
# expected_stream_count; Plex reporting fewer streams than that is a distinct
# hard failure, not a fall-through to resolution against a stale list.
# ---------------------------------------------------------------------------


def test_stream_not_yet_ingested_fails_distinctly_when_plex_reports_fewer_streams(
    session: Session,
) -> None:
    """Plex still reports only the pre-downmix streams -- the new one hasn't landed yet."""
    _mapped(session)
    pre_payload = _metadata_payload(_PRE_WRITE_STREAMS)  # 2 streams
    transport, seen = _sequenced_get_transport(get_payloads=[pre_payload])

    result = apply_default_audio_via_plex(
        session,
        FILE_PATH,
        PREFERENCE,
        base_url=BASE_URL,
        token=TOKEN,
        transport=transport,
        expected_stream_count=3,
    )

    assert result.outcome is PlexDefaultAudioOutcome.STREAM_NOT_YET_INGESTED
    assert result.success is False
    assert result.rating_key == RATING_KEY
    assert result.stream_id is None
    assert "hasn't finished ingesting" in result.detail
    assert [r.method for r in seen] == ["GET"]  # no resolution, no PUT, no verify attempted


def test_stream_not_yet_ingested_is_distinct_from_rating_key_unresolved(
    session: Session,
) -> None:
    """AC: a distinct, specific error -- not the generic ratingKey-resolution-failure message."""
    _mapped(session)
    pre_payload = _metadata_payload(_PRE_WRITE_STREAMS)
    transport, _ = _sequenced_get_transport(get_payloads=[pre_payload])

    result = apply_default_audio_via_plex(
        session,
        FILE_PATH,
        PREFERENCE,
        base_url=BASE_URL,
        token=TOKEN,
        transport=transport,
        expected_stream_count=3,
    )

    assert result.outcome is not PlexDefaultAudioOutcome.RATING_KEY_UNRESOLVED
    assert "Could not resolve a Plex ratingKey" not in result.detail


def test_expected_stream_count_met_proceeds_to_resolution_as_normal(session: Session) -> None:
    """Plex already reports at least as many streams as expected: no special-casing."""
    _mapped(session)
    pre_payload = _metadata_payload(_PRE_WRITE_STREAMS)  # 2 streams
    verify_payload = _metadata_payload(
        [
            _stream_entry(stream_id="1", channels=2, selected=False),
            _stream_entry(stream_id="2", channels=6, selected=True),
        ]
    )
    transport, seen = _sequenced_get_transport(get_payloads=[pre_payload, verify_payload])

    result = apply_default_audio_via_plex(
        session,
        FILE_PATH,
        PREFERENCE,
        base_url=BASE_URL,
        token=TOKEN,
        transport=transport,
        expected_stream_count=2,
    )

    assert result.outcome is PlexDefaultAudioOutcome.SUCCESS
    assert [r.method for r in seen] == ["GET", "PUT", "GET"]


def test_expected_stream_count_unset_never_checks_the_count(session: Session) -> None:
    """An immediate trigger (no expected_stream_count) never hits this check at all."""
    _mapped(session)
    single_stream_payload = _metadata_payload([_stream_entry(stream_id="1", selected=True)])
    transport, seen = _sequenced_get_transport(get_payloads=[single_stream_payload])

    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result.outcome is PlexDefaultAudioOutcome.NOTHING_TO_DO
    assert [r.method for r in seen] == ["GET"]


# ---------------------------------------------------------------------------
# Additional failure modes, for completeness alongside the three AC-mandated cases.
# ---------------------------------------------------------------------------


def test_nothing_to_do_when_fewer_than_two_streams_makes_no_write(session: Session) -> None:
    _mapped(session)
    single_stream_payload = _metadata_payload([_stream_entry(stream_id="1", selected=True)])
    transport, seen = _sequenced_get_transport(get_payloads=[single_stream_payload])

    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result.outcome is PlexDefaultAudioOutcome.NOTHING_TO_DO
    assert result.success is True
    assert result.rating_key == RATING_KEY
    assert result.stream_id is None
    assert [r.method for r in seen] == ["GET"]  # only the one fetch, no PUT, no verify


def test_stream_fetch_failure_fails_with_a_specific_error_and_makes_no_write(
    session: Session,
) -> None:
    _mapped(session)
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))

    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result.outcome is PlexDefaultAudioOutcome.STREAM_FETCH_FAILED
    assert result.success is False
    assert result.rating_key == RATING_KEY


def test_write_failure_fails_with_a_specific_error(session: Session) -> None:
    _mapped(session)
    pre_payload = _metadata_payload(_PRE_WRITE_STREAMS)
    transport, seen = _sequenced_get_transport(get_payloads=[pre_payload], put_status=500)

    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result.outcome is PlexDefaultAudioOutcome.WRITE_FAILED
    assert result.success is False
    assert result.rating_key == RATING_KEY
    assert result.stream_id == _WINNER_STREAM_ID
    put_requests = [r for r in seen if r.method == "PUT"]
    get_requests = [r for r in seen if r.method == "GET"]
    assert len(put_requests) == 1
    assert len(get_requests) == 1  # never reached the verifying GET


def test_verify_fetch_failure_is_distinct_from_a_verification_mismatch(session: Session) -> None:
    """The verifying GET itself failing ("we don't know") must be reported
    distinctly from a GET that succeeds but doesn't confirm the write
    ("we know it didn't")."""
    _mapped(session)
    pre_payload = _metadata_payload(_PRE_WRITE_STREAMS)
    call_count = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200)
        call_count["get"] += 1
        if call_count["get"] == 1:
            return httpx.Response(200, json=pre_payload)
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)

    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result.outcome is PlexDefaultAudioOutcome.VERIFY_FETCH_FAILED
    assert result.success is False


def test_reports_rating_key_unresolved_when_the_resolver_itself_returns_none(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirms this module's own dispatch on a ``None`` resolution, independent
    of *why* ``resolve_rating_key`` returned it (already covered end to end by
    ``test_rating_key_miss_...`` above)."""
    import collapsarr.plex.default_audio_write as module

    monkeypatch.setattr(module, "resolve_rating_key", lambda *a, **k: None)

    transport, seen = _sequenced_get_transport(get_payloads=[{"MediaContainer": {}}])
    result = apply_default_audio_via_plex(
        session, FILE_PATH, PREFERENCE, base_url=BASE_URL, token=TOKEN, transport=transport
    )

    assert result.outcome is PlexDefaultAudioOutcome.RATING_KEY_UNRESOLVED
    assert seen == []
