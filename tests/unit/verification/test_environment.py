"""C9-02: the manifest a project declares, and what running it actually does.

The manifest is repository content — the user's own, but still input from
outside Continuity — so every rejection has a test, and so does the shape of the
request Continuity ends up sending.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.verification.environment import (
    MAX_CHECKS,
    HostNotAllowed,
    ManifestInvalid,
    parse_manifest,
    resolve_target,
    run_checks,
)

VALID = {
    "base_url": "https://staging.example.test/",
    "timeout_seconds": 5,
    "checks": [
        {"name": "health", "method": "GET", "path": "/healthz", "expect_status": 200},
        {
            "name": "checkout",
            "method": "POST",
            "path": "/synthetic/checkout",
            "body": {"order": "synthetic"},
            "expect_status": 201,
            "expect_contains": "accepted",
        },
    ],
}


def _manifest(**overrides: object) -> str:
    return json.dumps({**VALID, **overrides})


# --- Parsing --------------------------------------------------------------


def test_a_valid_manifest_parses() -> None:
    environment = parse_manifest(_manifest())

    assert environment.base_url == "https://staging.example.test"
    assert environment.timeout_seconds == 5
    assert [check.name for check in environment.checks] == ["health", "checkout"]
    assert environment.checks[1].body == {"order": "synthetic"}


REJECTED = [
    pytest.param("not json at all", "not valid JSON", id="not json"),
    pytest.param("[]", "must be an object", id="not an object"),
    pytest.param(
        _manifest(base_url="file:///etc/passwd"), "http or https", id="file scheme"
    ),
    pytest.param(
        _manifest(base_url="/relative/path"), "http or https", id="not absolute"
    ),
    pytest.param(_manifest(timeout_seconds=0), "timeout_seconds", id="zero timeout"),
    pytest.param(_manifest(timeout_seconds=600), "timeout_seconds", id="long timeout"),
    pytest.param(_manifest(checks=[]), "non-empty", id="no checks"),
    pytest.param(
        _manifest(
            checks=[
                {"name": f"c{index}", "path": "/x"} for index in range(MAX_CHECKS + 1)
            ]
        ),
        f"at most {MAX_CHECKS}",
        id="too many checks",
    ),
    pytest.param(
        _manifest(checks=[{"name": "", "path": "/x"}]), "needs a name", id="unnamed"
    ),
    pytest.param(
        _manifest(checks=[{"name": "a", "method": "DELETE", "path": "/x"}]),
        "method must be one of",
        id="destructive method",
    ),
    pytest.param(
        _manifest(checks=[{"name": "a", "path": "https://elsewhere.test/x"}]),
        "must start with /",
        id="absolute path escaping the base url",
    ),
    pytest.param(
        _manifest(checks=[{"name": "a", "path": "/x", "expect_status": 999}]),
        "expect_status",
        id="not a status",
    ),
    pytest.param(
        _manifest(checks=[{"name": "a", "path": "/x", "body": "oops"}]),
        "body must be an object",
        id="body is not an object",
    ),
    pytest.param(
        _manifest(checks=[{"name": "a", "method": "GET", "path": "/x", "body": {}}]),
        "cannot carry a body",
        id="GET with a body",
    ),
    pytest.param(
        _manifest(
            checks=[{"name": "same", "path": "/a"}, {"name": "same", "path": "/b"}]
        ),
        "unique",
        id="duplicate names",
    ),
]


@pytest.mark.parametrize(("text", "message"), REJECTED)
def test_an_unusable_manifest_is_refused_with_a_reason(
    text: str, message: str
) -> None:
    with pytest.raises(ManifestInvalid, match=message):
        parse_manifest(text)


def test_a_delete_check_cannot_be_declared() -> None:
    """Stated on its own because it is a property, not a parsing detail.

    A verification check is a *synthetic* request against a live environment.
    Letting a manifest declare `DELETE /orders/1` would make "verification"
    into a way to destroy the thing being verified.
    """
    for method in ("DELETE", "PUT", "PATCH"):
        with pytest.raises(ManifestInvalid):
            parse_manifest(_manifest(checks=[{"name": "a", "method": method, "path": "/x"}]))


# --- Running --------------------------------------------------------------


def _transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


async def test_checks_that_answer_as_declared_pass() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, text="ok")
        return httpx.Response(201, text="accepted")

    observation = await run_checks(parse_manifest(_manifest()), transport=_transport(handler))

    assert observation.reached
    assert observation.failures == []
    assert [outcome.status for outcome in observation.outcomes] == [200, 201]


async def test_a_wrong_status_fails_the_check() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    observation = await run_checks(parse_manifest(_manifest()), transport=_transport(handler))

    assert len(observation.failures) == 2
    assert "expected HTTP 200, got 500" in observation.outcomes[0].reason


async def test_a_missing_expected_string_fails_the_check() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200 if request.url.path == "/healthz" else 201, text="something else"
        )

    observation = await run_checks(parse_manifest(_manifest()), transport=_transport(handler))

    (failure,) = observation.failures
    assert failure.name == "checkout"
    assert "does not contain" in failure.reason


async def test_no_credential_is_ever_sent() -> None:
    """The manifest's URL is written in a repository. Continuity holds a GitHub
    installation token and model keys, and neither goes there."""
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, text="accepted")

    await run_checks(parse_manifest(_manifest()), transport=_transport(handler))

    for headers in seen:
        assert "authorization" not in headers
        assert "cookie" not in headers
        assert "x-api-key" not in headers


async def test_the_request_goes_only_where_the_manifest_said() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="accepted")

    await run_checks(parse_manifest(_manifest()), transport=_transport(handler))

    assert seen == [
        "https://staging.example.test/healthz",
        "https://staging.example.test/synthetic/checkout",
    ]


async def test_a_redirect_is_not_followed() -> None:
    """A redirect is a different URL from the one that was declared."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://elsewhere.test/"})

    observation = await run_checks(parse_manifest(_manifest()), transport=_transport(handler))

    assert len(observation.failures) == 2
    assert all(outcome.status == 302 for outcome in observation.outcomes)


