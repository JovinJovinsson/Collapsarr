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

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.downmix.targets import DownmixSettings
from collapsarr.jobs.models import JobHistory
from collapsarr.jobs.queue import Job, JobStatus
from collapsarr.jobs.routes import get_job_scheduler
from collapsarr.main import create_app
from collapsarr.settings.service import get_global_settings, update_global_settings


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
) -> None:
    session.add(JobHistory(job_id=job_id, file_path=file_path, status=status))
    session.commit()


class _FakeScheduler:
    """Stand-in for :class:`JobScheduler` recording calls and returning fixed jobs."""

    def __init__(
        self,
        *,
        scan_jobs: list[Job] | None = None,
        trigger_job: Job | None = None,
    ) -> None:
        self._scan_jobs = scan_jobs or []
        self._trigger_job = trigger_job
        self.trigger_calls: list[tuple[str, frozenset[str]]] = []

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


def _job(file_path: str) -> Job:
    return Job(file_path=Path(file_path), settings=DownmixSettings())


# --- GET /api/jobs/history: shape & filters ----------------------------------


def test_history_is_empty_when_nothing_recorded(client: TestClient) -> None:
    response = client.get("/api/jobs/history", headers=_auth_headers(client))
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_history_lists_rows_with_full_shape(client: TestClient, session: Session) -> None:
    _seed_history(
        session, job_id="job-1", file_path="/media/a.mkv", status=JobStatus.SUCCEEDED
    )

    response = client.get("/api/jobs/history", headers=_auth_headers(client))

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["job_id"] == "job-1"
    assert row["file_path"] == "/media/a.mkv"
    assert row["status"] == "succeeded"
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


def test_history_combines_file_and_status_filters(
    client: TestClient, session: Session
) -> None:
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
