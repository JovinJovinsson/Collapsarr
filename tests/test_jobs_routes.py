"""Contract tests for the job history & trigger REST endpoints (COL-29).

Covers request/response shape and the API-key-required behaviour (COL-26) for
``GET /api/jobs/history``, ``POST /api/jobs/scan``, and ``POST /api/jobs/trigger``.

History rows are seeded through the real :class:`~collapsarr.jobs.models.JobHistory`
model into the same SQLite file the ``client`` app reads (via the shared
``session`` fixture), so the GET exercises the genuine
:func:`~collapsarr.jobs.history.list_job_history` query. The scan/trigger POSTs
wrap the live :class:`~collapsarr.jobs.scheduler.JobScheduler`; a fake scheduler
injected via ``dependency_overrides`` drives their response shapes deterministically
(no ffprobe or configured instances needed), plus one test hits a real
``enable_scheduler=True`` app to prove the wiring end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.arr.catalog import (
    CatalogEpisode,
    CatalogMovie,
    CatalogSeries,
    RadarrCatalog,
    SonarrCatalog,
)
from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.config import Settings
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.jobs.models import JobHistory
from collapsarr.jobs.queue import Job, JobKind, JobStatus
from collapsarr.jobs.routes import get_job_scheduler
from collapsarr.library.models import LibraryNodeKind, make_node_key
from collapsarr.library.service import list_nodes, sync_library
from collapsarr.main import create_app
from collapsarr.media.service import upsert_tracked_media
from collapsarr.settings.service import get_global_settings, update_global_settings

UNREACHABLE_URL = "http://127.0.0.1:9"


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _seed_history(
    session: Session,
    *,
    job_id: str,
    file_path: str,
    status: JobStatus,
    kind: JobKind = JobKind.DOWNMIX,
) -> None:
    session.add(JobHistory(job_id=job_id, file_path=file_path, status=status, kind=kind))
    session.commit()


class _FakeScheduler:
    """Stand-in for :class:`JobScheduler` recording calls and returning fixed jobs."""

    def __init__(
        self,
        *,
        scan_jobs: list[Job] | None = None,
        trigger_job: Job | None = None,
        default_audio_trigger_job: Job | None = None,
        default_audio_trigger_jobs_by_file: dict[str, Job | None] | None = None,
        cancel_result: bool | None = True,
    ) -> None:
        self._scan_jobs = scan_jobs or []
        self._trigger_job = trigger_job
        self._default_audio_trigger_job = default_audio_trigger_job
        #: Per-file override for the bulk endpoint's tests, where a fixed
        #: single job/None (the field above) can't tell different resolved
        #: files apart. Falls back to the fixed value above when unset.
        self._default_audio_trigger_jobs_by_file = default_audio_trigger_jobs_by_file
        #: COL-168's ``cancel_job`` result, mirroring the real
        #: :meth:`~collapsarr.jobs.scheduler.JobScheduler.cancel_job`'s
        #: three-way contract: ``None`` (404, "no such job"), ``True``
        #: (cancelled), ``False`` (too late). Defaults to ``True`` so a test
        #: that doesn't care about cancel behaviour still gets a sane value.
        self._cancel_result = cancel_result
        self.trigger_calls: list[tuple[str, frozenset[str]]] = []
        self.default_audio_trigger_calls: list[str] = []
        self.cancel_calls: list[UUID] = []

    def scan_now(self) -> list[Job]:
        return self._scan_jobs

    def trigger_file(
        self,
        file_path: str,
        *,
        extra_languages: Iterable[str] | None = None,
        session: Session | None = None,
    ) -> Job | None:
        self.trigger_calls.append(
            (file_path, frozenset(extra_languages) if extra_languages is not None else frozenset())
        )
        return self._trigger_job

    def trigger_set_default_audio(
        self,
        file_path: str,
        *,
        session: Session | None = None,
    ) -> Job | None:
        self.default_audio_trigger_calls.append(file_path)
        if self._default_audio_trigger_jobs_by_file is not None:
            return self._default_audio_trigger_jobs_by_file.get(file_path)
        return self._default_audio_trigger_job

    def cancel_job(self, job_id: UUID, *, session: Session | None = None) -> bool | None:
        self.cancel_calls.append(job_id)
        return self._cancel_result


def _job(file_path: str) -> Job:
    return Job(file_path=Path(file_path), settings=DownmixSettings())


# --- GET /api/jobs/history: shape & filters ----------------------------------


def test_history_is_empty_when_nothing_recorded(client: TestClient) -> None:
    response = client.get("/api/jobs/history", headers=_auth_headers(client))
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_history_lists_rows_with_full_shape(client: TestClient, session: Session) -> None:
    _seed_history(session, job_id="job-1", file_path="/media/a.mkv", status=JobStatus.SUCCEEDED)

    response = client.get("/api/jobs/history", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["job_id"] == "job-1"
    assert row["file_path"] == "/media/a.mkv"
    assert row["status"] == "succeeded"
    assert row["kind"] == "downmix"  # COL-155: a bare JobHistory() row defaults to DOWNMIX
    for key in (
        "id",
        "started_at",
        "ended_at",
        "exit_code",
        "error_text",
        "target",
        "language",
        "created_at",
        "updated_at",
    ):
        assert key in row


def test_history_filters_by_file(client: TestClient, session: Session) -> None:
    _seed_history(session, job_id="j-a", file_path="/media/a.mkv", status=JobStatus.SUCCEEDED)
    _seed_history(session, job_id="j-b", file_path="/media/b.mkv", status=JobStatus.SUCCEEDED)

    response = client.get(
        "/api/jobs/history", params={"file": "/media/a.mkv"}, headers=_auth_headers(client)
    )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [r["file_path"] for r in rows] == ["/media/a.mkv"]


def test_history_filters_by_status(client: TestClient, session: Session) -> None:
    _seed_history(session, job_id="j-ok", file_path="/media/a.mkv", status=JobStatus.SUCCEEDED)
    _seed_history(session, job_id="j-bad", file_path="/media/b.mkv", status=JobStatus.FAILED)

    response = client.get(
        "/api/jobs/history", params={"status": "failed"}, headers=_auth_headers(client)
    )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [r["job_id"] for r in rows] == ["j-bad"]


def test_history_combines_file_and_status_filters(client: TestClient, session: Session) -> None:
    _seed_history(session, job_id="j1", file_path="/media/a.mkv", status=JobStatus.SUCCEEDED)
    _seed_history(session, job_id="j2", file_path="/media/a.mkv", status=JobStatus.FAILED)

    response = client.get(
        "/api/jobs/history",
        params={"file": "/media/a.mkv", "status": "failed"},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["job_id"] == "j2"


def test_history_rejects_an_unknown_status_value(client: TestClient) -> None:
    response = client.get(
        "/api/jobs/history", params={"status": "bogus"}, headers=_auth_headers(client)
    )
    assert response.status_code == 422


def test_history_filters_by_kind(client: TestClient, session: Session) -> None:
    _seed_history(
        session,
        job_id="j-downmix",
        file_path="/media/a.mkv",
        status=JobStatus.SUCCEEDED,
        kind=JobKind.DOWNMIX,
    )
    _seed_history(
        session,
        job_id="j-default-audio",
        file_path="/media/b.mkv",
        status=JobStatus.SUCCEEDED,
        kind=JobKind.SET_DEFAULT_AUDIO,
    )

    response = client.get(
        "/api/jobs/history", params={"kind": "set_default_audio"}, headers=_auth_headers(client)
    )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [r["job_id"] for r in rows] == ["j-default-audio"]


def test_history_rejects_an_unknown_kind_value(client: TestClient) -> None:
    response = client.get(
        "/api/jobs/history", params={"kind": "bogus"}, headers=_auth_headers(client)
    )
    assert response.status_code == 422


# --- POST /api/jobs/scan -----------------------------------------------------


def test_scan_now_returns_the_enqueued_jobs(client: TestClient) -> None:
    fake = _FakeScheduler(scan_jobs=[_job("/media/a.mkv"), _job("/media/b.mkv")])
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/scan", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert [j["file_path"] for j in body["enqueued"]] == ["/media/a.mkv", "/media/b.mkv"]
    assert all(j["status"] == "pending" for j in body["enqueued"])
    assert all(j["id"] for j in body["enqueued"])


def test_scan_now_wires_through_a_real_scheduler(settings: Settings) -> None:
    """End-to-end: a real enable_scheduler app scans (no instances -> nothing)."""
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app) as client:
        response = client.post("/api/jobs/scan", headers=_auth_headers(client))

    assert response.status_code == 202, response.text
    assert response.json() == {"enqueued": []}


def test_scan_now_is_503_when_no_scheduler_is_wired(client: TestClient) -> None:
    """The default app has no scheduler -> scan fails loudly, not silently."""
    response = client.post("/api/jobs/scan", headers=_auth_headers(client))
    assert response.status_code == 503


# --- POST /api/jobs/trigger --------------------------------------------------


def test_trigger_enqueues_a_job_and_returns_it(client: TestClient) -> None:
    fake = _FakeScheduler(trigger_job=_job("/media/movie.mkv"))
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger",
            json={"file_path": "/media/movie.mkv"},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["enqueued"] is True
    assert body["job"]["file_path"] == "/media/movie.mkv"
    assert body["job"]["status"] == "pending"
    assert fake.trigger_calls == [("/media/movie.mkv", frozenset())]


def test_trigger_threads_extra_languages_as_the_bypass_option(client: TestClient) -> None:
    fake = _FakeScheduler(trigger_job=_job("/media/movie.mkv"))
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger",
            json={"file_path": "/media/movie.mkv", "extra_languages": ["jpn", "kor"]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    assert fake.trigger_calls == [("/media/movie.mkv", frozenset({"jpn", "kor"}))]


def test_trigger_reports_not_enqueued_when_the_file_is_skipped(client: TestClient) -> None:
    fake = _FakeScheduler(trigger_job=None)  # duplicate / nothing to do
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger",
            json={"file_path": "/media/movie.mkv"},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["enqueued"] is False
    assert body["job"] is None


def test_trigger_rejects_unknown_body_fields(client: TestClient) -> None:
    fake = _FakeScheduler(trigger_job=None)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger",
            json={"file_path": "/media/movie.mkv", "bogus": True},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text


# --- POST /api/jobs/trigger-default-audio (COL-155) --------------------------


def test_trigger_default_audio_enqueues_a_job_and_returns_it(client: TestClient) -> None:
    fake = _FakeScheduler(default_audio_trigger_job=_job("/media/movie.mkv"))
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio",
            json={"file_path": "/media/movie.mkv"},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["enqueued"] is True
    assert body["job"]["file_path"] == "/media/movie.mkv"
    assert body["job"]["status"] == "pending"
    assert fake.default_audio_trigger_calls == ["/media/movie.mkv"]


def test_trigger_default_audio_reports_not_enqueued_when_the_file_is_skipped(
    client: TestClient,
) -> None:
    fake = _FakeScheduler(default_audio_trigger_job=None)  # no preference / duplicate / correct
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio",
            json={"file_path": "/media/movie.mkv"},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["enqueued"] is False
    assert body["job"] is None


def test_trigger_default_audio_rejects_unknown_body_fields(client: TestClient) -> None:
    fake = _FakeScheduler(default_audio_trigger_job=None)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio",
            json={"file_path": "/media/movie.mkv", "extra_languages": ["jpn"]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text


def test_trigger_default_audio_is_503_when_no_scheduler_is_wired(client: TestClient) -> None:
    """The default app has no scheduler -> the trigger fails loudly, not silently."""
    response = client.post(
        "/api/jobs/trigger-default-audio",
        json={"file_path": "/media/movie.mkv"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 503


def test_trigger_default_audio_wires_through_a_real_scheduler(settings: Settings) -> None:
    """End-to-end: a real enable_scheduler app, no preference configured -> skipped."""
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/trigger-default-audio",
            json={"file_path": "/media/movie.mkv"},
            headers=_auth_headers(client),
        )

    assert response.status_code == 202, response.text
    assert response.json() == {"enqueued": False, "job": None}


# --- POST /api/jobs/trigger-default-audio/bulk (COL-156) ---------------------
#
# Mirrors tests/test_library_routes.py's POST /api/library/tracked (COL-101)
# bulk-Tracked-update test shape: seed a real Library via sync_library, resolve
# a node id by its deterministic node_key, exercise single/cascading/mixed
# reference batches, and the same 404 (unknown node)/422 (kind mismatch)
# validation. The de-duplicating cascade-to-leaves resolution itself
# (Series/Season -> descendant Episode/Movie files) is
# :func:`collapsarr.jobs.routes._resolve_default_audio_leaf_files`; these
# tests exercise it only through the HTTP contract.


def _seed_instance(session: Session, *, type_: InstanceType = InstanceType.SONARR) -> int:
    instance = ArrInstance(
        name=f"Instance {type_.value}", type=type_, base_url=UNREACHABLE_URL, api_key="k"
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    return instance.id


def _sonarr_catalog(instance_id: int) -> SonarrCatalog:
    return SonarrCatalog(
        instance_id=instance_id,
        series=(
            CatalogSeries(
                series_id=1,
                title="Breaking Bad",
                season_numbers=(1,),
                episodes=(
                    CatalogEpisode(101, 1, 1, "Pilot", has_file=True),
                    CatalogEpisode(102, 1, 2, "Cat's in the Bag", has_file=True),
                    # Never bridged to a TrackedMediaFile row by any test below --
                    # exercises the "leaf with no known file is silently excluded"
                    # behaviour even mid-cascade.
                    CatalogEpisode(103, 1, 3, "...And the Bag's in the River", has_file=True),
                ),
            ),
        ),
    )


def _radarr_catalog(instance_id: int) -> RadarrCatalog:
    return RadarrCatalog(
        instance_id=instance_id, movies=(CatalogMovie(movie_id=1, title="Arrival", has_file=True),)
    )


def _seed_library(client: TestClient, *, type_: InstanceType = InstanceType.SONARR) -> int:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        instance_id = _seed_instance(session, type_=type_)
        if type_ is InstanceType.SONARR:
            sync_library(session, instance_id=instance_id, catalog=_sonarr_catalog(instance_id))
        else:
            sync_library(session, instance_id=instance_id, catalog=_radarr_catalog(instance_id))
        return instance_id


def _node_id(client: TestClient, instance_id: int, node_key: str) -> int:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        match = next(n for n in list_nodes(session, instance_id) if n.node_key == node_key)
        return match.id


def _bridge_file(
    client: TestClient,
    *,
    file_path: str,
    instance_id: int,
    sonarr_episode_id: int | None = None,
    radarr_movie_id: int | None = None,
) -> None:
    """Bridge ``file_path`` to a Library node the same way a scan/webhook probe would (COL-154)."""
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        upsert_tracked_media(
            session,
            file_path=file_path,
            streams=[
                AudioStreamInfo(
                    index=0, codec="ac3", channels=2, channel_layout="stereo", language="eng"
                )
            ],
            settings=DownmixSettings(enabled_targets=frozenset({DownmixTarget.STEREO})),
            instance_id=instance_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_movie_id=radarr_movie_id,
        )


def test_bulk_trigger_default_audio_resolves_an_episode_reference_to_its_file(
    client: TestClient,
) -> None:
    instance_id = _seed_library(client)
    episode_id = _node_id(
        client, instance_id, make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=101)
    )
    _bridge_file(
        client, file_path="/media/pilot.mkv", instance_id=instance_id, sonarr_episode_id=101
    )

    job = _job("/media/pilot.mkv")
    fake = _FakeScheduler(default_audio_trigger_jobs_by_file={"/media/pilot.mkv": job})
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={"references": [{"node_type": "episode", "node_id": episode_id}]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    results = response.json()["results"]
    assert results == [
        {
            "file_path": "/media/pilot.mkv",
            "enqueued": True,
            "job": {"id": str(job.id), "file_path": "/media/pilot.mkv", "status": "pending"},
        }
    ]
    assert fake.default_audio_trigger_calls == ["/media/pilot.mkv"]


def test_bulk_trigger_default_audio_cascades_a_series_reference_to_descendant_files(
    client: TestClient,
) -> None:
    instance_id = _seed_library(client)
    series_id = _node_id(client, instance_id, make_node_key(LibraryNodeKind.SERIES, series_id=1))
    _bridge_file(
        client, file_path="/media/e101.mkv", instance_id=instance_id, sonarr_episode_id=101
    )
    _bridge_file(
        client, file_path="/media/e102.mkv", instance_id=instance_id, sonarr_episode_id=102
    )
    # Episode 103 is deliberately left unbridged (see _sonarr_catalog) -- it
    # must not appear in the result even though the cascade reaches it.

    fake = _FakeScheduler(
        default_audio_trigger_jobs_by_file={
            "/media/e101.mkv": _job("/media/e101.mkv"),
            "/media/e102.mkv": None,
        }
    )
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={"references": [{"node_type": "series", "node_id": series_id}]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    results = response.json()["results"]
    assert {(r["file_path"], r["enqueued"]) for r in results} == {
        ("/media/e101.mkv", True),
        ("/media/e102.mkv", False),
    }
    assert set(fake.default_audio_trigger_calls) == {"/media/e101.mkv", "/media/e102.mkv"}


def test_bulk_trigger_default_audio_cascade_includes_a_hidden_descendant_episode(
    client: TestClient,
) -> None:
    """A Series-level cascade must still reach a soft-hidden descendant Episode.

    Regression test for the ``include_hidden=False`` divergence fixed in
    :func:`~collapsarr.jobs.routes._resolve_default_audio_leaf_files` -- a node
    soft-hidden by a later sync (dropped from the catalog, not deleted, see
    :func:`~collapsarr.library.service.sync_library`) still has a real on-disk
    file, so it must stay in scope for the cascade exactly like
    :func:`~collapsarr.library.service.set_tracked`'s cascade. Mirrors the
    hidden-node fixture pattern from
    ``tests.test_library_service.test_disappeared_node_is_hidden_then_reappears_with_override``:
    sync once with the full catalog, then again with a catalog that drops one
    episode, soft-hiding it without deleting it.
    """
    instance_id = _seed_library(client)
    series_id = _node_id(client, instance_id, make_node_key(LibraryNodeKind.SERIES, series_id=1))

    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        # Episode 103 dropped from this catalog -> soft-hidden, not deleted.
        reduced = SonarrCatalog(
            instance_id=instance_id,
            series=(
                CatalogSeries(
                    series_id=1,
                    title="Breaking Bad",
                    season_numbers=(1,),
                    episodes=(
                        CatalogEpisode(101, 1, 1, "Pilot", has_file=True),
                        CatalogEpisode(102, 1, 2, "Cat's in the Bag", has_file=True),
                    ),
                ),
            ),
        )
        sync_library(session, instance_id=instance_id, catalog=reduced)
        hidden_episode = next(
            n
            for n in list_nodes(session, instance_id)
            if n.node_key == make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=103)
        )
        assert hidden_episode.hidden is True  # sanity: this test needs a genuinely hidden node

    _bridge_file(
        client, file_path="/media/e103.mkv", instance_id=instance_id, sonarr_episode_id=103
    )

    fake = _FakeScheduler(
        default_audio_trigger_jobs_by_file={"/media/e103.mkv": _job("/media/e103.mkv")}
    )
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={"references": [{"node_type": "series", "node_id": series_id}]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    file_paths = {r["file_path"] for r in response.json()["results"]}
    # The hidden episode's file must still surface here -- proves the cascade
    # walks list_nodes' hidden-included set. Against the old
    # include_hidden=False behaviour this file is silently dropped and both
    # assertions below fail.
    assert "/media/e103.mkv" in file_paths
    assert "/media/e103.mkv" in fake.default_audio_trigger_calls


def test_bulk_trigger_default_audio_deduplicates_a_file_reachable_via_two_references(
    client: TestClient,
) -> None:
    """A Series reference plus a standalone reference to one of its own episodes."""
    instance_id = _seed_library(client)
    series_id = _node_id(client, instance_id, make_node_key(LibraryNodeKind.SERIES, series_id=1))
    episode_id = _node_id(
        client, instance_id, make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=101)
    )
    _bridge_file(
        client, file_path="/media/e101.mkv", instance_id=instance_id, sonarr_episode_id=101
    )
    _bridge_file(
        client, file_path="/media/e102.mkv", instance_id=instance_id, sonarr_episode_id=102
    )

    fake = _FakeScheduler(
        default_audio_trigger_jobs_by_file={
            "/media/e101.mkv": _job("/media/e101.mkv"),
            "/media/e102.mkv": _job("/media/e102.mkv"),
        }
    )
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={
                "references": [
                    {"node_type": "series", "node_id": series_id},
                    {"node_type": "episode", "node_id": episode_id},
                ]
            },
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    results = response.json()["results"]
    file_paths = [r["file_path"] for r in results]
    assert sorted(file_paths) == ["/media/e101.mkv", "/media/e102.mkv"]  # each file once
    # trigger_set_default_audio was called exactly once per unique file, not per reference.
    assert sorted(fake.default_audio_trigger_calls) == ["/media/e101.mkv", "/media/e102.mkv"]


def test_bulk_trigger_default_audio_accepts_a_mixed_sonarr_and_radarr_batch(
    client: TestClient,
) -> None:
    sonarr_instance_id = _seed_library(client, type_=InstanceType.SONARR)
    radarr_instance_id = _seed_library(client, type_=InstanceType.RADARR)
    episode_id = _node_id(
        client,
        sonarr_instance_id,
        make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=101),
    )
    movie_id = _node_id(
        client, radarr_instance_id, make_node_key(LibraryNodeKind.MOVIE, movie_id=1)
    )
    _bridge_file(
        client,
        file_path="/media/e101.mkv",
        instance_id=sonarr_instance_id,
        sonarr_episode_id=101,
    )
    _bridge_file(
        client, file_path="/media/arrival.mkv", instance_id=radarr_instance_id, radarr_movie_id=1
    )

    fake = _FakeScheduler(
        default_audio_trigger_jobs_by_file={
            "/media/e101.mkv": _job("/media/e101.mkv"),
            "/media/arrival.mkv": _job("/media/arrival.mkv"),
        }
    )
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={
                "references": [
                    {"node_type": "episode", "node_id": episode_id},
                    {"node_type": "movie", "node_id": movie_id},
                ]
            },
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    file_paths = {r["file_path"] for r in response.json()["results"]}
    assert file_paths == {"/media/e101.mkv", "/media/arrival.mkv"}


def test_bulk_trigger_default_audio_returns_no_results_for_an_unbridged_leaf(
    client: TestClient,
) -> None:
    """An Episode with no scanned/probed TrackedMediaFile row has no known file to trigger."""
    instance_id = _seed_library(client)
    episode_id = _node_id(
        client, instance_id, make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=103)
    )

    fake = _FakeScheduler()
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={"references": [{"node_type": "episode", "node_id": episode_id}]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    assert response.json()["results"] == []
    assert fake.default_audio_trigger_calls == []


def test_bulk_trigger_default_audio_unknown_node_id_returns_404(client: TestClient) -> None:
    _seed_library(client)
    fake = _FakeScheduler()
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={"references": [{"node_type": "episode", "node_id": 999999}]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_bulk_trigger_default_audio_mismatched_node_type_returns_422(client: TestClient) -> None:
    instance_id = _seed_library(client)
    series_id = _node_id(client, instance_id, make_node_key(LibraryNodeKind.SERIES, series_id=1))
    fake = _FakeScheduler()
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={"references": [{"node_type": "episode", "node_id": series_id}]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert fake.default_audio_trigger_calls == []


def test_bulk_trigger_default_audio_rejects_unknown_body_fields(client: TestClient) -> None:
    fake = _FakeScheduler()
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={
                "references": [{"node_type": "episode", "node_id": 1}],
                "file_path": "/media/movie.mkv",
            },
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text


def test_bulk_trigger_default_audio_requires_at_least_one_reference(client: TestClient) -> None:
    fake = _FakeScheduler()
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={"references": []},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text


def test_bulk_trigger_default_audio_is_503_when_no_scheduler_is_wired(client: TestClient) -> None:
    """The default app has no scheduler -> the trigger fails loudly, not silently."""
    response = client.post(
        "/api/jobs/trigger-default-audio/bulk",
        json={"references": [{"node_type": "episode", "node_id": 1}]},
        headers=_auth_headers(client),
    )
    assert response.status_code == 503


def test_bulk_trigger_default_audio_wires_through_a_real_scheduler(settings: Settings) -> None:
    """End-to-end: a real enable_scheduler app, no preference configured -> skipped."""
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            instance_id = _seed_instance(session)
            sync_library(session, instance_id=instance_id, catalog=_sonarr_catalog(instance_id))
        episode_id = _node_id(
            client,
            instance_id,
            make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=101),
        )
        _bridge_file(
            client, file_path="/media/pilot.mkv", instance_id=instance_id, sonarr_episode_id=101
        )
        response = client.post(
            "/api/jobs/trigger-default-audio/bulk",
            json={"references": [{"node_type": "episode", "node_id": episode_id}]},
            headers=_auth_headers(client),
        )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "results": [{"file_path": "/media/pilot.mkv", "enqueued": False, "job": None}]
    }


# --- DELETE /api/jobs/{job_id} (COL-168) --------------------------------------
#
# These are HTTP-contract tests only -- request/response shape, the id ->
# scheduler.cancel_job(UUID) call, and the None/True/False -> 404/200 mapping
# -- via the same fake-scheduler dependency_overrides pattern as every other
# endpoint above. JobScheduler.cancel_job's own behaviour (the real
# JobQueue.cancel + JobHistory-delete sequence) is exercised directly, with a
# real queue/session, in tests/test_jobs_scheduler.py.


def test_cancel_job_returns_cancelled_true_on_success(client: TestClient) -> None:
    job_id = uuid4()
    fake = _FakeScheduler(cancel_result=True)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.delete(f"/api/jobs/{job_id}", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {"cancelled": True}
    assert fake.cancel_calls == [job_id]


def test_cancel_job_returns_cancelled_false_when_already_claimed(client: TestClient) -> None:
    """A no-longer-PENDING Job is "too late", not an error and not a silent success."""
    job_id = uuid4()
    fake = _FakeScheduler(cancel_result=False)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.delete(f"/api/jobs/{job_id}", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {"cancelled": False}
    assert fake.cancel_calls == [job_id]


def test_cancel_job_returns_404_for_a_job_not_in_the_live_queue(client: TestClient) -> None:
    job_id = uuid4()
    fake = _FakeScheduler(cancel_result=None)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.delete(f"/api/jobs/{job_id}", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert fake.cancel_calls == [job_id]


def test_cancel_job_returns_404_for_a_malformed_job_id_without_calling_the_scheduler(
    client: TestClient,
) -> None:
    fake = _FakeScheduler()
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.delete("/api/jobs/not-a-uuid", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert fake.cancel_calls == []  # not a UUID at all -- never reaches the scheduler


def test_cancel_job_is_503_when_no_scheduler_is_wired(client: TestClient) -> None:
    """The default app has no scheduler -> cancel fails loudly, not silently."""
    response = client.delete(f"/api/jobs/{uuid4()}", headers=_auth_headers(client))
    assert response.status_code == 503


def test_cancel_job_wires_through_a_real_scheduler(settings: Settings) -> None:
    """End-to-end: a real enable_scheduler app, unknown id -> 404 (nothing to act on)."""
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app) as client:
        response = client.delete(f"/api/jobs/{uuid4()}", headers=_auth_headers(client))

    assert response.status_code == 404


# --- auth-required behaviour --------------------------------------------------


def test_history_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    assert client.get("/api/jobs/history").status_code == 401


def test_scan_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    assert client.post("/api/jobs/scan").status_code == 401


def test_trigger_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    response = client.post("/api/jobs/trigger", json={"file_path": "/media/movie.mkv"})
    assert response.status_code == 401


def test_trigger_default_audio_endpoint_requires_the_api_key(
    client: TestClient, session: Session
) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    response = client.post(
        "/api/jobs/trigger-default-audio", json={"file_path": "/media/movie.mkv"}
    )
    assert response.status_code == 401


def test_bulk_trigger_default_audio_endpoint_requires_the_api_key(
    client: TestClient, session: Session
) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    response = client.post(
        "/api/jobs/trigger-default-audio/bulk",
        json={"references": [{"node_type": "episode", "node_id": 1}]},
    )
    assert response.status_code == 401


def test_cancel_job_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    response = client.delete(f"/api/jobs/{uuid4()}")
    assert response.status_code == 401
