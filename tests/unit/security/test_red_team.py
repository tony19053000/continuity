"""C9-01: one fixture per attack class, plus the merge and reporting rules.

Every attack class in the catalogue gets a fixture here that exhibits the
weakness and a counter-fixture that does not. Both halves matter: a probe that
fires on everything is as useless as one that fires on nothing, and only the
negative case distinguishes them.

The fixtures are written as post-migration source — the code as it would exist
after merge — because that is what the Red Team is given. Handing these probes a
diff would test the wrong module.
"""

from __future__ import annotations

import pytest

from backend.agents.contracts import ProposedAttack, RedTeamOutput
from backend.agents.red_team import (
    ATTACK_CATEGORY,
    attack_migration,
    recommendation_for,
)
from backend.models.enums import AttackClass, Confidence, PolicyDecision, Severity
from backend.security.attacks import BLOCKING_SEVERITIES, SEVERITY, attack
from tests.support.agent_stubs import FixedRunner

PATH = "app/payments.py"


def _landed(source: str) -> set[AttackClass]:
    return {weakness.attack for weakness in attack({PATH: source})}


# --- One fixture per attack class ----------------------------------------

#: (attack class, code that suffers it, code that does not).
#
# The safe variant is not merely the weakness removed — it is the defence a real
# engineer would write, so a probe cannot pass this table by matching on the
# absence of a keyword.
CASES: list[tuple[AttackClass, str, str]] = [
    (
        AttackClass.MALFORMED_RESPONSE,
        """
import httpx

def fetch(client):
    response = client.get("/v1/charges", timeout=5)
    return response.json()
""",
        """
import httpx

def fetch(client):
    response = client.get("/v1/charges", timeout=5)
    try:
        return response.json()
    except ValueError:
        return {}
""",
    ),
    (
        AttackClass.MISSING_FIELD,
        """
def read(client):
    data = client.get("/v1/charges", timeout=5).json()
    return data["currency"]
""",
        """
def read(client):
    data = client.get("/v1/charges", timeout=5).json()
    return data.get("currency", "usd")
""",
    ),
    (
        AttackClass.UNEXPECTED_FIELD,
        """
def build(client, Charge):
    data = client.get("/v1/charges", timeout=5).json()
    return Charge(**data)
""",
        """
def build(client, Charge):
    data = client.get("/v1/charges", timeout=5).json()
    return Charge(amount=data.get("amount", 0))
""",
    ),
    (
        AttackClass.UNEXPECTED_NULL,
        """
def total(client):
    data = client.get("/v1/charges", timeout=5).json()
    return float(data.get("amount"))
""",
        """
def total(client):
    data = client.get("/v1/charges", timeout=5).json()
    amount = data.get("amount")
    return float(amount) if amount is not None else 0.0
""",
    ),
    (
        AttackClass.EXPIRED_CREDENTIAL,
        """
def charge(client, api_key):
    return client.post(
        "/v1/charges",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5,
    ).status_code
""",
        """
def charge(client, api_key, refresh_token):
    response = client.post(
        "/v1/charges",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5,
    )
    if response.status_code == 401:
        api_key = refresh_token()
    return response
""",
    ),
    (
        AttackClass.INVALID_TOKEN,
        """
def charge(client, api_key):
    response = client.post(
        "/v1/charges",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5,
    )
    if response.reason == "token_expired":
        return None
    return response
""",
        """
def charge(client, api_key):
    response = client.post(
        "/v1/charges",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5,
    )
    response.raise_for_status()
    if response.status_code == 401:
        raise PermissionError("acmepay rejected the credential")
    return response
""",
    ),
    (
        AttackClass.WEBHOOK_REPLAY,
        """
def handle_webhook(payload, delivery_id, seen_event):
    if seen_event(delivery_id):
        return None
    return payload["type"]
""",
        """
import time

def handle_webhook(payload, delivery_id, seen_event):
    if seen_event(delivery_id):
        return None
    if time.time() - payload["timestamp"] > 300:
        raise ValueError("stale webhook delivery")
    return payload["type"]
""",
    ),
    (
        AttackClass.WEBHOOK_DUPLICATION,
        """
import time

def handle_webhook(payload):
    if time.time() - payload["sent_at"] > 300:
        raise ValueError("stale webhook delivery")
    return payload["type"]
""",
        """
import time

def handle_webhook(payload, already_processed):
    if time.time() - payload["sent_at"] > 300:
        raise ValueError("stale webhook delivery")
    if already_processed(payload["event_id"]):
        return None
    return payload["type"]
""",
    ),
    (
        AttackClass.DUPLICATE_TRANSACTION,
        """
def charge(client, order_id, amount):
    return client.post(
        "/v1/charges",
        json={"order": order_id, "amount": amount},
        timeout=5,
    )
""",
        """
def charge(client, order_id, amount, key):
    return client.post(
        "/v1/charges",
        json={"order": order_id, "amount": amount},
        headers={"Idempotency-Key": key},
        timeout=5,
    )
""",
    ),
    (
        AttackClass.TIMEOUT,
        """
def fetch(client):
    return client.get("/v1/charges")
""",
        """
def fetch(client):
    return client.get("/v1/charges", timeout=5)
""",
    ),
    (
        AttackClass.RETRY_STORM,
        """
def fetch(client):
    while True:
        response = client.get("/v1/charges", timeout=5)
        if response.status_code == 200:
            return response
""",
        """
import time

def fetch(client):
    for attempt in range(3):
        response = client.get("/v1/charges", timeout=5)
        if response.status_code == 200:
            return response
        time.sleep(2**attempt)
    return None
""",
    ),
    (
        AttackClass.RATE_LIMIT,
        """
def fetch(client):
    return client.get("/v1/charges", timeout=5)
""",
        """
def fetch(client):
    response = client.get("/v1/charges", timeout=5)
    if response.status_code == 429:
        raise RuntimeError(response.headers["Retry-After"])
    return response
""",
    ),
    (
        AttackClass.MALICIOUS_EXTERNAL_TEXT,
        """
from markupsafe import Markup

def render(client):
    data = client.get("/v1/charges", timeout=5).json()
    return Markup(data["description"])
""",
        """
from markupsafe import escape

def render(client):
    data = client.get("/v1/charges", timeout=5).json()
    return escape(data.get("description", ""))
""",
    ),
    (
        AttackClass.PROMPT_INJECTION,
        """
def summarise(client, model):
    data = client.get("/v1/charges", timeout=5).json()
    return model.invoke(data["description"])
""",
        """
def summarise(client, model):
    data = client.get("/v1/charges", timeout=5).json()
    return data.get("description", "")
""",
    ),
    (
        AttackClass.UNAUTHORIZED_TOOL,
        """
import subprocess

def sync(client):
    subprocess.run(["./sync.sh"], check=True)
    return client.get("/v1/charges", timeout=5)
""",
        """
def sync(client):
    return client.get("/v1/charges", timeout=5)
""",
    ),
    (
        AttackClass.PERMISSION_ESCALATION,
        """
SCOPES = "charges.admin"

def fetch(client):
    return client.get("/v1/charges", timeout=5)
""",
        """
SCOPES = "charges.read"

def fetch(client):
    return client.get("/v1/charges", timeout=5)
""",
    ),
    (
        AttackClass.INVALID_SIGNATURE,
        """
def verify(payload, signature, expected):
    return signature == expected
""",
        """
import hmac

def verify(payload, signature, expected):
    return hmac.compare_digest(signature, expected)
""",
    ),
]


