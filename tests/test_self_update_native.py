"""Tests for the native self-update flow: staged handoff + atomic swap (COL-235).

Exercised through the bare service-layer ``session`` fixture (mirroring
``tests/test_self_update_apply.py``) plus ``tmp_path`` directories standing in
for the real install/staging/backup dirs -- per the AC, no real process is ever
spawned, no real PID is ever waited on, and no real ``os.execv`` runs: every
OS-touching seam (``spawn_handoff``, ``exit_fn``, ``pid_probe``, ``sleep``,
``monotonic``, ``reexec_fn``) is a fake/spy. ``resolve_platform_arch`` is
patched to a fixed ``(linux, amd64)`` so the fixture archive filename/checksum
stay deterministic across the CI matrix.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from collapsarr import __version__
from collapsarr.self_update.models import PHASE_AWAITING_HEALTH, PHASE_IDLE
from collapsarr.self_update.native import (
    FINISH_UPDATE_FLAG,
    NATIVE_BACKUP_DIRNAME,
    NATIVE_STAGING_DIRNAME,
    FinishUpdateArgs,
    _resolve_staged_root,  # noqa: PLC2701 - unit under test
    apply_native_update,
    build_finish_update_argv,
    finish_native_update,
    parse_finish_update_argv,
    restore_from_backup,
    swap_install_dir,
    wait_for_pid_exit,
)
from collapsarr.self_update.service import (
    SelfUpdateAlreadyInProgressError,
    begin_self_update,
    get_self_update_state,
)

_TAG = "v1.2.3"
_VERSION = "1.2.3"
_FILENAME = f"collapsarr-{_VERSION}-linux-amd64.tar.gz"


def _make_release_tar(files: dict[str, bytes]) -> bytes:
    """Build a ``.tar.gz`` whose members are ``files`` (``arcname -> bytes``)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for arcname, data in files.items():
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# A release archive wrapping the install tree in a single top-level dir, exactly
# as release.yml's PyInstaller COLLECT output (name="collapsarr") produces.
_ARCHIVE_BYTES = _make_release_tar(
    {
        "collapsarr/collapsarr": b"#!/new-binary\n",
        "collapsarr/_internal/base.txt": b"new-internal-data",
    }
)
_ARCHIVE_SHA256 = hashlib.sha256(_ARCHIVE_BYTES).hexdigest()
_SHA256SUMS_CONTENT = f"{_ARCHIVE_SHA256}  {_FILENAME}\n"


