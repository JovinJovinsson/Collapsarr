"""Tests for the Self-Update in-progress guard and singleton state (COL-230).

Mirrors ``tests/test_update_check_dismiss.py``'s structure -- a dedicated
guard-behaviour test file, exercised through a bare service-layer ``session``
(no HTTP app), since ``collapsarr.self_update.service`` is pure persistence
with no network dependency.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from collapsarr.self_update.models import (
    PHASE_APPLYING,
    PHASE_DOWNLOADING,
    PHASE_IDLE,
    PHASE_ROLLED_BACK,
    PHASE_VERIFYING,
    SelfUpdateState,
)
from collapsarr.self_update.service import (
    SelfUpdateAlreadyInProgressError,
    begin_self_update,
    clear_self_update,
    get_self_update_state,
    set_self_update_phase,
)

# --------------------------------------------------------------------------- #
# get_self_update_state: get-or-create
# --------------------------------------------------------------------------- #


def test_get_self_update_state_creates_a_default_row_when_absent(session: Session) -> None:
    state = get_self_update_state(session)

    assert isinstance(state, SelfUpdateState)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE
    assert state.previous_version is None


def test_get_self_update_state_returns_the_same_row_on_repeated_calls(session: Session) -> None:
    first = get_self_update_state(session)
    second = get_self_update_state(session)

    assert first.id == second.id == 1


# --------------------------------------------------------------------------- #
# AC: the in-progress guard can be set (begin_self_update)...
# --------------------------------------------------------------------------- #


def test_begin_self_update_sets_the_guard(session: Session) -> None:
    state = begin_self_update(session, previous_version="1.0.0")

    assert state.in_progress is True
    assert state.phase == PHASE_DOWNLOADING
    assert state.previous_version == "1.0.0"


def test_begin_self_update_accepts_an_explicit_starting_phase(session: Session) -> None:
    state = begin_self_update(session, previous_version="1.0.0", phase=PHASE_VERIFYING)

    assert state.phase == PHASE_VERIFYING


def test_begin_self_update_stamps_updated_at(session: Session) -> None:
    state = begin_self_update(session, previous_version="1.0.0")

    assert state.updated_at is not None


# --------------------------------------------------------------------------- #
# AC: ...and attempting to set it while already set is rejected...
# --------------------------------------------------------------------------- #


def test_begin_self_update_rejects_a_second_concurrent_attempt(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0")

    with pytest.raises(SelfUpdateAlreadyInProgressError):
        begin_self_update(session, previous_version="2.0.0")


def test_a_rejected_begin_does_not_mutate_the_row(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0")

    with pytest.raises(SelfUpdateAlreadyInProgressError):
        begin_self_update(session, previous_version="2.0.0", phase=PHASE_APPLYING)

    state = get_self_update_state(session)
    assert state.previous_version == "1.0.0"
    assert state.phase == PHASE_DOWNLOADING


def test_begin_self_update_succeeds_again_after_a_clear(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0")
    clear_self_update(session)

    state = begin_self_update(session, previous_version="1.1.0")

    assert state.in_progress is True
    assert state.previous_version == "1.1.0"


# --------------------------------------------------------------------------- #
# set_self_update_phase: advances phase without touching the guard
# --------------------------------------------------------------------------- #


def test_set_self_update_phase_advances_the_phase(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0")

    state = set_self_update_phase(session, PHASE_VERIFYING)

    assert state.phase == PHASE_VERIFYING
    assert state.in_progress is True  # unaffected


def test_set_self_update_phase_is_callable_regardless_of_the_guard(session: Session) -> None:
    # No begin_self_update call first -- still a valid setter.
    state = set_self_update_phase(session, PHASE_ROLLED_BACK)

    assert state.phase == PHASE_ROLLED_BACK
    assert state.in_progress is False


# --------------------------------------------------------------------------- #
# AC: ...and cleared.
# --------------------------------------------------------------------------- #


def test_clear_self_update_clears_the_guard(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0")

    state = clear_self_update(session)

    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_clear_self_update_accepts_an_explicit_final_phase(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0")

    state = clear_self_update(session, phase=PHASE_ROLLED_BACK)

    assert state.in_progress is False
    assert state.phase == PHASE_ROLLED_BACK


def test_clear_self_update_preserves_previous_version(session: Session) -> None:
    begin_self_update(session, previous_version="1.0.0")

    state = clear_self_update(session, phase=PHASE_ROLLED_BACK)

    assert state.previous_version == "1.0.0"


def test_clear_self_update_is_idempotent_on_an_already_clear_row(session: Session) -> None:
    state = clear_self_update(session)

    assert state.in_progress is False
    assert state.phase == PHASE_IDLE
