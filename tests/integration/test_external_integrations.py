"""Live verification of the four external integrations.

These replace one-off verification scripts. A verification that only ever ran
once, by hand, is not a verification — it is an anecdote. Each test runs for
real when its integration is configured and **skips naming the exact blocker**
when it is not, so a green suite never implies an integration works.

They require network access and real credentials, so they skip in CI. Run them
locally after any change to configuration or an external integration:

    uv run pytest tests/integration/test_external_integrations.py -v -rs

Nothing here prints a token, a key, or an account identifier.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend.shared.config import (
    GitHubAppConfig,
    GoogleOAuthConfig,
    Settings,
)

GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
EXPECTED_REDIRECT_URI = "http://localhost:8000/auth/callback"
TARGET_REPOSITORY = ("tony19053000", "continuity")


# --- AWS -----------------------------------------------------------------


@pytest.mark.requires_aws
def test_aws_credentials_resolve_through_the_standard_chain() -> None:
    """AWS auth must come from the chain, never from Continuity's own config."""
    settings = Settings()
    if not settings.AWS_PROFILE and not settings.AWS_REGION:
        pytest.skip("SKIPPED, NOT PASSED — AWS_PROFILE/AWS_REGION are not set.")

    boto3 = pytest.importorskip("boto3")
    botocore_exceptions = pytest.importorskip("botocore.exceptions")

    session = boto3.Session(
        profile_name=settings.AWS_PROFILE or None,
        region_name=settings.AWS_REGION or None,
    )
    try:
        identity = session.client("sts").get_caller_identity()
    except botocore_exceptions.BotoCoreError as exc:
        pytest.skip(f"SKIPPED, NOT PASSED — AWS credentials unavailable: {type(exc).__name__}")

    assert identity["Arn"].endswith(settings.AWS_PROFILE or "")
    assert session.region_name == settings.AWS_REGION


def test_no_aws_key_is_stored_in_continuity_configuration() -> None:
    """Runs always. The rule holds whether or not AWS is reachable."""
    fields = set(Settings.model_fields)

    for forbidden in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert forbidden not in fields, f"{forbidden} must not be a Continuity setting"


# --- GitHub App ----------------------------------------------------------


def _github_or_skip() -> GitHubAppConfig:
    config = Settings().github_app
    if not isinstance(config, GitHubAppConfig):
        pytest.skip(f"SKIPPED, NOT PASSED — GitHub App is not configured: {config.reason}")
    return config


@pytest.mark.requires_github_app
def test_an_app_jwt_can_be_minted() -> None:
    from backend.github.client import mint_app_jwt

    token = mint_app_jwt(_github_or_skip())

    assert token.count(".") == 2, "an RS256 JWT has three segments"


@pytest.mark.requires_github_app
async def test_installation_discovery_finds_the_installation() -> None:
    from backend.github.client import discover_installations

    installations = await discover_installations(_github_or_skip())

    assert installations, "the App is not installed anywhere"
    assert all(inst.installation_id > 0 for inst in installations)


@pytest.mark.requires_github_app
async def test_an_installation_token_grants_access_to_the_authorized_repository() -> None:
    from backend.github.client import GitHubAppClient, discover_installations

    config = _github_or_skip()
    installations = await discover_installations(config)
    client = GitHubAppClient(config, installations[0].installation_id)

    try:
        repositories = await client.list_repositories()
        owner, name = TARGET_REPOSITORY
        full_name = f"{owner}/{name}"

        assert any(repo.full_name == full_name for repo in repositories), (
            f"{full_name} is not in this installation's authorized set"
        )

        repository = await client.get_repository(owner, name)
        content = await client.read_file(owner, name, "README.md", repository.default_branch)

        assert content.startswith("# Continuity")
    finally:
        await client.aclose()


@pytest.mark.requires_github_app
async def test_an_unauthorized_repository_is_refused_by_the_live_api() -> None:
    """The boundary holds against GitHub itself, not only in unit tests."""
    from backend.github.client import (
        GitHubAppClient,
        RepositoryNotAuthorized,
        discover_installations,
    )

    config = _github_or_skip()
    installations = await discover_installations(config)
    client = GitHubAppClient(config, installations[0].installation_id)

    try:
        with pytest.raises(RepositoryNotAuthorized):
            await client.get_repository("torvalds", "linux")
    finally:
        await client.aclose()


@pytest.mark.requires_github_app
async def test_the_real_repository_can_be_indexed_through_the_github_source() -> None:
    """End to end: GitHub App → RepositorySource → deterministic index."""
    from backend.github.client import GitHubAppClient, discover_installations
    from backend.github.repository_source import GitHubRepositorySource
    from backend.repository.indexer import build_index

    config = _github_or_skip()
    installations = await discover_installations(config)
    client = GitHubAppClient(config, installations[0].installation_id)

    try:
        owner, name = TARGET_REPOSITORY
        repository = await client.get_repository(owner, name)
        source = await GitHubRepositorySource(
            client, owner, name, repository.default_branch
        ).load()

        index = build_index(source)
        summary = index.summary()

        assert summary["files_indexed"] > 50
        assert "python" in summary["languages"]
        assert "fastapi" in summary["frameworks"]
        # Continuity's own .env is excluded rather than fetched.
        assert ".env" not in index.files
    finally:
        await client.aclose()


# --- Google OAuth --------------------------------------------------------


def _google_or_skip() -> GoogleOAuthConfig:
    config = Settings().google_oauth
    if not isinstance(config, GoogleOAuthConfig):
        pytest.skip(f"SKIPPED, NOT PASSED — Google OAuth is not configured: {config.reason}")
    return config


@pytest.mark.requires_google_oauth
async def test_google_oidc_discovery_is_reachable() -> None:
    _google_or_skip()

    async with httpx.AsyncClient(timeout=20.0) as http:
        metadata = (await http.get(GOOGLE_DISCOVERY)).json()

    assert metadata["issuer"] == "https://accounts.google.com"


@pytest.mark.requires_google_oauth
async def test_the_authorization_redirect_matches_the_registered_client() -> None:
    """A mismatched `redirect_uri` fails at Google, long after our tests pass."""
    from backend.api.app import create_app
    from backend.models.session import create_all

    config = _google_or_skip()
    app = create_app(Settings())

    async with app.router.lifespan_context(app):
        await create_all()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost:8000"
        ) as client:
            response = await client.get("/auth/login", follow_redirects=False)

    assert response.status_code in (302, 307)

    parsed = urlparse(response.headers["location"])
    params = parse_qs(parsed.query)

    assert "accounts.google.com" in parsed.netloc
    assert params["client_id"][0] == config.client_id
    assert params["redirect_uri"][0] == EXPECTED_REDIRECT_URI
    assert params["response_type"][0] == "code"
    assert "state" in params