def _transport(
    *, sha256sums: str = _SHA256SUMS_CONTENT, archive: bytes = _ARCHIVE_BYTES
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/SHA256SUMS"):
            return httpx.Response(200, text=sha256sums)
        if path.endswith(f"/{_FILENAME}"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def _patch_platform(monkeypatch: pytest.MonkeyPatch, resolved: tuple[str, str] | None) -> None:
    monkeypatch.setattr(
        "collapsarr.self_update.native.resolve_platform_arch", lambda: resolved
    )


class _SpawnSpy:
    """Fake ``HandoffSpawner`` -- records args, never launches a process."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[FinishUpdateArgs, Path]] = []
        self._events = events

    def __call__(self, args: FinishUpdateArgs, staged_dir: Path) -> None:
        self.calls.append((args, staged_dir))
        if self._events is not None:
            self._events.append("spawn")


class _ExitSpy:
    """Fake ``ExitFn`` -- records the call and returns (a real exit never returns)."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.calls = 0
        self._events = events

    def __call__(self) -> None:
        self.calls += 1
        if self._events is not None:
            self._events.append("exit")


def _write_install(root: Path, name: str, content: bytes) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    marker = root / name
    marker.write_bytes(content)
    return marker


# --------------------------------------------------------------------------- #
# swap_install_dir -- atomic swap correctness (the load-bearing invariant)
# --------------------------------------------------------------------------- #


def test_swap_install_dir_replaces_live_with_staged_and_keeps_old_as_backup(
    tmp_path: Path,
) -> None:
    live = tmp_path / "install"
    staged = tmp_path / "staged"
    backup = tmp_path / "backup"
    _write_install(live, "marker.txt", b"OLD")
    _write_install(staged, "marker.txt", b"NEW")

    swap_install_dir(live, staged, backup)

    # Postcondition: live is now the new install, old preserved under backup,
    # staged consumed.
    assert (live / "marker.txt").read_bytes() == b"NEW"
    assert (backup / "marker.txt").read_bytes() == b"OLD"
    assert not staged.exists()


def test_swap_install_dir_second_rename_failure_rolls_back_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "install"
    staged = tmp_path / "staged"
    backup = tmp_path / "backup"
    _write_install(live, "marker.txt", b"OLD")
    _write_install(staged, "marker.txt", b"NEW")

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # the staged -> live rename
            raise OSError("simulated cross-device / locked failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    with pytest.raises(OSError, match="simulated"):
        swap_install_dir(live, staged, backup)

    # Rolled back: the old install is right back where it was, never left empty.
    assert (live / "marker.txt").read_bytes() == b"OLD"
    assert not backup.exists()


def test_swap_never_leaves_a_partially_populated_live_dir(tmp_path: Path) -> None:
    """A crash between the two renames leaves exactly one recoverable state."""
    live = tmp_path / "install"
    staged = tmp_path / "staged"
    backup = tmp_path / "backup"
    _write_install(live, "a.txt", b"OLD-A")
    _write_install(live, "b.txt", b"OLD-B")
    _write_install(staged, "a.txt", b"NEW-A")

    # Simulate a hard death immediately after the first rename only.
    os.replace(live, backup)
    assert not live.exists()  # the transient window: live absent, old at backup
    assert (backup / "a.txt").read_bytes() == b"OLD-A"
    assert (backup / "b.txt").read_bytes() == b"OLD-B"

    # Recovery restores the complete old install -- nothing was half-written.
    assert restore_from_backup(live, backup) is True
    assert (live / "a.txt").read_bytes() == b"OLD-A"
    assert (live / "b.txt").read_bytes() == b"OLD-B"
    assert not backup.exists()


def test_restore_from_backup_is_a_noop_when_live_dir_already_exists(tmp_path: Path) -> None:
    live = tmp_path / "install"
    backup = tmp_path / "backup"
    _write_install(live, "marker.txt", b"LIVE")
    _write_install(backup, "marker.txt", b"STALE")

    # live exists -> the swap either never started or fully completed; never
    # move a stale backup over a live install.
    assert restore_from_backup(live, backup) is False
    assert (live / "marker.txt").read_bytes() == b"LIVE"
    assert backup.exists()


def test_swap_never_touches_the_user_data_dir(tmp_path: Path) -> None:
    """AC: database/config in the user-data dir are untouched by the swap."""
    live = tmp_path / "install"
    staged = tmp_path / "staged"
    backup = tmp_path / "backup"
    data_dir = tmp_path / "data"
    _write_install(live, "collapsarr", b"OLD")
    _write_install(staged, "collapsarr", b"NEW")
    db_file = _write_install(data_dir, "collapsarr.db", b"precious-database-bytes")

    swap_install_dir(live, staged, backup)

    assert db_file.read_bytes() == b"precious-database-bytes"
    assert (live / "collapsarr").read_bytes() == b"NEW"


# --------------------------------------------------------------------------- #
# wait_for_pid_exit -- injectable liveness poll
# --------------------------------------------------------------------------- #


def test_wait_for_pid_exit_returns_true_immediately_when_already_gone() -> None:
    slept: list[float] = []
    ok = wait_for_pid_exit(
        1234,
        timeout=5.0,
        is_running=lambda _pid: False,
        sleep=slept.append,
        monotonic=lambda: 0.0,
    )
    assert ok is True
    assert slept == []  # never had to wait


def test_wait_for_pid_exit_returns_true_once_the_pid_disappears() -> None:
    states = iter([True, True, False])
    slept: list[float] = []
    ok = wait_for_pid_exit(
        1234,
        timeout=100.0,
        poll_interval=0.1,
        is_running=lambda _pid: next(states),
        sleep=slept.append,
        monotonic=lambda: 0.0,  # never near the deadline
    )
    assert ok is True
    assert slept == [0.1, 0.1]  # polled twice before it vanished


def test_wait_for_pid_exit_times_out_when_the_pid_never_exits() -> None:
    clock = iter([0.0, 0.5, 1.5])  # deadline check, then past the 1.0s deadline
    ok = wait_for_pid_exit(
        1234,
        timeout=1.0,
        is_running=lambda _pid: True,
        sleep=lambda _s: None,
        monotonic=lambda: next(clock),
    )
    assert ok is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.kill(pid, 0) probe")
def test_default_pid_probe_sees_this_running_process() -> None:
    from collapsarr.self_update.native import _pid_is_running

    assert _pid_is_running(os.getpid()) is True


# --------------------------------------------------------------------------- #
# finish_native_update -- wait, swap, re-exec (handoff side)
# --------------------------------------------------------------------------- #


def _finish_args(tmp_path: Path) -> FinishUpdateArgs:
    return FinishUpdateArgs(
        old_pid=4321,
        live_dir=tmp_path / "install",
        staged_dir=tmp_path / "staged",
        backup_dir=tmp_path / "backup",
    )


def test_finish_native_update_happy_path_waits_swaps_and_reexecs(tmp_path: Path) -> None:
    args = _finish_args(tmp_path)
    _write_install(args.live_dir, "collapsarr", b"OLD")
    _write_install(args.staged_dir, "collapsarr", b"NEW")
    reexec_calls: list[Path] = []

    outcome = finish_native_update(
        args,
        pid_probe=lambda _pid: False,  # old process already gone
        sleep=lambda _s: None,
        monotonic=lambda: 0.0,
        reexec_fn=reexec_calls.append,
    )

    assert outcome.ok is True
    # Real swap ran against the tmp_path dirs.
    assert (args.live_dir / "collapsarr").read_bytes() == b"NEW"
    assert (args.backup_dir / "collapsarr").read_bytes() == b"OLD"
    assert reexec_calls == [args.live_dir]


def test_finish_native_update_never_swaps_while_the_old_pid_is_alive(tmp_path: Path) -> None:
    args = _finish_args(tmp_path)
    _write_install(args.live_dir, "collapsarr", b"OLD")
    _write_install(args.staged_dir, "collapsarr", b"NEW")
    swap_calls = 0
    reexec_calls: list[Path] = []

    def counting_swap(_live: Path, _staged: Path, _backup: Path) -> None:
        nonlocal swap_calls
        swap_calls += 1

    outcome = finish_native_update(
        args,
        wait_timeout=1.0,
        pid_probe=lambda _pid: True,  # old process never exits
        sleep=lambda _s: None,
        monotonic=iter([0.0, 2.0]).__next__,  # straight past the deadline
        swap_fn=counting_swap,
        reexec_fn=reexec_calls.append,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "did not exit" in outcome.error
    # The install directory must be left completely untouched.
    assert swap_calls == 0
    assert reexec_calls == []
    assert (args.live_dir / "collapsarr").read_bytes() == b"OLD"
    assert not args.backup_dir.exists()


def test_finish_native_update_reports_a_swap_failure_without_reexecing(tmp_path: Path) -> None:
    args = _finish_args(tmp_path)
    reexec_calls: list[Path] = []

    def failing_swap(_live: Path, _staged: Path, _backup: Path) -> None:
        raise OSError("swap blew up")

    outcome = finish_native_update(
        args,
        pid_probe=lambda _pid: False,
        sleep=lambda _s: None,
        monotonic=lambda: 0.0,
        swap_fn=failing_swap,
        reexec_fn=reexec_calls.append,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "swap" in outcome.error.lower()
    assert reexec_calls == []


# --------------------------------------------------------------------------- #
# argv contract + staged-root resolution
# --------------------------------------------------------------------------- #


def test_finish_update_argv_round_trips() -> None:
    args = FinishUpdateArgs(
        old_pid=99,
        live_dir=Path("/opt/collapsarr"),
        staged_dir=Path("/opt/.collapsarr-self-update-staging/collapsarr"),
        backup_dir=Path("/opt/.collapsarr-self-update-backup"),
    )
    argv = build_finish_update_argv("/opt/staged/collapsarr", args)
    assert argv[0] == "/opt/staged/collapsarr"
    assert FINISH_UPDATE_FLAG in argv

    parsed = parse_finish_update_argv(argv[1:])
    assert parsed == args


def test_parse_finish_update_argv_returns_none_for_an_ordinary_launch() -> None:
    assert parse_finish_update_argv([]) is None
    assert parse_finish_update_argv(["--host", "0.0.0.0", "--port", "8282"]) is None


def test_resolve_staged_root_descends_into_a_single_top_level_dir(tmp_path: Path) -> None:
    extraction = tmp_path / "extract"
    (extraction / "collapsarr").mkdir(parents=True)
    (extraction / "collapsarr" / "collapsarr").write_bytes(b"binary")
    assert _resolve_staged_root(extraction) == extraction / "collapsarr"


def test_resolve_staged_root_uses_the_extraction_dir_for_a_flat_archive(
    tmp_path: Path,
) -> None:
    extraction = tmp_path / "extract"
    extraction.mkdir()
    (extraction / "collapsarr").write_bytes(b"binary")
    (extraction / "_internal").mkdir()
    assert _resolve_staged_root(extraction) == extraction


# --------------------------------------------------------------------------- #
# apply_native_update -- download, verify, stage, spawn, exit (live-process side)
# --------------------------------------------------------------------------- #


def test_apply_native_update_happy_path(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    install_dir = tmp_path / "install"
    _write_install(install_dir, "collapsarr", b"OLD")
    events: list[str] = []
    spawn = _SpawnSpy(events)
    exit_fn = _ExitSpy(events)

    outcome = apply_native_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        install_dir=install_dir,
        spawn_handoff=spawn,
        exit_fn=exit_fn,
    )

    assert outcome.ok is True

    # AC: verified archive extracted into a staging dir *beside* (not into) the
    # live install dir; the top-level wrapper is resolved as the staged root.
    staging_dir = tmp_path / NATIVE_STAGING_DIRNAME
    staged_root = staging_dir / "collapsarr"
    assert (staged_root / "collapsarr").read_bytes() == b"#!/new-binary\n"
    assert (staged_root / "_internal" / "base.txt").read_bytes() == b"new-internal-data"
    assert (install_dir / "collapsarr").read_bytes() == b"OLD"  # live dir untouched

    # AC: handoff launched, then the old process exits -- in that order.
    assert events == ["spawn", "exit"]
    assert spawn.calls[0][0].old_pid == os.getpid()
    assert spawn.calls[0][0].live_dir == install_dir
    assert spawn.calls[0][0].staged_dir == staged_root
    assert spawn.calls[0][0].backup_dir == tmp_path / NATIVE_BACKUP_DIRNAME
    assert exit_fn.calls == 1

    # Guard handed off across the process boundary, not cleared.
    state = get_self_update_state(session)
    assert state.in_progress is True
    assert state.phase == PHASE_AWAITING_HEALTH
    assert state.previous_version == __version__


def test_apply_native_update_checksum_mismatch_aborts_without_staging(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    install_dir = tmp_path / "install"
    _write_install(install_dir, "collapsarr", b"OLD")
    wrong_sums = f"{'0' * 64}  {_FILENAME}\n"
    spawn = _SpawnSpy()
    exit_fn = _ExitSpy()

    outcome = apply_native_update(
        session,
        target_tag=_TAG,
        transport=_transport(sha256sums=wrong_sums),
        install_dir=install_dir,
        spawn_handoff=spawn,
        exit_fn=exit_fn,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "sha-256" in outcome.error.lower() or "mismatch" in outcome.error.lower()
    # Nothing staged, no handoff, no exit -- and the guard is back to idle.
    assert not (tmp_path / NATIVE_STAGING_DIRNAME).exists()
    assert spawn.calls == []
    assert exit_fn.calls == 0
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_apply_native_update_missing_checksum_entry_aborts(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    install_dir = tmp_path / "install"
    _write_install(install_dir, "collapsarr", b"OLD")
    spawn = _SpawnSpy()
    exit_fn = _ExitSpy()

    outcome = apply_native_update(
        session,
        target_tag=_TAG,
        transport=_transport(sha256sums=""),
        install_dir=install_dir,
        spawn_handoff=spawn,
        exit_fn=exit_fn,
    )

    assert outcome.ok is False
    assert spawn.calls == []
    assert exit_fn.calls == 0
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_apply_native_update_rejects_a_corrupt_archive(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    install_dir = tmp_path / "install"
    _write_install(install_dir, "collapsarr", b"OLD")
    # A checksum that matches the (non-tar) bytes, so download+verify passes but
    # extraction fails -- exercising the staging failure path.
    junk = b"not a real tar archive"
    sums = f"{hashlib.sha256(junk).hexdigest()}  {_FILENAME}\n"
    spawn = _SpawnSpy()
    exit_fn = _ExitSpy()

    outcome = apply_native_update(
        session,
        target_tag=_TAG,
        transport=_transport(sha256sums=sums, archive=junk),
        install_dir=install_dir,
        spawn_handoff=spawn,
        exit_fn=exit_fn,
    )

    assert outcome.ok is False
    assert spawn.calls == []
    assert exit_fn.calls == 0
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_apply_native_update_rejects_a_concurrent_trigger(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    install_dir = tmp_path / "install"
    _write_install(install_dir, "collapsarr", b"OLD")
    begin_self_update(session, previous_version="0.9.0")
    spawn = _SpawnSpy()
    exit_fn = _ExitSpy()

    with pytest.raises(SelfUpdateAlreadyInProgressError):
        apply_native_update(
            session,
            target_tag=_TAG,
            transport=_transport(),
            install_dir=install_dir,
            spawn_handoff=spawn,
            exit_fn=exit_fn,
        )

    assert spawn.calls == []
    assert exit_fn.calls == 0
    state = get_self_update_state(session)
    assert state.previous_version == "0.9.0"  # the first attempt's, untouched


def test_apply_native_update_clears_the_guard_on_an_unexpected_spawn_exception(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    install_dir = tmp_path / "install"
    _write_install(install_dir, "collapsarr", b"OLD")

    def exploding_spawn(_args: FinishUpdateArgs, _staged: Path) -> None:
        raise OSError("could not spawn the handoff process")

    exit_fn = _ExitSpy()
    outcome = apply_native_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        install_dir=install_dir,
        spawn_handoff=exploding_spawn,
        exit_fn=exit_fn,
    )

    assert outcome.ok is False
    assert exit_fn.calls == 0  # never exited, since the handoff never launched
    # The must-fix discipline: the guard is cleared, not left poisoned.
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE
    begin_self_update(session, previous_version="1.0.0")  # proof it recovered


def test_apply_native_update_unsupported_platform_fails_before_taking_the_guard(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_platform(monkeypatch, None)
    spawn = _SpawnSpy()
    exit_fn = _ExitSpy()

    outcome = apply_native_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        install_dir=tmp_path / "install",
        spawn_handoff=spawn,
        exit_fn=exit_fn,
    )

    assert outcome.ok is False
    assert spawn.calls == []
    assert exit_fn.calls == 0
    state = get_self_update_state(session)
    assert state.in_progress is False  # guard never reserved