@pytest.mark.parametrize(
    ("attack_class", "vulnerable", "safe"),
    CASES,
    ids=[case[0].value for case in CASES],
)
def test_each_attack_class_has_a_fixture_that_lands_and_one_that_does_not(
    attack_class: AttackClass, vulnerable: str, safe: str
) -> None:
    assert attack_class in _landed(vulnerable), "the attack should have landed"
    assert attack_class not in _landed(safe), "the defended code should be clean"


def test_the_catalogue_covers_every_attack_class() -> None:
    """A class with no fixture is a class nobody proved was implemented."""
    assert {case[0] for case in CASES} == set(AttackClass)


def test_every_attack_class_has_a_severity_and_a_finding_category() -> None:
    """A gap in either table produces a finding policy never classifies."""
    assert set(SEVERITY) == set(AttackClass)
    assert set(ATTACK_CATEGORY) == set(AttackClass)


# --- Reporting rules ------------------------------------------------------


def test_a_probe_fires_once_per_file() -> None:
    """Nine unguarded calls are one timeout problem, not nine findings."""
    source = "\n".join(
        f"def fetch_{index}(client):\n    return client.get('/v1/charges')\n"
        for index in range(9)
    )
    timeouts = [w for w in attack({PATH: source}) if w.attack is AttackClass.TIMEOUT]
    assert len(timeouts) == 1


def test_findings_quote_a_line_of_code_not_the_docstring() -> None:
    """An anchor in prose points a reviewer at nothing.

    This is a real defect that existed: the money-moving probe matched the word
    "payment" in the module docstring and cited line 1.
    """
    source = '''"""Payment integration: charges an order at checkout."""

def charge(client, order_id):
    return client.post("/v1/charges", json={"order": order_id}, timeout=5)
'''
    (weakness,) = [
        w
        for w in attack({PATH: source})
        if w.attack is AttackClass.DUPLICATE_TRANSACTION
    ]
    assert weakness.line == 4
    assert "client.post" in weakness.excerpt


