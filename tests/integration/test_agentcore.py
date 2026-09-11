"""C8-05: what AgentCore this account actually has.

Every assertion below comes from a live AWS call. There is no fixture here and
no mock: the ticket's acceptance is that each integrated feature is demonstrated
live, and a mocked AgentCore probe would demonstrate nothing at all.

Skips with an explicit named reason when AWS is unconfigured — and a skip is
never a pass.
"""

from __future__ import annotations

import pytest

from backend.observability.agentcore import (
    SETUP_STEPS,
    AgentCoreStatus,
    ServiceState,
    probe,
    setup_steps,
)
from backend.shared.config import Settings

pytestmark = pytest.mark.requires_aws


async def _probe_or_skip() -> AgentCoreStatus:
    settings = Settings()
    status = await probe(settings)
    if not status.configured:
        pytest.skip(
            f"SKIPPED, NOT PASSED — AWS is not configured: {status.reason}. "
            "AgentCore availability is unverified until this runs."
        )
    return status


async def test_the_probe_reaches_a_real_aws_account() -> None:
    """The baseline: these credentials work and name an account."""
    status = await _probe_or_skip()

    assert status.account_id and status.account_id.isdigit()
    assert status.region


async def test_every_agentcore_service_is_probed() -> None:
    """All five from `02_ARCHITECTURE.md` §17 that Phase 8 covers."""
    status = await _probe_or_skip()

    assert {p.name for p in status.probes} == {
        "runtime",
        "identity",
        "gateway",
        "memory",
        "observability",
    }


async def test_a_service_is_only_integrated_when_something_exists() -> None:
    """C8-05's honesty requirement, as a property of the data.

    A reachable control-plane API is a permission, not an integration. Rounding
    "the API answered" up to "AgentCore is integrated" is exactly the fake
    infrastructure claim `CLAUDE.md` §3.5 forbids.
    """
    status = await _probe_or_skip()

    for service in status.probes:
        if service.state is ServiceState.REACHABLE_NOT_PROVISIONED:
            assert service.resource_count == 0
            assert not service.integrated
        elif service.state is ServiceState.PROVISIONED:
            assert service.resource_count > 0
            assert service.integrated
        else:
            assert not service.integrated
            assert service.reason


async def test_nothing_unprovisioned_appears_in_integrated_services() -> None:
    """The list the UI and the evidence report are allowed to read."""
    status = await _probe_or_skip()

    provisioned = {p.name for p in status.probes if p.resource_count > 0}

    assert set(status.integrated_services) == provisioned


async def test_the_probe_creates_nothing() -> None:
    """Probing must be read-only.

    A probe that provisioned a resource would make "is it provisioned" a
    question about itself, and would bill the account for being asked.
    """
    first = await _probe_or_skip()
    second = await probe(Settings())

    assert [p.resource_count for p in first.probes] == [
        p.resource_count for p in second.probes
    ]


async def test_an_unprovisioned_service_carries_actionable_setup_steps() -> None:
    """A blocker nobody can act on is a shrug in a table.

    `STATUS.md` records what is missing; these are the commands that would fix
    it, kept beside the probe that reports it missing so they cannot drift.
    """
    status = await _probe_or_skip()

    for service in status.probes:
        if service.integrated:
            continue
        steps = setup_steps(service.name)
        assert steps, f"{service.name} is unprovisioned with no documented setup"
        assert any("aws " in step or "Build" in step or "Enable" in step for step in steps)


def test_every_probed_service_has_setup_steps() -> None:
    """Checked without AWS, so a service added later fails here immediately."""
    from backend.observability.agentcore import _PROBES

    assert {name for name, *_ in _PROBES} == set(SETUP_STEPS)


async def test_an_unconfigured_deployment_says_so_rather_than_failing() -> None:
    """A developer with no AWS account gets an honest answer, not a traceback."""
    status = await probe(Settings(AWS_REGION="", _env_file=None))

    assert not status.configured
    assert status.reason
    assert status.integrated_services == []
