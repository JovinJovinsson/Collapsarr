"""Contract tests for the job history & trigger REST endpoints (COL-29).

Covers request/response shape and the API-key-required behaviour (COL-26) for
``GET /api/jobs/history``, ``POST /api/jobs/scan``, ``POST /api/jobs/trigger``,
``POST /api/jobs/requeue`` (COL-170), and ``POST /api/jobs/requeue-failed``
(COL-172).

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

import pytest
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
from collapsarr.arr.files import MonitoredFile
from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.config import Settings
from collapsarr.downmix.probe import AudioStreamInfo
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.jobs import scheduler as scheduler_module
from collapsarr.jobs.models import JobHistory
from collapsarr.jobs.queue import Job, JobKind, JobQueue, JobStatus
from collapsarr.jobs.routes import get_job_scheduler
from collapsarr.jobs.scheduler import BulkRequeueResult, ClearQueueResult, JobScheduler
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
        requeue_job: Job | None = None,
        requeue_all_failed_result: BulkRequeueResult | None = None,
        default_audio_trigger_job: Job | None = None,
        default_audio_trigger_jobs_by_file: dict[str, Job | None] | None = None,
        cancel_result: bool | None = True,
        bump_result: bool | None = True,
        clear_queue_result: ClearQueueResult | None = None,
    ) -> None:
        self._scan_jobs = scan_jobs or []
        self._trigger_job = trigger_job
        #: COL-170's ``requeue_file`` result -- the per-row Requeue endpoint's
        #: fixed return value. Defaults to ``None`` (skipped) so a test that
        #: doesn't care still gets a sane, unenqueued response.
        self._requeue_job = requeue_job
        #: COL-172's ``requeue_all_failed`` result -- the bulk "Requeue all
        #: failed" endpoint's fixed return value. Defaults to an empty
        #: all-skipped-nothing-to-do split so a test that doesn't care still
        #: gets a well-formed, empty response.
        self._requeue_all_failed_result = requeue_all_failed_result or BulkRequeueResult(
            requeued=[], skipped=[]
        )
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
        #: COL-169's ``bump_job_to_front`` result, mirroring the real
        #: :meth:`~collapsarr.jobs.scheduler.JobScheduler.bump_job_to_front`'s
        #: three-way contract: ``None`` (404, "no such job"), ``True``
        #: (bumped), ``False`` (too late). Defaults to ``True`` so a test
        #: that doesn't care about bump behaviour still gets a sane value.
        self._bump_result = bump_result
        #: COL-173's ``clear_queue`` result -- the bulk "Clear queue" cancel
        #: endpoint's fixed return value. Defaults to a zero-cancelled,
        #: zero-already-running split so a test that doesn't care still gets
        #: a well-formed, empty-queue-shaped response.
        self._clear_queue_result = clear_queue_result or ClearQueueResult(
            cancelled=0, already_running=0
        )
        self.trigger_calls: list[tuple[str, frozenset[str], bool]] = []
        self.requeue_calls: list[str] = []
        self.requeue_all_failed_calls: int = 0
        self.default_audio_trigger_calls: list[str] = []
        self.cancel_calls: list[UUID] = []
        self.bump_calls: list[UUID] = []
        self.clear_queue_calls: int = 0

    def scan_now(self) -> list[Job]:
        return self._scan_jobs

    def trigger_file(
        self,
        file_path: str,
        *,
        extra_languages: Iterable[str] | None = None,
        session: Session | None = None,
        bypass_dedup_window: bool = False,
    ) -> Job | None:
        self.trigger_calls.append(
            (
                file_path,
                frozenset(extra_languages) if extra_languages is not None else frozenset(),
                bypass_dedup_window,
            )
        )
        return self._trigger_job

    def requeue_file(self, file_path: str, *, session: Session | None = None) -> Job | None:
        self.requeue_calls.append(file_path)
        return self._requeue_job

    def requeue_all_failed(self, *, session: Session | None = None) -> BulkRequeueResult:
        self.requeue_all_failed_calls += 1
        return self._requeue_all_failed_result

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

    def bump_job_to_front(self, job_id: UUID) -> bool | None:
        self.bump_calls.append(job_id)
        return self._bump_result

    def clear_queue(self, *, session: Session | None = None) -> ClearQueueResult:
        self.clear_queue_calls += 1
        return self._clear_queue_result


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
    assert fake.trigger_calls == [("/media/movie.mkv", frozenset(), True)]


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
    assert fake.trigger_calls == [("/media/movie.mkv", frozenset({"jpn", "kor"}), True)]


def test_trigger_always_bypasses_the_recently_processed_window(client: TestClient) -> None:
    """COL-170: every explicit trigger now bypasses the window (a behavior change)."""
    fake = _FakeScheduler(trigger_job=_job("/media/movie.mkv"))
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        client.post(
            "/api/jobs/trigger",
            json={"file_path": "/media/movie.mkv"},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    [(_, _extra_languages, bypass_dedup_window)] = fake.trigger_calls
    assert bypass_dedup_window is True


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


# --- POST /api/jobs/requeue (COL-170) -----------------------------------------


def test_requeue_enqueues_a_job_and_returns_it(client: TestClient) -> None:
    fake = _FakeScheduler(requeue_job=_job("/media/movie.mkv"))
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/requeue",
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
    assert fake.requeue_calls == ["/media/movie.mkv"]


def test_requeue_reports_not_enqueued_when_the_file_is_skipped(client: TestClient) -> None:
    fake = _FakeScheduler(requeue_job=None)  # duplicate / unprobeable / nothing to do
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/requeue",
            json={"file_path": "/media/movie.mkv"},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["enqueued"] is False
    assert body["job"] is None


def test_requeue_rejects_unknown_body_fields(client: TestClient) -> None:
    fake = _FakeScheduler(requeue_job=None)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/requeue",
            json={"file_path": "/media/movie.mkv", "bogus": True},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text


def test_requeue_rejects_an_extra_languages_field(client: TestClient) -> None:
    """Unlike ``/api/jobs/trigger``, requeue has no allow-list-bypass option at all."""
    fake = _FakeScheduler(requeue_job=None)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(
            "/api/jobs/requeue",
            json={"file_path": "/media/movie.mkv", "extra_languages": ["jpn"]},
            headers=_auth_headers(client),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text


def test_requeue_is_503_when_no_scheduler_is_wired(client: TestClient) -> None:
    """The default app has no scheduler -> requeue fails loudly, not silently."""
    response = client.post(
        "/api/jobs/requeue",
        json={"file_path": "/media/movie.mkv"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 503


def test_requeue_wires_through_a_real_scheduler(settings: Settings) -> None:
    """End-to-end: a real enable_scheduler app, unprobeable file -> skipped, not an error."""
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/requeue",
            json={"file_path": "/media/does-not-exist.mkv"},
            headers=_auth_headers(client),
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["enqueued"] is False
    assert body["job"] is None


# --- POST /api/jobs/requeue-failed (COL-172) ----------------------------------
#
# Contract-only: request/response shape and the pass-through to
# JobScheduler.requeue_all_failed(), via the same fake-scheduler
# dependency_overrides pattern as every other endpoint above. The real
# window-respecting split logic (JobScheduler._requeue_all_failed_in`) is
# exercised directly, with a real queue/session, in tests/test_jobs_scheduler.py.


def test_requeue_all_failed_reports_a_full_success_split(client: TestClient) -> None:
    """Every currently-failed file was requeued -> an empty ``skipped`` list."""
    jobs = [_job("/media/a.mkv"), _job("/media/b.mkv")]
    fake = _FakeScheduler(
        requeue_all_failed_result=BulkRequeueResult(requeued=jobs, skipped=[])
    )
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/requeue-failed", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert [j["file_path"] for j in body["requeued"]] == ["/media/a.mkv", "/media/b.mkv"]
    assert all(j["status"] == "pending" for j in body["requeued"])
    assert body["skipped"] == []
    assert fake.requeue_all_failed_calls == 1


def test_requeue_all_failed_reports_a_partial_skip_split(client: TestClient) -> None:
    """Some currently-failed files requeued, others skipped -- both surfaced, never silent."""
    fake = _FakeScheduler(
        requeue_all_failed_result=BulkRequeueResult(
            requeued=[_job("/media/a.mkv")], skipped=["/media/b.mkv", "/media/c.mkv"]
        )
    )
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/requeue-failed", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert [j["file_path"] for j in body["requeued"]] == ["/media/a.mkv"]
    assert body["skipped"] == ["/media/b.mkv", "/media/c.mkv"]


def test_requeue_all_failed_reports_an_all_skipped_split_as_a_valid_response(
    client: TestClient,
) -> None:
    """Every currently-failed file was skipped (e.g. all inside the window) -> not an error."""
    fake = _FakeScheduler(
        requeue_all_failed_result=BulkRequeueResult(
            requeued=[], skipped=["/media/a.mkv", "/media/b.mkv"]
        )
    )
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/requeue-failed", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["requeued"] == []
    assert body["skipped"] == ["/media/a.mkv", "/media/b.mkv"]


def test_requeue_all_failed_reports_an_empty_split_when_nothing_is_failed(
    client: TestClient,
) -> None:
    """No currently-failed Job at all -> both lists empty, still a 202."""
    fake = _FakeScheduler()  # default: BulkRequeueResult(requeued=[], skipped=[])
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/requeue-failed", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    assert response.json() == {"requeued": [], "skipped": []}


def test_requeue_all_failed_is_503_when_no_scheduler_is_wired(client: TestClient) -> None:
    """The default app has no scheduler -> the bulk requeue fails loudly, not silently."""
    response = client.post("/api/jobs/requeue-failed", headers=_auth_headers(client))
    assert response.status_code == 503


def test_requeue_all_failed_wires_through_a_real_scheduler(settings: Settings) -> None:
    """End-to-end: a real enable_scheduler app, no failed history -> an empty split."""
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app) as client:
        response = client.post("/api/jobs/requeue-failed", headers=_auth_headers(client))

    assert response.status_code == 202, response.text
    assert response.json() == {"requeued": [], "skipped": []}


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


# --- POST /api/jobs/clear (COL-173) -------------------------------------------
#
# The empty/full-split shape and the id -> scheduler.clear_queue() call are
# HTTP-contract tests via the same fake-scheduler dependency_overrides pattern
# as every other endpoint above. The real batch cancel/history-delete/skip
# logic (JobScheduler._clear_queue_in`) is exercised directly, with a real
# queue/session, in tests/test_jobs_scheduler.py. The final test below drives
# the whole thing end to end through this HTTP endpoint against a real
# JobScheduler + JobQueue (a stub probe/pipeline_runner standing in for the
# ffmpeg/ffprobe boundary, same pattern as test_wanted_pipeline_integration.py)
# to cover the two behaviours this ticket's AC calls out explicitly: a
# simulated race (a snapshotted PENDING job transitions to RUNNING before its
# own cancel lands) and the post-clear Auto-Queue Limit top-up.


def test_clear_queue_reports_a_full_cancel_split_when_nothing_races(client: TestClient) -> None:
    fake = _FakeScheduler(clear_queue_result=ClearQueueResult(cancelled=3, already_running=0))
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/clear", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    assert response.json() == {"cancelled": 3, "already_running": 0}
    assert fake.clear_queue_calls == 1


def test_clear_queue_reports_an_already_running_split_when_some_jobs_raced(
    client: TestClient,
) -> None:
    """A too-late-to-cancel Job is surfaced in ``already_running``, not silently dropped."""
    fake = _FakeScheduler(clear_queue_result=ClearQueueResult(cancelled=2, already_running=1))
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/clear", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    assert response.json() == {"cancelled": 2, "already_running": 1}


def test_clear_queue_reports_a_zero_cancelled_split_for_an_empty_queue(
    client: TestClient,
) -> None:
    """AC: clearing an already-empty queue is not an error -- a valid, zero-cancelled result."""
    fake = _FakeScheduler()  # default: ClearQueueResult(cancelled=0, already_running=0)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/clear", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    assert response.json() == {"cancelled": 0, "already_running": 0}


def test_clear_queue_is_503_when_no_scheduler_is_wired(client: TestClient) -> None:
    """The default app has no scheduler -> clear fails loudly, not silently."""
    response = client.post("/api/jobs/clear", headers=_auth_headers(client))
    assert response.status_code == 503


def test_clear_queue_wires_through_a_real_scheduler(settings: Settings) -> None:
    """End-to-end: a real enable_scheduler app, empty queue -> a valid zero-cancelled result."""
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app) as client:
        response = client.post("/api/jobs/clear", headers=_auth_headers(client))

    assert response.status_code == 202, response.text
    assert response.json() == {"cancelled": 0, "already_running": 0}


def test_clear_queue_reports_a_mid_request_race_and_tops_up_after(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the real HTTP endpoint, against a real JobScheduler + JobQueue.

    Three PENDING Jobs are seeded; one (``b``) is made to race PENDING ->
    RUNNING in between the request snapshotting it and its own individual
    cancel attempt landing (there is no push mechanism to freeze the live
    queue mid-request, only polling -- the queue's worker pool keeps running
    concurrently the whole time). The response must report that split rather
    than silently ignoring it, and -- since cancelling frees a slot exactly
    like any other cancellation -- the Auto-Queue Limit's top-up (COL-171)
    must have run immediately after, pulling a Wanted file back in.
    """
    app = client.app
    assert isinstance(app, FastAPI)
    session_factory = app.state.session_factory

    with session_factory() as session:
        instance = ArrInstance(
            name="inst", type=InstanceType.SONARR, base_url="http://arr.local", api_key="k"
        )
        session.add(instance)
        session.commit()
        session.refresh(instance)
        instance_id = instance.id

    def fake_fetch(instance: ArrInstance, **_: object) -> list[MonitoredFile]:
        return [
            MonitoredFile(instance_id=instance_id, media_title="Show", file_path="/tv/extra.mkv")
        ]

    monkeypatch.setattr(scheduler_module, "fetch_monitored_files", fake_fetch)

    surround_stream = [
        AudioStreamInfo(
            index=0, codec="ac3", channels=6, channel_layout="5.1(side)", language="eng"
        )
    ]

    def stub_probe(path: Path) -> list[AudioStreamInfo]:
        return surround_stream

    queue = JobQueue()  # pipeline never actually runs -- every seeded Job stays PENDING/RUNNING
    scheduler = JobScheduler(queue, session_factory, settings, probe=stub_probe)
    app.state.job_scheduler = scheduler

    job_a = scheduler.trigger_file("/media/a.mkv")
    job_b = scheduler.trigger_file("/media/b.mkv")
    job_c = scheduler.trigger_file("/media/c.mkv")
    assert job_a is not None
    assert job_b is not None
    assert job_c is not None

    original_cancel = queue.cancel

    def racy_cancel(job_id: UUID) -> bool:
        if job_id == job_b.id:
            job_b.status = JobStatus.RUNNING  # simulate a worker claiming it, mid-request
        return original_cancel(job_id)

    monkeypatch.setattr(queue, "cancel", racy_cancel)

    response = client.post("/api/jobs/clear", headers=_auth_headers(client))

    assert response.status_code == 202, response.text
    assert response.json() == {"cancelled": 2, "already_running": 1}

    # a/c: genuinely cancelled -- gone from the live queue.
    assert queue.get_job(job_a.id) is None
    assert queue.get_job(job_c.id) is None
    # b: raced to RUNNING -- left exactly as it was, not cancelled.
    still_there = queue.get_job(job_b.id)
    assert still_there is not None
    assert still_there.status is JobStatus.RUNNING

    # Post-clear top-up (COL-171): the freed slots pulled a Wanted file back in.
    assert any(job.file_path == Path("/tv/extra.mkv") for job in queue.list_jobs())