#: Code that is correct, and mentions a weakness only in prose. Each of these
#: raised a finding before probes were made comment-aware — and the escalation
#: one raised a *blocking* finding with no line and no excerpt, which would have
#: returned a correct migration to the repair loop with nothing to fix.
PROSE_ONLY = [
    pytest.param(
        '# scope = "full" was required by the old SDK; we now request scope="read"\n'
        'def fetch(client):\n'
        '    return client.get("/v1/charges", timeout=5)\n',
        AttackClass.PERMISSION_ESCALATION,
        id="a comment naming the scope that was removed",
    ),
    pytest.param(
        '"""This module used to verify a webhook signature."""\n'
        "def fetch(client):\n"
        '    return client.get("/v1/charges", timeout=5)\n',
        AttackClass.INVALID_SIGNATURE,
        id="a docstring mentioning a signature",
    ),
    pytest.param(
        "# TODO: this should charge the customer once the endpoint exists\n"
        "def fetch(client):\n"
        '    return client.get("/v1/charges", timeout=5)\n',
        AttackClass.DUPLICATE_TRANSACTION,
        id="a comment describing a charge that is not made",
    ),
]


@pytest.mark.parametrize(("source", "attack_class"), PROSE_ONLY)
def test_prose_alone_does_not_raise_a_finding(
    source: str, attack_class: AttackClass
) -> None:
    assert attack_class not in _landed(source)


def test_no_finding_is_reported_without_a_line_to_quote() -> None:
    """A CONFIRMED finding that cannot point at code is not evidence.

    At a blocking severity it is worse than useless: an unanswerable complaint
    that returns the run to repair every pass, which no edit can satisfy.
    """
    for source, _ in [(case.values[0], case.values[1]) for case in PROSE_ONLY]:
        for weakness in attack({PATH: source}):
            assert weakness.line is not None
            assert weakness.excerpt.strip()


def test_a_defence_mentioned_only_in_a_comment_does_not_suppress_a_finding() -> None:
    """The same rule in the other direction, which is the dangerous one.

    If prose counted as a defence, writing "we set a timeout here" would be
    enough to silence the timeout probe.
    """
    source = (
        "def fetch(client):\n"
        "    # we should pass timeout=5 here\n"
        '    return client.get("/v1/charges")\n'
    )
    assert AttackClass.TIMEOUT in _landed(source)


def test_code_sharing_a_line_with_a_docstring_is_still_seen() -> None:
    """Prose is blanked character for character, not by dropping the line.

    An earlier version skipped any line where a fence resolved, which silently
    disabled every probe gated on a token that happened to sit there.
    """
    source = (
        "def handle(payload, client):\n"
        '    """Docs.\n'
        '    More docs."""; signature = payload["sig"]\n'
        '    return signature == "expected"\n'
    )
    assert AttackClass.INVALID_SIGNATURE in _landed(source)


def test_a_string_literal_is_code_and_still_matches() -> None:
    """Only a string that is a statement by itself is prose.

    `SCOPES = "charges.admin"` is the escalation, not a description of one.
    """
    source = 'SCOPES = "charges.admin"\n\n\ndef fetch(client):\n    return client.get("/v1/charges", timeout=5)\n'
    assert AttackClass.PERMISSION_ESCALATION in _landed(source)


def test_a_file_that_cannot_be_parsed_is_still_attacked() -> None:
    """A syntax error in one file is not a reason to stop attacking."""
    landed = _landed("def broken(:\n    signature == expected\n")
    assert AttackClass.INVALID_SIGNATURE in landed


def test_only_high_and_critical_attacks_block() -> None:
    blocking = {cls for cls, severity in SEVERITY.items() if severity in BLOCKING_SEVERITIES}
    assert AttackClass.DUPLICATE_TRANSACTION in blocking
    assert AttackClass.INVALID_SIGNATURE in blocking
    assert AttackClass.RATE_LIMIT not in blocking
    assert AttackClass.UNEXPECTED_FIELD not in blocking


# --- The agent half -------------------------------------------------------


class _Provider:
    """Enough of a model provider to construct an agent."""

    def build_model(self, role: object) -> object:
        return object()


#: Integration code with no weakness in the catalogue. Used wherever a test
#: needs the deterministic probes to contribute nothing, so that what the model
#: half did is unambiguous.
CLEAN = """
def charge(client, order_id, amount, key):
    response = client.post(
        "/v1/charges",
        json={"order": order_id, "amount": amount},
        headers={"Idempotency-Key": key},
        timeout=5,
    )
    if response.status_code == 429:
        raise RuntimeError("rate limited")
    return response
"""


