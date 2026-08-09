"""Contract tests for the tail-read log endpoint (COL-131).

Covers ``GET /api/system/logs``: the auth gate, the default ~200-line tail
window (newest last), the ``offset`` parameter paging further back, the
``level`` parameter's server-side minimum-severity filtering (including a
multi-line record's continuation lines staying with their header), and the
rotation race the ticket AC calls out explicitly -- reading the file fresh per
request and tolerating a transient missing file.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from collapsarr.config import Settings
from collapsarr.logging_setup import current_log_path, logs_dir
from collapsarr.settings.service import get_global_settings


def _auth_headers(client: TestClient) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    with app.state.session_factory() as session:
        return {"X-Api-Key": get_global_settings(session).api_key}


def _write_log_lines(settings: Settings, lines: list[str]) -> Path:
    path = current_log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _line(level: str, index: int) -> str:
    return f"2026-08-09 12:00:{index % 60:02d},000 {level} collapsarr.some.module line {index}"


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #
def test_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/system/logs").status_code == 401


# --------------------------------------------------------------------------- #
# Default window: most recent ~200 lines, newest last
# --------------------------------------------------------------------------- #
def test_returns_the_most_recent_200_lines_by_default(
    client: TestClient, settings: Settings
) -> None:
    lines = [_line("INFO", i) for i in range(250)]
    _write_log_lines(settings, lines)
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 200
    # Newest last: the tail of the 250-line file is lines 50..249 (0-indexed).
    assert body["entries"][0]["text"] == lines[50]
    assert body["entries"][-1]["text"] == lines[249]
    assert body["next_offset"] == 200


def test_short_file_returns_every_line_with_no_next_offset(
    client: TestClient, settings: Settings
) -> None:
    lines = [_line("INFO", i) for i in range(5)]
    _write_log_lines(settings, lines)
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", headers=headers)

    body = response.json()
    assert [entry["text"] for entry in body["entries"]] == lines
    assert body["next_offset"] is None


def test_missing_log_file_returns_an_empty_window_rather_than_erroring(
    client: TestClient, settings: Settings
) -> None:
    # The app's startup lifespan already created the log file via
    # configure_logging(); delete it to simulate an edge case where it's
    # briefly absent (e.g. mid-rotation), which _read_log_lines must tolerate.
    current_log_path(settings).unlink()
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"entries": [], "next_offset": None}


# --------------------------------------------------------------------------- #
# offset pages further back
# --------------------------------------------------------------------------- #
def test_offset_pages_further_back_through_the_file(client: TestClient, settings: Settings) -> None:
    lines = [_line("INFO", i) for i in range(250)]
    _write_log_lines(settings, lines)
    headers = _auth_headers(client)

    first = client.get("/api/system/logs", headers=headers).json()
    older = client.get(
        "/api/system/logs", params={"offset": first["next_offset"]}, headers=headers
    ).json()

    assert [entry["text"] for entry in older["entries"]] == lines[:50]
    assert older["next_offset"] is None


# --------------------------------------------------------------------------- #
# level applies minimum-severity filtering, scanned server-side
# --------------------------------------------------------------------------- #
def test_level_filter_keeps_only_matching_and_higher_severity(
    client: TestClient, settings: Settings
) -> None:
    lines = [
        _line("DEBUG", 0),
        _line("INFO", 1),
        _line("WARNING", 2),
        _line("ERROR", 3),
    ]
    _write_log_lines(settings, lines)
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", params={"level": "WARNING"}, headers=headers)

    body = response.json()
    assert [entry["text"] for entry in body["entries"]] == [lines[2], lines[3]]
    assert [entry["level"] for entry in body["entries"]] == ["WARNING", "ERROR"]


def test_level_filter_is_applied_before_pagination_not_after(
    client: TestClient, settings: Settings
) -> None:
    """The AC's "scanned server-side, not post-filtered" requirement: with 300
    DEBUG lines interleaved with 10 WARNING lines, a ``level=WARNING`` request
    must still surface all 10 WARNING lines rather than truncating to a
    200-line raw window and only then filtering (which would lose some)."""
    lines = [_line("DEBUG", i) for i in range(300)]
    for i in range(0, 300, 30):
        lines[i] = _line("WARNING", i)
    _write_log_lines(settings, lines)
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", params={"level": "WARNING"}, headers=headers)

    body = response.json()
    assert len(body["entries"]) == 10
    assert all(entry["level"] == "WARNING" for entry in body["entries"])
    assert body["next_offset"] is None


def test_multiline_record_continuation_lines_inherit_their_headers_level(
    client: TestClient, settings: Settings
) -> None:
    lines = [
        _line("INFO", 0),
        _line("ERROR", 1),
        "Traceback (most recent call last):",
        "  File \"module.py\", line 1, in <module>",
        "ValueError: boom",
    ]
    _write_log_lines(settings, lines)
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", params={"level": "WARNING"}, headers=headers)

    body = response.json()
    # The ERROR header plus its two traceback continuation lines -- not the
    # leading INFO line.
    assert [entry["text"] for entry in body["entries"]] == lines[1:]
    assert [entry["level"] for entry in body["entries"]] == ["ERROR", "ERROR", "ERROR", "ERROR"]


def test_negative_offset_is_rejected(client: TestClient) -> None:
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", params={"offset": -1}, headers=headers)

    assert response.status_code == 422


def test_unknown_level_is_rejected(client: TestClient) -> None:
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", params={"level": "TRACE"}, headers=headers)

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Reads the *current* file location COL-128's logging setup writes to
# --------------------------------------------------------------------------- #
def test_reads_from_the_same_path_configure_logging_writes_to(
    client: TestClient, settings: Settings
) -> None:
    assert current_log_path(settings) == logs_dir(settings) / "collapsarr.log"
    # The app's own startup lifespan calls configure_logging(), so the file
    # already exists and is readable via the endpoint with no writes of our own.
    headers = _auth_headers(client)

    response = client.get("/api/system/logs", headers=headers)

    assert response.status_code == 200