async def test_an_unreachable_environment_is_not_a_pass_and_not_a_regression() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    observation = await run_checks(parse_manifest(_manifest()), transport=_transport(handler))

    assert observation.reached is True, "the client opened; the requests failed"
    assert len(observation.failures) == 2
    assert "the request failed" in observation.outcomes[0].reason


async def test_a_response_body_is_bounded_and_filtered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200 if request.url.path == "/healthz" else 201,
            text="accepted ghp_" + "a" * 40 + "x" * 5000,
        )

    observation = await run_checks(parse_manifest(_manifest()), transport=_transport(handler))

    for outcome in observation.outcomes:
        assert len(outcome.excerpt) <= 500
        assert "ghp_a" not in outcome.excerpt


# --- Where a request may go -----------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254",
        "http://metadata.google.internal",
        "http://metadata",
    ],
)
def test_a_metadata_endpoint_is_refused_before_anything_is_sent(base_url: str) -> None:
    """The attack this guard exists for.

    A manifest lives in the project's repository. Without this, `base_url:
    http://169.254.169.254` plus `path: /latest/meta-data/iam/...` turns release
    verification into a way to read Continuity's own instance credentials out of
    a file someone committed — and the response would be excerpted into the
    evidence report.
    """
    with pytest.raises(HostNotAllowed, match="metadata"):
        parse_manifest(_manifest(base_url=base_url))


@pytest.mark.parametrize(
    ("host", "because"),
    [
        ("127.0.0.1", "loopback"),
        ("10.1.2.3", "private"),
        ("192.168.1.10", "private"),
        ("172.16.0.5", "private"),
        ("169.254.1.1", "link-local"),
        ("::1", "loopback"),
    ],
)
def test_an_internal_address_is_refused_unless_a_deployment_allows_it(
    host: str, because: str
) -> None:
    with pytest.raises(HostNotAllowed, match=because):
        resolve_target(host, allow_private=False)

    # A deployment that knows its own network may say so...
    resolve_target(host, allow_private=True)


def test_allowing_private_hosts_never_allows_a_metadata_endpoint() -> None:
    """...and saying so does not open the one address that is never legitimate."""
    with pytest.raises(HostNotAllowed, match="metadata"):
        resolve_target("169.254.169.254", allow_private=True)


def test_a_name_that_does_not_resolve_is_refused_rather_than_dialled() -> None:
    with pytest.raises(HostNotAllowed, match="does not resolve"):
        resolve_target("this-host-does-not-exist.invalid", allow_private=False)


async def test_a_refused_target_reaches_no_socket_and_verifies_nothing() -> None:
    """No transport here on purpose: this is the real outbound path."""
    environment = parse_manifest(_manifest(base_url="http://127.0.0.1:9"))

    observation = await run_checks(environment)

    assert not observation.reached
    assert observation.outcomes == []
    assert "loopback" in observation.note
