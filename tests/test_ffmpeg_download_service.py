"""Tests for the FFmpeg auto-download orchestration (COL-222).

Covers :func:`~collapsarr.ffmpeg_download.service.resolve_platform_arch`'s
platform/arch mapping, and :func:`~collapsarr.ffmpeg_download.service.
download_and_install_ffmpeg`'s full download-verify-extract-persist flow:
zip and tar archive extraction, zip-slip/tar-slip rejection, a missing
``ffmpeg`` member, a network/checksum failure, and an unsupported platform --
all via ``httpx.MockTransport`` (no real network) and a monkeypatched
manifest entry (so the test controls the exact bytes/checksum, rather than
needing them to match the real, committed ``manifest.json`` pin).
"""

from __future__ import annotations

import hashlib
import io
import platform
import tarfile
import zipfile

import httpx
import pytest
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.ffmpeg_download.manifest import ManifestEntry
from collapsarr.ffmpeg_download.service import (
    download_and_install_ffmpeg,
    resolve_platform_arch,
)
from collapsarr.settings.service import get_global_settings

_FFMPEG_BYTES = b"pretend this is a real ffmpeg executable's bytes"


def _zip_archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _tar_xz_archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _entry_for(content: bytes, *, url: str) -> ManifestEntry:
    return ManifestEntry(
        platform="linux",
        arch="amd64",
        version="8.1.2",
        url=url,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _transport(content: bytes, *, status_code: int = 200) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content if status_code < 400 else b"")

    return httpx.MockTransport(handler)


# --- resolve_platform_arch ----------------------------------------------------


def test_resolve_platform_arch_maps_linux_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert resolve_platform_arch() == ("linux", "amd64")


def test_resolve_platform_arch_maps_linux_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert resolve_platform_arch() == ("linux", "arm64")


def test_resolve_platform_arch_maps_macos_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert resolve_platform_arch() == ("macos", "arm64")


def test_resolve_platform_arch_maps_windows_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    assert resolve_platform_arch() == ("windows", "amd64")


def test_resolve_platform_arch_returns_none_for_an_unsupported_os(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert resolve_platform_arch() is None


def test_resolve_platform_arch_returns_none_for_an_unsupported_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "i386")
    assert resolve_platform_arch() is None


# --- download_and_install_ffmpeg: success -------------------------------------


def test_success_extracts_from_zip_and_persists_ffmpeg_path(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip_archive({"ffmpeg-build/bin/ffmpeg": _FFMPEG_BYTES, "ffmpeg-build/README": b"x"})
    entry = _entry_for(archive, url="https://example.invalid/ffmpeg.zip")
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=_transport(archive)
    )

    assert outcome.ok is True
    assert outcome.error is None
    assert outcome.ffmpeg_path is not None
    from pathlib import Path

    written = Path(outcome.ffmpeg_path)
    assert written.exists()
    assert written.read_bytes() == _FFMPEG_BYTES
    assert written.parent.name == "ffmpeg"

    persisted = get_global_settings(session)
    assert persisted.ffmpeg_path == outcome.ffmpeg_path


def test_success_extracts_from_tar_xz(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _tar_xz_archive({"ffmpeg-build/bin/ffmpeg": _FFMPEG_BYTES})
    entry = _entry_for(archive, url="https://example.invalid/ffmpeg.tar.xz")
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=_transport(archive)
    )

    assert outcome.ok is True
    assert outcome.ffmpeg_path is not None
    from pathlib import Path

    assert Path(outcome.ffmpeg_path).read_bytes() == _FFMPEG_BYTES


def test_success_marks_the_binary_executable_on_posix(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    if os.name != "posix":
        pytest.skip("executable-bit check is POSIX-only")

    archive = _zip_archive({"ffmpeg": _FFMPEG_BYTES})
    entry = _entry_for(archive, url="https://example.invalid/ffmpeg.zip")
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=_transport(archive)
    )

    assert outcome.ok is True
    assert outcome.ffmpeg_path is not None
    from pathlib import Path

    mode = Path(outcome.ffmpeg_path).stat().st_mode
    assert mode & stat.S_IXUSR