# --- POST /api/jobs/{job_id}/bump (COL-169) -----------------------------------
#
# These are HTTP-contract tests only -- request/response shape, the id ->
# scheduler.bump_job_to_front(UUID) call, and the None/True/False -> 404/200
# mapping -- via the same fake-scheduler dependency_overrides pattern as
# every other endpoint above, mirroring the DELETE section. The real
# priority-reassignment + "next claimed by a free worker" behaviour is
# exercised end-to-end against a real JobQueue and worker pool in
# tests/test_jobs_queue.py::test_bumped_job_is_claimed_before_earlier_enqueued_pending_jobs,
# and JobScheduler.bump_job_to_front's own None/True/False mapping is
# exercised directly, with a real queue, in tests/test_jobs_scheduler.py.


def test_bump_job_returns_bumped_true_on_success(client: TestClient) -> None:
    job_id = uuid4()
    fake = _FakeScheduler(bump_result=True)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(f"/api/jobs/{job_id}/bump", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {"bumped": True}
    assert fake.bump_calls == [job_id]


def test_bump_job_returns_bumped_false_when_already_claimed(client: TestClient) -> None:
    """A no-longer-PENDING Job is "too late", not an error and not a silent success."""
    job_id = uuid4()
    fake = _FakeScheduler(bump_result=False)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(f"/api/jobs/{job_id}/bump", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {"bumped": False}
    assert fake.bump_calls == [job_id]


def test_bump_job_returns_404_for_a_job_not_in_the_live_queue(client: TestClient) -> None:
    job_id = uuid4()
    fake = _FakeScheduler(bump_result=None)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post(f"/api/jobs/{job_id}/bump", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert fake.bump_calls == [job_id]


def test_bump_job_returns_404_for_a_malformed_job_id_without_calling_the_scheduler(
    client: TestClient,
) -> None:
    fake = _FakeScheduler()
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_job_scheduler] = lambda: fake
    try:
        response = client.post("/api/jobs/not-a-uuid/bump", headers=_auth_headers(client))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert fake.bump_calls == []  # not a UUID at all -- never reaches the scheduler


def test_bump_job_is_503_when_no_scheduler_is_wired(client: TestClient) -> None:
    """The default app has no scheduler -> bump fails loudly, not silently."""
    response = client.post(f"/api/jobs/{uuid4()}/bump", headers=_auth_headers(client))
    assert response.status_code == 503


def test_bump_job_wires_through_a_real_scheduler(settings: Settings) -> None:
    """End-to-end: a real enable_scheduler app, unknown id -> 404 (nothing to act on)."""
    app = create_app(settings=settings, enable_scheduler=True)
    with TestClient(app) as client:
        response = client.post(f"/api/jobs/{uuid4()}/bump", headers=_auth_headers(client))

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


def test_requeue_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    response = client.post("/api/jobs/requeue", json={"file_path": "/media/movie.mkv"})
    assert response.status_code == 401


def test_requeue_all_failed_endpoint_requires_the_api_key(
    client: TestClient, session: Session
) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    response = client.post("/api/jobs/requeue-failed")
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


def test_bump_job_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    response = client.post(f"/api/jobs/{uuid4()}/bump")
    assert response.status_code == 401


def test_clear_queue_endpoint_requires_the_api_key(client: TestClient, session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)
    response = client.post("/api/jobs/clear")
    assert response.status_code == 401
