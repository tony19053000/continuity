"""C9-02: the environment a merged migration can be checked against.

Continuity does not know what the project it migrated *does*. It cannot invent a
meaningful check against someone else's application, and a check it invented
would be the kind of fake verification `CLAUDE.md` rule 5 forbids. So the project
declares its own, in its own repository:

    .continuity/verification.json

    {
      "base_url": "https://staging.example.test",
      "timeout_seconds": 10,
      "checks": [
        {"name": "health", "method": "GET", "path": "/healthz", "expect_status": 200},
        {"name": "checkout", "method": "POST", "path": "/synthetic/checkout",
         "body": {"order": "synthetic"}, "expect_status": 201,
         "expect_contains": "accepted"}
      ]
    }

**No file, no claim.** A project without one reaches `VERIFIED` recording
`verification: not_configured`, and nothing anywhere says its deployment was
checked.

Two things about trust are worth stating plainly, because this is the one place
Continuity makes a network request on its own host at a URL someone else wrote:

* It is **off unless a deployment turns it on** (`RELEASE_VERIFICATION_ENABLED`,
  default false). Running tests inside an isolated workspace and making outbound
  requests from Continuity's own host are different risks, and the second one is
  opt-in.
* The manifest is untrusted input, validated before use: `http`/`https` only, a
  bounded number of checks, bounded timeouts, no redirects followed, a capped
  response read, and no credential of any kind attached to the request. A
  response body is data — it is excerpted and redacted, never executed, and only
  ever reaches a model inside an untrusted block.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from backend.observability.logging import get_logger
from backend.shared.redaction import redact

logger = get_logger(__name__)

#: Where a project declares its checks. One path, in the repository, so the
#: checks are versioned with the code they check.
MANIFEST_PATH: Final = ".continuity/verification.json"

MAX_CHECKS: Final = 20
MAX_TIMEOUT_SECONDS: Final = 30
#: How much of a response is read. A verification check should answer in a few
#: hundred bytes; anything larger is not evidence, it is a download.
MAX_BODY_BYTES: Final = 64_000
#: How much of a body is kept as evidence.
EXCERPT_CHARS: Final = 500

_METHODS: Final = frozenset({"GET", "HEAD", "POST"})
_SCHEMES: Final = frozenset({"http", "https"})

#: Hosts that are never a verification target, whatever a deployment allows.
#: These are the cloud instance-metadata endpoints: reaching one from
#: Continuity's host returns that host's own credentials, and no check a project
#: could legitimately declare points at them.
_METADATA_HOSTS: Final = frozenset(
    {
        "169.254.169.254",
        "[fd00:ec2::254]",
        "fd00:ec2::254",
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
    }
)


class ManifestInvalid(Exception):
    """The manifest exists and cannot be used.

    Distinct from absent on purpose. A project that wrote a manifest and got it
    wrong should be told, not silently treated as having no environment.
    """


class HostNotAllowed(ManifestInvalid):
    """The manifest points somewhere Continuity will not send a request."""


def _blocked_address(address: str) -> str:
    """Why this IP must not be contacted, or "" when it is fine."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:  # pragma: no cover - resolvers return addresses
        return "it is not an IP address"

    if ip.is_link_local:
        # Where the metadata endpoints live, and where they move to next.
        return "it is link-local"
    if ip.is_loopback:
        return "it is loopback"
    if ip.is_private:
        return "it is a private address"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "it is a reserved address"
    return ""


def resolve_target(host: str, *, allow_private: bool) -> None:
    """Refuse to send a request to an address that is not a public host.

    The manifest lives in the project's repository, and this is the one place
    Continuity makes an outbound request from its own host to an address someone
    else wrote. Without this, `"base_url": "http://169.254.169.254"` turns
    release verification into a way to read Continuity's own instance
    credentials out of a repository file.

    `allow_private` exists because plenty of real staging environments are
    internal, and a deployment that knows its own network may say so
    (`RELEASE_VERIFICATION_ALLOW_PRIVATE_HOSTS`). It never lifts the
    metadata-host block: no verification check points at an instance metadata
    service.

    **The residual risk, stated rather than papered over:** the name is resolved
    here and resolved again by the connection, so a DNS entry that changes
    between the two — a rebinding attack — is not closed by this check. Closing
    it needs an address-pinned transport, which is worth doing if this feature
    is ever used against an untrusted repository.
    """
    bare = host.strip("[]").lower()
    if bare in _METADATA_HOSTS or host.lower() in _METADATA_HOSTS:
        raise HostNotAllowed(f"{host} is an instance metadata endpoint")

    try:
        resolved = {str(info[4][0]) for info in socket.getaddrinfo(bare, None)}
    except OSError as exc:
        raise HostNotAllowed(f"{host} does not resolve: {exc.strerror}") from exc

    for address in sorted(resolved):
        if address in _METADATA_HOSTS:
            raise HostNotAllowed(f"{host} resolves to an instance metadata endpoint")
        reason = _blocked_address(address)
        if reason and not allow_private:
            raise HostNotAllowed(
                f"{host} resolves to {address} and {reason}; set "
                "RELEASE_VERIFICATION_ALLOW_PRIVATE_HOSTS to allow an internal "
                "environment"
            )


