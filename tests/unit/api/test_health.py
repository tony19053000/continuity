"""C1-01 acceptance: /health reports exactly four components and rolls up correctly."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.api.routers.health import HealthComponents, _roll_up

EXPECTED_COMPONENTS = {"database", "bedrock", "github_app", "job_queue"}


async def test_health_returns_the_documented_shape(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"version", "status", "components"}
    assert set(body["components"]) == EXPECTED_COMPONENTS


async def test_database_reports_ok_when_reachable(client: AsyncClient) -> None:
    body = (await client.get("/health")).json()
    assert body["components"]["database"] == "ok"


async def test_unconfigured_integrations_report_not_configured(client: AsyncClient) -> None:
    """A fresh checkout has no AWS or GitHub App credentials.

    Reporting `not_configured` rather than `ok` is the whole point: the system
    must never imply an integration works when it has not been told how.
    """
    body = (await client.get("/health")).json()

    assert body["components"]["bedrock"] == "not_configured"
    assert body["components"]["github_app"] == "not_configured"
    assert body["status"] == "degraded"


async def test_health_leaks_no_configuration_values(client: AsyncClient) -> None:
    """The response carries states, never values."""
    raw = (await client.get("/health")).text.lower()

    for forbidden in ("secret", "password", "token", "client_id", "aws_", "database_url"):
        assert forbidden not in raw


@pytest.mark.parametrize(
    ("database", "bedrock", "github_app", "job_queue", "expected"),
    [
        ("ok", "configured", "configured", "ok", "ok"),
        ("ok", "not_configured", "configured", "ok", "degraded"),
        ("ok", "configured", "not_configured", "ok", "degraded"),
        ("ok", "not_configured", "not_configured", "ok", "degraded"),
        ("error", "configured", "configured", "ok", "error"),
        ("ok", "configured", "configured", "error", "error"),
        # A required component failing outranks an unconfigured optional one.
        ("error", "not_configured", "not_configured", "ok", "error"),
    ],
)
def test_status_roll_up(
    database: str, bedrock: str, github_app: str, job_queue: str, expected: str
) -> None:
    components = HealthComponents(
        database=database,  # type: ignore[arg-type]
        bedrock=bedrock,  # type: ignore[arg-type]
        github_app=github_app,  # type: ignore[arg-type]
        job_queue=job_queue,  # type: ignore[arg-type]
    )
    assert _roll_up(components) == expected


async def test_openapi_schema_generates(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/health" in response.json()["paths"]
