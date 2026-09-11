"""C8-05: what AgentCore actually gives this deployment, and what it does not.

`02_ARCHITECTURE.md` §17 lists seven AgentCore services with a stated reason for
each. This module reports, from **live API calls**, which of them this account
can reach and which are provisioned. Nothing here is asserted from
configuration: an ARN nobody created is not a capability, and a dashboard that
does not exist is not observability.

The distinction that matters, and the one the UI renders:

* **reachable** — the control-plane API answered with this account's
  credentials. It means the permission is in place and the service could be
  used.
* **provisioned** — a resource actually exists. Only this counts as an
  integration, and only this may be claimed anywhere.

A service that is reachable but unprovisioned is reported as exactly that. It is
not rounded up to "integrated", because `CLAUDE.md` §3.5 forbids faking
AgentCore integration and a green light for infrastructure nobody built is the
clearest possible way to do it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from backend.observability.logging import get_logger
from backend.shared.config import Settings

logger = get_logger(__name__)

PROBE_TIMEOUT_SECONDS: Final = 15


class ServiceState(StrEnum):
    """Three-valued on purpose.

    Two values would force "reachable but empty" into either "working" or
    "broken", and it is neither.
    """

    PROVISIONED = "provisioned"
    REACHABLE_NOT_PROVISIONED = "reachable_not_provisioned"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ServiceProbe:
    """One AgentCore service, as this account actually finds it."""

    name: str
    state: ServiceState
    #: How many resources of this kind exist. Zero is a fact, not a failure.
    resource_count: int = 0
    #: Why it is unavailable. Never a guess — the API's own error class.
    reason: str | None = None

    @property
    def integrated(self) -> bool:
        """Whether this may be claimed as an integration anywhere."""
        return self.state is ServiceState.PROVISIONED

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "resource_count": self.resource_count,
            "reason": self.reason,
            "integrated": self.integrated,
        }


@dataclass(slots=True)
class AgentCoreStatus:
    """What this deployment can honestly say about AgentCore."""

    configured: bool
    region: str | None = None
    account_id: str | None = None
    probes: list[ServiceProbe] = field(default_factory=list)
    reason: str | None = None

    @property
    def integrated_services(self) -> list[str]:
        """The only names that may appear in a UI, README, or evidence report."""
        return sorted(probe.name for probe in self.probes if probe.integrated)

    @property
    def any_integrated(self) -> bool:
        return bool(self.integrated_services)

    def summary(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "region": self.region,
            "account_id": self.account_id,
            "reason": self.reason,
            "integrated_services": self.integrated_services,
            "probes": [probe.summary() for probe in self.probes],
        }


#: (service name, boto3 client, list call, response key). Each entry is a
#: read-only call: probing must never create a resource, and a probe that
#: provisioned something would make "is it provisioned" unanswerable.
_PROBES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("runtime", "bedrock-agentcore-control", "list_agent_runtimes", "agentRuntimes"),
    (
        "identity",
        "bedrock-agentcore-control",
        "list_workload_identities",
        "workloadIdentities",
    ),
    ("gateway", "bedrock-agentcore-control", "list_gateways", "items"),
    ("memory", "bedrock-agentcore-control", "list_memories", "memories"),
    ("observability", "logs", "describe_log_groups", "logGroups"),
)


async def probe(settings: Settings) -> AgentCoreStatus:
    """Ask AWS what exists. Read-only, bounded, and never inferred.

    Returns an unconfigured status rather than raising when AWS is absent: a
    developer with no AWS account should get an honest "not configured", not a
    stack trace.
    """
    if not settings.AWS_REGION:
        return AgentCoreStatus(
            configured=False, reason="AWS_REGION is not set; AgentCore is not configured."
        )

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover - boto3 is a declared dependency
        return AgentCoreStatus(configured=False, reason="boto3 is not installed.")

    def _run() -> AgentCoreStatus:
        # The configured profile, not the ambient chain. `AWS_PROFILE` names a
        # profile rather than carrying a credential, which is why it is the one
        # AWS value that lives in settings at all — and ignoring it here made
        # every AgentCore probe skip under `scripts/verify.sh` while passing
        # when a developer happened to have the profile exported.
        session = boto3.Session(
            profile_name=settings.AWS_PROFILE or None, region_name=settings.AWS_REGION
        )

        try:
            identity = session.client("sts").get_caller_identity()
        except (BotoCoreError, ClientError) as exc:
            return AgentCoreStatus(
                configured=False,
                region=settings.AWS_REGION,
                reason=f"AWS credentials are not usable: {type(exc).__name__}",
            )

        status = AgentCoreStatus(
            configured=True,
            region=settings.AWS_REGION,
            account_id=str(identity.get("Account")),
        )

        for name, service, call, key in _PROBES:
            status.probes.append(_probe_one(session, name, service, call, key))
        return status

    # boto3 is synchronous; a probe on the event loop would block every other
    # request for as long as AWS takes to answer.
    return await asyncio.to_thread(_run)


def _probe_one(
    session: Any, name: str, service: str, call: str, key: str
) -> ServiceProbe:
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client = session.client(service)
        response = getattr(client, call)()
    except (BotoCoreError, ClientError) as exc:
        # The error class, not a message: an AWS error string can name a role
        # ARN or a request id, and this is rendered in a browser.
        return ServiceProbe(
            name=name, state=ServiceState.UNAVAILABLE, reason=type(exc).__name__
        )
    except Exception as exc:  # pragma: no cover - unknown client shape
        return ServiceProbe(
            name=name, state=ServiceState.UNAVAILABLE, reason=type(exc).__name__
        )

    resources = response.get(key) or []
    count = len(resources)

    return ServiceProbe(
        name=name,
        state=ServiceState.PROVISIONED if count else ServiceState.REACHABLE_NOT_PROVISIONED,
        resource_count=count,
    )


#: Exact setup steps for each unprovisioned service, so the blocker in
#: `STATUS.md` is actionable rather than a shrug. Kept beside the probe that
#: reports the service missing.
SETUP_STEPS: Final[dict[str, tuple[str, ...]]] = {
    "runtime": (
        "Build a container image for the Continuity agent entrypoint.",
        "Push it to an ECR repository in the target account and region.",
        "Create an IAM execution role trusting bedrock-agentcore.amazonaws.com "
        "with permission to pull that image and write CloudWatch logs.",
        "aws bedrock-agentcore-control create-agent-runtime "
        "--agent-runtime-name continuity --role-arn <role> "
        "--agent-runtime-artifact containerConfiguration={containerUri=<image>}",
    ),
    "identity": (
        "aws bedrock-agentcore-control create-workload-identity "
        "--name continuity-provider-tokens",
        "Grant the runtime role bedrock-agentcore:GetResourceApiKey and "
        "GetResourceOauth2Token on that identity.",
        "Move GITHUB_APP_* and provider credentials out of .env into the vault.",
    ),
    "gateway": (
        "aws bedrock-agentcore-control create-gateway --name continuity "
        "--role-arn <role> --protocol-type MCP",
        "Register each Continuity tool as a gateway target.",
        "Only adopt this if it demonstrably enforces something "
        "backend/security/policy.py does not (02_ARCHITECTURE.md §17).",
    ),
    "memory": (
        "aws bedrock-agentcore-control create-memory --name continuity-decisions",
        "Phase 9, and only after approvals are in use — the memory worth keeping "
        "is 'this team rejected customers.write', which does not exist yet.",
    ),
    "observability": (
        "Enable Transaction Search in the CloudWatch console for this account.",
        "Configure an OTEL exporter on the agent runtime to the CloudWatch "
        "endpoint.",
        "Log groups appear once the runtime emits its first span.",
    ),
}


def setup_steps(service: str) -> tuple[str, ...]:
    return SETUP_STEPS.get(service, ())
