"""Tests for the per-instance Arr-unreachable connectivity check (COL-78).

Every instance is configured via :func:`collapsarr.arr.service.create_instance`
(the real Arr Integration write path, same idiom as
``tests/test_health_arr_instances.py``) with an *always-ok* ``httpx.MockTransport``
so each instance's cached ``status`` column starts as ``ok`` -- then the check
itself is run with a *different* transport that fails for specific hosts. This
proves the check re-probes live via
:func:`collapsarr.arr.client.check_connectivity` rather than reading the stale
cached ``status``/``status_error`` columns, per this ticket's acceptance
criteria.
"""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from collapsarr.arr.models import ArrInstance, ConnectivityStatus, InstanceType
from collapsarr.arr.service import create_instance
from collapsarr.config import Settings
from collapsarr.health import (
    ARR_CONNECTIVITY_CATEGORY,
    ARR_CONNECTIVITY_CHECK_NAME,
    ARR_UNREACHABLE_CODE,
    SEVERITY_ERROR,
    HealthCheckContext,
    make_arr_connectivity_check_run,
)


def _always_ok_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "4.0.1.929"})

    return httpx.MockTransport(handler)


def _transport_failing_hosts(*unreachable_hosts: str) -> httpx.MockTransport:
    """A transport where any request to ``unreachable_hosts`` fails, others succeed."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in unreachable_hosts:
            raise httpx.ConnectError("Connection refused", request=request)
        return httpx.Response(200, json={"version": "4.0.1.929"})

    return httpx.MockTransport(handler)


def _make_instance(
    session: Session,
    *,
    name: str,
    host: str,
    port: int,
    instance_type: InstanceType,
) -> ArrInstance:
    return create_instance(
        session,
        name=name,
        instance_type=instance_type,
        base_url=f"http://{host}:{port}",
        api_key=f"{name}-api-key",
        transport=_always_ok_transport(),
    )


def _context(session: Session) -> HealthCheckContext:
    return HealthCheckContext(settings=Settings(), session=session)


# ---------------------------------------------------------------------------
# All reachable.
# ---------------------------------------------------------------------------


def test_all_instances_reachable_reports_all_passing(session: Session) -> None:
    sonarr = _make_instance(
        session, name="Sonarr", host="sonarr.local", port=8989, instance_type=InstanceType.SONARR
    )
    radarr = _make_instance(
        session, name="Radarr", host="radarr.local", port=7878, instance_type=InstanceType.RADARR
    )

    run = make_arr_connectivity_check_run(_always_ok_transport())
    results = list(run(_context(session)))

    assert len(results) == 2
    by_instance = {r.instance_id: r for r in results}
    for instance in (sonarr, radarr):
        result = by_instance[instance.id]
        assert result.passing is True
        assert result.code == ARR_UNREACHABLE_CODE
        assert result.category == ARR_CONNECTIVITY_CATEGORY
        assert result.severity == SEVERITY_ERROR
        assert instance.name in result.message


# ---------------------------------------------------------------------------
# One unreachable -- and proof the check re-probes live, ignoring the cached
# (still-`ok`) status column.
# ---------------------------------------------------------------------------


def test_one_unreachable_instance_is_reported_independently(session: Session) -> None:
    sonarr = _make_instance(
        session, name="Sonarr", host="sonarr.local", port=8989, instance_type=InstanceType.SONARR
    )
    radarr = _make_instance(
        session, name="Radarr", host="radarr.local", port=7878, instance_type=InstanceType.RADARR
    )
    # Both instances cached `ok` from creation -- prove the check doesn't read that.
    assert sonarr.status == ConnectivityStatus.OK
    assert radarr.status == ConnectivityStatus.OK

    run = make_arr_connectivity_check_run(_transport_failing_hosts("radarr.local"))
    results = list(run(_context(session)))

    by_instance = {r.instance_id: r for r in results}
    assert by_instance[sonarr.id].passing is True
    assert by_instance[radarr.id].passing is False
    assert by_instance[radarr.id].code == ARR_UNREACHABLE_CODE
    assert "Radarr" in by_instance[radarr.id].message
    # Sonarr's own result is untouched by Radarr's failure.
    assert by_instance[sonarr.id].instance_id == sonarr.id


# ---------------------------------------------------------------------------
# Multiple unreachable -- each tracked as its own distinct failure.
# ---------------------------------------------------------------------------


def test_multiple_unreachable_instances_are_each_tracked_distinctly(session: Session) -> None:
    one = _make_instance(
        session, name="Sonarr-1", host="sonarr1.local", port=8989, instance_type=InstanceType.SONARR
    )
    two = _make_instance(
        session, name="Sonarr-2", host="sonarr2.local", port=8990, instance_type=InstanceType.SONARR
    )
    three = _make_instance(
        session, name="Radarr-1", host="radarr1.local", port=7878, instance_type=InstanceType.RADARR
    )

    run = make_arr_connectivity_check_run(
        _transport_failing_hosts("sonarr1.local", "sonarr2.local")
    )
    results = list(run(_context(session)))

    by_instance = {r.instance_id: r for r in results}
    assert by_instance[one.id].passing is False
    assert by_instance[two.id].passing is False
    assert by_instance[three.id].passing is True
    # Every failing result shares the same Check Code (the check *type*) but a
    # distinct instance_id (the Check Key disambiguator) and its own message.
    assert by_instance[one.id].code == ARR_UNREACHABLE_CODE
    assert by_instance[two.id].code == ARR_UNREACHABLE_CODE
    assert by_instance[one.id].instance_id != by_instance[two.id].instance_id
    assert "Sonarr-1" in by_instance[one.id].message
    assert "Sonarr-2" in by_instance[two.id].message


# ---------------------------------------------------------------------------
# A previously-unreachable instance recovering.
# ---------------------------------------------------------------------------


def test_previously_unreachable_instance_recovers_on_next_run(session: Session) -> None:
    instance = _make_instance(
        session, name="Sonarr", host="sonarr.local", port=8989, instance_type=InstanceType.SONARR
    )

    down_run = make_arr_connectivity_check_run(_transport_failing_hosts("sonarr.local"))
    before = list(down_run(_context(session)))
    assert len(before) == 1
    assert before[0].passing is False
    assert before[0].instance_id == instance.id

    up_run = make_arr_connectivity_check_run(_always_ok_transport())
    after = list(up_run(_context(session)))
    assert len(after) == 1
    assert after[0].passing is True
    assert after[0].instance_id == instance.id
    assert after[0].code == ARR_UNREACHABLE_CODE


def test_recovering_one_instance_does_not_affect_another(session: Session) -> None:
    """AC: recovering one instance doesn't clear/affect another's state."""
    flaky = _make_instance(
        session, name="Flaky", host="flaky.local", port=8989, instance_type=InstanceType.SONARR
    )
    steady_down = _make_instance(
        session,
        name="SteadyDown",
        host="steady.local",
        port=7878,
        instance_type=InstanceType.RADARR,
    )

    both_down = make_arr_connectivity_check_run(
        _transport_failing_hosts("flaky.local", "steady.local")
    )
    before = {r.instance_id: r for r in both_down(_context(session))}
    assert before[flaky.id].passing is False
    assert before[steady_down.id].passing is False

    only_steady_down = make_arr_connectivity_check_run(
        _transport_failing_hosts("steady.local")
    )
    after = {r.instance_id: r for r in only_steady_down(_context(session))}
    assert after[flaky.id].passing is True
    assert after[steady_down.id].passing is False


# ---------------------------------------------------------------------------
# No configured instances -> no results (nothing to probe).
# ---------------------------------------------------------------------------


def test_no_configured_instances_reports_no_results(session: Session) -> None:
    run = make_arr_connectivity_check_run(_always_ok_transport())

    results = list(run(_context(session)))

    assert results == []


# ---------------------------------------------------------------------------
# Registration with the framework's default registry.
# ---------------------------------------------------------------------------


def test_registered_with_default_health_checks() -> None:
    from collapsarr.health import default_health_checks

    names = [check.name for check in default_health_checks()]

    assert ARR_CONNECTIVITY_CHECK_NAME in names


def test_default_health_checks_forwards_arr_transport(session: Session) -> None:
    """AC: unit tests must use a mocked HTTP transport, never a real network call --
    exercised here via the registry's ``arr_transport`` injection seam."""
    from collapsarr.health import default_health_checks

    _make_instance(
        session, name="Sonarr", host="sonarr.local", port=8989, instance_type=InstanceType.SONARR
    )

    checks = default_health_checks(arr_transport=_always_ok_transport())
    arr_check = next(c for c in checks if c.name == ARR_CONNECTIVITY_CHECK_NAME)

    results = list(arr_check.run(_context(session)))

    assert len(results) == 1
    assert results[0].passing is True
