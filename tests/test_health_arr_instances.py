"""Tests for the no-Arr-instances-configured health check (COL-77).

Configuring an instance goes through :func:`collapsarr.arr.service.create_instance`
(the real Arr Integration write path) with an ``httpx.MockTransport`` standing
in for the connectivity probe -- the same idiom ``tests/test_arr_service.py``
uses -- so this check's "at least one instance" case reflects a genuinely
configured instance, not a bare ORM row.
"""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from collapsarr.arr.models import InstanceType
from collapsarr.arr.service import create_instance
from collapsarr.config import Settings
from collapsarr.health import (
    ARR_INSTANCES_CATEGORY,
    NO_ARR_INSTANCES_CODE,
    SEVERITY_WARNING,
    HealthCheckContext,
    run_arr_instances_check,
)


def _ok_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "4.0.1.929"})

    return httpx.MockTransport(handler)


def test_warns_when_zero_arr_instances_are_configured(
    settings: Settings, session: Session
) -> None:
    context = HealthCheckContext(settings=settings, session=session)

    results = run_arr_instances_check(context)

    assert len(results) == 1
    result = results[0]
    assert result.passing is False
    assert result.code == NO_ARR_INSTANCES_CODE
    assert result.category == ARR_INSTANCES_CATEGORY
    assert result.severity == SEVERITY_WARNING
    assert result.instance_id is None
    assert "no arr" in result.message.lower()


def test_passes_when_at_least_one_arr_instance_is_configured(
    settings: Settings, session: Session
) -> None:
    create_instance(
        session,
        name="Main Sonarr",
        instance_type=InstanceType.SONARR,
        base_url="http://sonarr.local:8989",
        api_key="sonarr-api-key",
        transport=_ok_transport(),
    )
    context = HealthCheckContext(settings=settings, session=session)

    results = run_arr_instances_check(context)

    assert len(results) == 1
    result = results[0]
    assert result.passing is True
    assert result.code == NO_ARR_INSTANCES_CODE
    assert result.category == ARR_INSTANCES_CATEGORY
    assert result.severity == SEVERITY_WARNING
    assert result.instance_id is None
    assert "1 Arr instance" in result.message


def test_configuring_an_instance_clears_a_prior_warning_on_the_next_check(
    settings: Settings, session: Session
) -> None:
    """AC: configuring at least one instance clears the warning on the *next*
    check -- exercised by calling the check twice against the same session,
    creating the instance in between, matching how the scheduler re-runs every
    registered check fresh on each tick (COL-75's ``run_once``)."""
    context = HealthCheckContext(settings=settings, session=session)

    before = run_arr_instances_check(context)
    assert before[0].passing is False

    create_instance(
        session,
        name="Main Radarr",
        instance_type=InstanceType.RADARR,
        base_url="http://radarr.local:7878",
        api_key="radarr-api-key",
        transport=_ok_transport(),
    )

    after = run_arr_instances_check(context)
    assert after[0].passing is True


def test_registered_with_default_health_checks() -> None:
    """AC: the check is registered with the framework's default registry."""
    from collapsarr.health import ARR_INSTANCES_CHECK_NAME, default_health_checks

    names = [check.name for check in default_health_checks()]

    assert ARR_INSTANCES_CHECK_NAME in names