# --- download_and_install_ffmpeg: failure paths -------------------------------


def test_unsupported_platform_fails_without_touching_ffmpeg_path(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: None
    )

    outcome = download_and_install_ffmpeg(session, data_dir=settings.data_dir)

    assert outcome.ok is False
    assert outcome.ffmpeg_path is None
    assert outcome.error is not None
    assert get_global_settings(session).ffmpeg_path is None


def test_no_pinned_manifest_entry_fails_cleanly(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "linux"/"arm7" is a real, valid resolve_platform_arch shape but is not
    # one of the 5 pairs manifest.json actually pins.
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "arm7")
    )

    outcome = download_and_install_ffmpeg(session, data_dir=settings.data_dir)

    assert outcome.ok is False
    assert outcome.ffmpeg_path is None
    assert "linux/arm7" in (outcome.error or "")
    assert get_global_settings(session).ffmpeg_path is None


def test_checksum_mismatch_fails_and_does_not_persist_a_path(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip_archive({"ffmpeg": _FFMPEG_BYTES})
    # Pin the wrong checksum deliberately.
    entry = ManifestEntry(
        platform="linux",
        arch="amd64",
        version="8.1.2",
        url="https://example.invalid/ffmpeg.zip",
        sha256=hashlib.sha256(b"not the real archive").hexdigest(),
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=_transport(archive)
    )

    assert outcome.ok is False
    assert outcome.ffmpeg_path is None
    assert outcome.error is not None
    assert "sha-256" in outcome.error.lower() or "mismatch" in outcome.error.lower()
    assert get_global_settings(session).ffmpeg_path is None


def test_network_error_fails_cleanly(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = ManifestEntry(
        platform="linux",
        arch="amd64",
        version="8.1.2",
        url="https://example.invalid/ffmpeg.zip",
        sha256="0" * 64,
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=httpx.MockTransport(handler)
    )

    assert outcome.ok is False
    assert outcome.ffmpeg_path is None
    assert get_global_settings(session).ffmpeg_path is None


def test_archive_missing_an_ffmpeg_member_fails_cleanly(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip_archive({"README.txt": b"no ffmpeg here"})
    entry = _entry_for(archive, url="https://example.invalid/ffmpeg.zip")
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=_transport(archive)
    )

    assert outcome.ok is False
    assert outcome.ffmpeg_path is None
    assert outcome.error is not None
    assert "ffmpeg executable" in outcome.error.lower()
    assert get_global_settings(session).ffmpeg_path is None


def test_zip_slip_entry_is_rejected(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip_archive({"../../../../etc/evil": b"pwned", "ffmpeg": _FFMPEG_BYTES})
    entry = _entry_for(archive, url="https://example.invalid/ffmpeg.zip")
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=_transport(archive)
    )

    assert outcome.ok is False
    assert outcome.ffmpeg_path is None
    assert outcome.error is not None
    assert "path-traversal" in outcome.error.lower() or "zip-slip" in outcome.error.lower()
    assert get_global_settings(session).ffmpeg_path is None


def test_tar_slip_entry_is_rejected(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _tar_xz_archive({"../../etc/evil": b"pwned", "ffmpeg": _FFMPEG_BYTES})
    entry = _entry_for(archive, url="https://example.invalid/ffmpeg.tar.xz")
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=_transport(archive)
    )

    assert outcome.ok is False
    assert outcome.ffmpeg_path is None
    assert get_global_settings(session).ffmpeg_path is None


def test_corrupt_archive_fails_cleanly(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    garbage = b"this is not a zip file at all"
    entry = _entry_for(garbage, url="https://example.invalid/ffmpeg.zip")
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.get_manifest_entry", lambda *_a, **_k: entry
    )
    monkeypatch.setattr(
        "collapsarr.ffmpeg_download.service.resolve_platform_arch", lambda: ("linux", "amd64")
    )

    outcome = download_and_install_ffmpeg(
        session, data_dir=settings.data_dir, transport=_transport(garbage)
    )

    assert outcome.ok is False
    assert outcome.ffmpeg_path is None
    assert outcome.error is not None
    assert "not a valid" in outcome.error.lower()