async def test_the_probes_run_with_no_model_at_all() -> None:
    """The floor does not depend on a model being reachable."""
    report = await attack_migration(
        provider_id="acmepay",
        files={PATH: CASES[8][1]},
        model_provider=None,
    )
    assert not report.model_consulted
    assert {item.attack for item in report.attacks} >= {
        AttackClass.DUPLICATE_TRANSACTION
    }
    assert report.blocking
    assert not report.held


async def test_a_clean_migration_holds() -> None:
    report = await attack_migration(
        provider_id="acmepay", files={PATH: CLEAN}, model_provider=None
    )
    assert report.attacks == []
    assert report.held
    assert recommendation_for(report) is PolicyDecision.ALLOW


async def test_a_model_failure_leaves_the_probes_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the agent degrades the attack; it never becomes an assurance."""
    from backend.agents import red_team as module

    monkeypatch.setattr(
        module, "RedTeamAgent", _agent_returning(RuntimeError("model unavailable"))
    )
    report = await attack_migration(
        provider_id="acmepay", files={PATH: CASES[8][1]}, model_provider=_Provider()
    )

    assert not report.model_consulted
    assert report.blocking, "the deterministic finding survives"
    assert "deterministic probes" in report.summary


async def test_the_model_cannot_restate_a_class_the_probes_already_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import red_team as module

    monkeypatch.setattr(
        module,
        "RedTeamAgent",
        _agent_returning(
            RedTeamOutput(
                attacks=[
                    ProposedAttack(
                        attack=AttackClass.DUPLICATE_TRANSACTION,
                        severity=Severity.CRITICAL,
                        summary="I also noticed the missing idempotency key.",
                        file_path=PATH,
                    )
                ],
                summary="One attack.",
            )
        ),
    )
    report = await attack_migration(
        provider_id="acmepay", files={PATH: CASES[8][1]}, model_provider=_Provider()
    )

    duplicates = [
        item
        for item in report.attacks
        if item.attack is AttackClass.DUPLICATE_TRANSACTION
    ]
    assert len(duplicates) == 1
    assert duplicates[0].source == "deterministic"


async def test_an_attack_citing_a_file_it_was_not_given_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finding whose evidence points nowhere cannot be checked.

    It would also be a way to stall a run indefinitely: an unlocatable CRITICAL
    returns the run to repair every pass, and no edit could ever answer it.
    """
    from backend.agents import red_team as module

    monkeypatch.setattr(
        module,
        "RedTeamAgent",
        _agent_returning(
            RedTeamOutput(
                attacks=[
                    ProposedAttack(
                        attack=AttackClass.UNAUTHORIZED_TOOL,
                        severity=Severity.CRITICAL,
                        summary="There is a subprocess call in a file I invented.",
                        file_path="app/not_given.py",
                    )
                ],
                summary="One attack.",
            )
        ),
    )
    report = await attack_migration(
        provider_id="acmepay", files={PATH: CLEAN}, model_provider=_Provider()
    )

    assert report.attacks == []
    assert report.held


async def test_a_model_attack_is_recorded_as_inferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import red_team as module

    monkeypatch.setattr(
        module,
        "RedTeamAgent",
        _agent_returning(
            RedTeamOutput(
                attacks=[
                    ProposedAttack(
                        attack=AttackClass.MALFORMED_RESPONSE,
                        severity=Severity.MEDIUM,
                        summary="The retry branch decodes an error page.",
                        file_path=PATH,
                        excerpt="return response.json()",
                    )
                ],
                summary="One attack.",
            )
        ),
    )
    report = await attack_migration(
        provider_id="acmepay", files={PATH: CLEAN}, model_provider=_Provider()
    )

    (item,) = report.attacks
    assert item.source == "red_team"
    assert item.evidence.confidence is Confidence.INFERRED


async def test_the_report_says_what_it_attacked() -> None:
    """`files_attacked` is the scope of the claim, so it must be real."""
    report = await attack_migration(
        provider_id="acmepay",
        files={PATH: CLEAN, "app/other.py": "x = 1\n"},
        model_provider=None,
    )
    assert report.report()["files_attacked"] == ["app/other.py", PATH]


async def test_the_brief_names_every_blocking_attack() -> None:
    """What the Migration Engineer is told on the next attempt."""
    report = await attack_migration(
        provider_id="acmepay", files={PATH: CASES[8][1]}, model_provider=None
    )
    brief = report.brief()
    assert "idempotency" in brief.lower()
    assert PATH in brief
    assert "weaken or remove a test" in brief


def _agent_returning(outcome: object) -> type:
    """An agent class whose runner returns (or raises) `outcome`."""
    from backend.agents.specialists import RedTeamAgent

    class Patched(RedTeamAgent):  # type: ignore[misc]
        def __init__(self, provider: object, **kwargs: object) -> None:
            kwargs.pop("runner", None)
            super().__init__(provider, runner=FixedRunner(outcome), **kwargs)  # type: ignore[arg-type]

    return Patched