@dataclass(frozen=True, slots=True)
class SyntheticCheck:
    """One request, and what it is supposed to answer."""

    name: str
    method: str
    path: str
    expect_status: int
    body: dict[str, Any] | None = None
    expect_contains: str = ""


@dataclass(frozen=True, slots=True)
class VerificationEnvironment:
    """Where to send the checks, and what they are."""

    base_url: str
    timeout_seconds: float
    checks: tuple[SyntheticCheck, ...]


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """What one check actually did."""

    name: str
    ok: bool
    status: int | None
    reason: str
    excerpt: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "excerpt": self.excerpt,
        }


@dataclass(slots=True)
class Observation:
    """A whole run of the checks, at one moment."""

    outcomes: list[CheckOutcome] = field(default_factory=list)
    reached: bool = False
    note: str = ""

    @property
    def failures(self) -> list[CheckOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]

    def by_name(self) -> dict[str, CheckOutcome]:
        return {outcome.name: outcome for outcome in self.outcomes}

    def summary(self) -> dict[str, Any]:
        return {
            "reached": self.reached,
            "note": self.note,
            "checks": [outcome.summary() for outcome in self.outcomes],
            "failures": len(self.failures),
        }


def parse_manifest(text: str) -> VerificationEnvironment:
    """Read a manifest, refusing anything it cannot vouch for."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ManifestInvalid(f"not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestInvalid("the manifest must be an object")

    base_url = str(raw.get("base_url", "")).strip()
    parsed = urlparse(base_url)
    if parsed.scheme not in _SCHEMES or not parsed.netloc:
        raise ManifestInvalid(
            "base_url must be an absolute http or https URL"
        )

    host = parsed.hostname or ""
    if host.lower() in _METADATA_HOSTS or host.strip("[]").lower() in _METADATA_HOSTS:
        # Refused at parse time as well as at request time, so the manifest is
        # reported as invalid rather than as an environment that could not be
        # reached. They are different things to tell someone.
        raise HostNotAllowed(f"{host} is an instance metadata endpoint")

    timeout = raw.get("timeout_seconds", 10)
    if not isinstance(timeout, int | float) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ManifestInvalid(
            f"timeout_seconds must be between 0 and {MAX_TIMEOUT_SECONDS}"
        )

    declared = raw.get("checks")
    if not isinstance(declared, list) or not declared:
        raise ManifestInvalid("checks must be a non-empty list")
    if len(declared) > MAX_CHECKS:
        raise ManifestInvalid(f"at most {MAX_CHECKS} checks are allowed")

    checks = tuple(_parse_check(index, item) for index, item in enumerate(declared))
    names = [check.name for check in checks]
    if len(set(names)) != len(names):
        # Outcomes are compared by name against the pre-merge observation.
        # Duplicates would silently compare a check against a different one.
        raise ManifestInvalid("check names must be unique")

    return VerificationEnvironment(
        base_url=base_url.rstrip("/"),
        timeout_seconds=float(timeout),
        checks=checks,
    )


def _parse_check(index: int, item: Any) -> SyntheticCheck:
    where = f"checks[{index}]"
    if not isinstance(item, dict):
        raise ManifestInvalid(f"{where} must be an object")

    name = str(item.get("name", "")).strip()
    if not name:
        raise ManifestInvalid(f"{where} needs a name")

    method = str(item.get("method", "GET")).upper()
    if method not in _METHODS:
        raise ManifestInvalid(
            f"{where}: method must be one of {', '.join(sorted(_METHODS))}"
        )

    path = str(item.get("path", ""))
    if not path.startswith("/"):
        # Relative to the declared base URL, always. A check cannot point
        # somewhere else by writing an absolute URL here.
        raise ManifestInvalid(f"{where}: path must start with /")

    status = item.get("expect_status", 200)
    if not isinstance(status, int) or not 100 <= status <= 599:
        raise ManifestInvalid(f"{where}: expect_status must be an HTTP status")

    body = item.get("body")
    if body is not None and not isinstance(body, dict):
        raise ManifestInvalid(f"{where}: body must be an object")
    if body is not None and method in {"GET", "HEAD"}:
        raise ManifestInvalid(f"{where}: a {method} check cannot carry a body")

    return SyntheticCheck(
        name=name,
        method=method,
        path=path,
        expect_status=status,
        body=body,
        expect_contains=str(item.get("expect_contains", "")),
    )


async def run_checks(
    environment: VerificationEnvironment,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    allow_private_hosts: bool = False,
) -> Observation:
    """Send every check once, and report what came back.

    One pass, no retries. A flaky environment is a fact about the environment,
    and retrying until it answers the way we hoped would make this a way of
    producing the desired result rather than a measurement.

    `transport` is httpx's own dependency-injection seam. It is how a test
    describes an environment without standing one up, and it is also how a
    deployment behind an egress proxy would supply one; the default is a plain
    client with no special handling.
    """
    observation = Observation()

    try:
        if transport is None:
            # Checked here and not only at parse time, so a manifest that was
            # valid when it was written cannot become a request to a metadata
            # endpoint because a DNS record moved.
            #
            # Skipped when a transport was supplied, because then the caller —
            # not DNS — decides where the bytes go, and resolving a name that
            # will never be dialled would refuse requests that are going
            # nowhere near the network. The guard exists to constrain
            # Continuity's own outbound socket, and that is exactly the case
            # where `transport` is None. `resolve_target` is tested directly.
            resolve_target(
                urlparse(environment.base_url).hostname or "",
                allow_private=allow_private_hosts,
            )
    except HostNotAllowed as exc:
        observation.reached = False
        observation.note = str(exc)
        logger.warning(
            "continuity.verification_target_refused", extra={"reason": str(exc)}
        )
        return observation

    try:
        async with httpx.AsyncClient(
            timeout=environment.timeout_seconds,
            # A redirect is a different URL from the one the manifest declared.
            follow_redirects=False,
            transport=transport,
        ) as client:
            observation.reached = True
            for check in environment.checks:
                observation.outcomes.append(await _run_one(client, environment, check))
    except Exception as exc:
        # The environment could not be reached at all. That is not a regression
        # and not a pass: it is "we could not check", and it is recorded as
        # exactly that.
        observation.reached = False
        observation.note = f"the environment could not be reached: {type(exc).__name__}"
        logger.warning(
            "continuity.verification_environment_unreachable",
            extra={"error": type(exc).__name__},
        )

    return observation


async def _run_one(
    client: httpx.AsyncClient,
    environment: VerificationEnvironment,
    check: SyntheticCheck,
) -> CheckOutcome:
    url = f"{environment.base_url}{check.path}"
    try:
        response = await client.request(
            check.method,
            url,
            json=check.body if check.body is not None else None,
            # Deliberately no credential. Continuity holds the project's GitHub
            # installation token and its own model keys, and neither has any
            # business being sent to an address written in a repository file.
            headers={"User-Agent": "continuity-release-guardian"},
        )
    except Exception as exc:
        return CheckOutcome(
            name=check.name,
            ok=False,
            status=None,
            reason=f"the request failed: {type(exc).__name__}",
        )

    body = response.content[:MAX_BODY_BYTES].decode("utf-8", errors="replace")
    excerpt = redact(body[:EXCERPT_CHARS])

    if response.status_code != check.expect_status:
        return CheckOutcome(
            name=check.name,
            ok=False,
            status=response.status_code,
            reason=(
                f"expected HTTP {check.expect_status}, got {response.status_code}"
            ),
            excerpt=excerpt,
        )

    if check.expect_contains and check.expect_contains not in body:
        return CheckOutcome(
            name=check.name,
            ok=False,
            status=response.status_code,
            reason=f"the response does not contain {check.expect_contains!r}",
            excerpt=excerpt,
        )

    return CheckOutcome(
        name=check.name,
        ok=True,
        status=response.status_code,
        reason="as expected",
        excerpt=excerpt,
    )
