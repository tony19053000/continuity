"""C9-01: attack the migration before a human is asked to trust it.

The Security Reviewer asks whether the patch changed anything dangerous. The Red
Team asks a different question, and asks it of different input: given the code
exactly as it will exist after merge, what does a hostile provider or an attacker
do to it? A migration can be a perfectly clean diff and still ship a payment call
with no idempotency key.

Same two halves as the security review, for the same reason. Deterministic probes
in `backend/security/attacks.py` run whether or not a model is reachable, so the
floor does not depend on one. The agent reads the same files for what patterns
miss, and its attacks are marked INFERRED and merged rather than substituted — a
class code already landed is not re-reported by the model.

**The Red Team can stop a run; it cannot start one.** A HIGH or CRITICAL attack
returns the run to the repair loop with the attack as evidence. That is not the
model authorizing anything: refusing is the conservative direction, the repair
budget bounds how many times it can happen, and exhaustion ends at
`HUMAN_REVIEW_REQUIRED` where only a person can move it. Nothing here can permit
a delivery — `backend/security/policy.py` is still the only thing that decides
what is allowed, and `backend/orchestration/delivery_gate.py` is still the only
thing that lets a patch through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from backend.agents.contracts import AttackSurfaceFile, RedTeamInput, RedTeamOutput
from backend.agents.specialists import RedTeamAgent
from backend.models.enums import (
    AttackClass,
    Confidence,
    EvidenceKind,
    FindingCategory,
    PolicyDecision,
    Severity,
)
from backend.models.schemas import Evidence
from backend.observability.logging import get_logger
from backend.security.attacks import BLOCKING_SEVERITIES, Weakness, attack
from backend.shared.model_provider import ModelProvider
from backend.shared.redaction import redact

logger = get_logger(__name__)

#: How each attack class is recorded as a security finding. Attack classes are
#: what an attacker does; finding categories are what is wrong with the code —
#: and `backend/security/policy.py` classifies categories and nothing else, so
#: every attack has to land on one. A test asserts this mapping is total: an
#: `AttackClass` with no category could produce a finding policy never sees.
ATTACK_CATEGORY: Final[dict[AttackClass, FindingCategory]] = {
    AttackClass.MALFORMED_RESPONSE: FindingCategory.UNSAFE_PARAMETER,
    AttackClass.MISSING_FIELD: FindingCategory.UNSAFE_PARAMETER,
    AttackClass.UNEXPECTED_FIELD: FindingCategory.UNSAFE_PARAMETER,
    AttackClass.UNEXPECTED_NULL: FindingCategory.UNSAFE_PARAMETER,
    AttackClass.EXPIRED_CREDENTIAL: FindingCategory.AUTHENTICATION_CHANGE,
    AttackClass.INVALID_TOKEN: FindingCategory.AUTHENTICATION_CHANGE,
    AttackClass.WEBHOOK_REPLAY: FindingCategory.WEBHOOK_VERIFICATION,
    AttackClass.WEBHOOK_DUPLICATION: FindingCategory.WEBHOOK_VERIFICATION,
    AttackClass.DUPLICATE_TRANSACTION: FindingCategory.DUPLICATE_TRANSACTION_RISK,
    AttackClass.TIMEOUT: FindingCategory.DANGEROUS_RETRY,
    AttackClass.RETRY_STORM: FindingCategory.DANGEROUS_RETRY,
    AttackClass.RATE_LIMIT: FindingCategory.DANGEROUS_RETRY,
    AttackClass.MALICIOUS_EXTERNAL_TEXT: FindingCategory.PROMPT_INJECTION_SUSPECTED,
    AttackClass.PROMPT_INJECTION: FindingCategory.PROMPT_INJECTION_SUSPECTED,
    AttackClass.UNAUTHORIZED_TOOL: FindingCategory.TOOL_MISUSE,
    AttackClass.PERMISSION_ESCALATION: FindingCategory.PRIVILEGE_EXPANSION,
    AttackClass.INVALID_SIGNATURE: FindingCategory.WEBHOOK_VERIFICATION,
}

#: How much source the model sees. A migration larger than this is attacked on
#: its deterministic probes alone, which the report states rather than hiding.
MAX_SOURCE_CHARS: Final = 60_000


@dataclass(frozen=True, slots=True)
class Attack:
    """One attack that would land."""

    attack: AttackClass
    category: FindingCategory
    severity: Severity
    summary: str
    evidence: Evidence
    source: str  # "deterministic" or "red_team"

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    def summary_dict(self) -> dict[str, object]:
        return {
            "attack": self.attack.value,
            "category": self.category.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "source": self.source,
            "file_path": self.evidence.file_path,
        }


@dataclass(slots=True)
class RedTeamReport:
    """What the attack run found, and how much of it ran."""

    attacks: list[Attack] = field(default_factory=list)
    summary: str = ""
    model_consulted: bool = False
    source_truncated: bool = False
    files_attacked: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[Attack]:
        return [item for item in self.attacks if item.blocking]

    @property
    def held(self) -> bool:
        """Whether the migration survived. Named for what it asserts.

        `passed` would claim the code is safe. This claims only that no attack
        in the catalogue landed hard, which is all that was tested.
        """
        return not self.blocking

    def report(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "model_consulted": self.model_consulted,
            "source_truncated": self.source_truncated,
            "files_attacked": sorted(self.files_attacked),
            "blocking": len(self.blocking),
            "attacks": [item.summary_dict() for item in self.attacks],
        }

    def brief(self) -> str:
        """What the Migration Engineer is told, so the next attempt responds."""
        if not self.blocking:
            return ""
        lines = ["The Red Team broke the previous patch:"]
        for item in self.blocking:
            where = item.evidence.file_path or "the migrated code"
            line = f" line {item.evidence.line_start}" if item.evidence.line_start else ""
            lines.append(f"- [{item.severity.value}] {where}{line}: {item.summary}")
        lines.append("Fix these in the code. Do not weaken or remove a test to pass.")
        return "\n".join(lines)


def _from_weakness(weakness: Weakness) -> Attack:
    return Attack(
        attack=weakness.attack,
        category=ATTACK_CATEGORY[weakness.attack],
        severity=weakness.severity,
        summary=redact(weakness.summary),
        evidence=weakness.evidence(),
        source="deterministic",
    )


async def attack_migration(
    *,
    provider_id: str,
    files: dict[str, str],
    model_provider: ModelProvider | None = None,
) -> RedTeamReport:
    """Attack the post-migration source. Probes first, then the model.

    `files` maps a repository-relative path to the file's content *after* the
    patch was applied. Passing a diff here would silently test the wrong thing —
    the weaknesses this looks for are usually in code the patch never touched.
    """
    report = RedTeamReport(files_attacked=sorted(files))

    for weakness in attack(files):
        report.attacks.append(_from_weakness(weakness))

    already_found = {item.attack for item in report.attacks}

    if model_provider is not None and files:
        await _consult(
            report,
            model_provider=model_provider,
            provider_id=provider_id,
            files=files,
            already_found=already_found,
        )

    if not report.summary:
        report.summary = (
            f"{len(report.attacks)} attack(s) would land; "
            f"{len(report.blocking)} block delivery."
            if report.attacks
            else "No attack in the catalogue landed against this migration."
        )

    logger.info(
        "continuity.red_team",
        extra={
            "attacks": len(report.attacks),
            "blocking": len(report.blocking),
            "files": len(files),
            "model_consulted": report.model_consulted,
        },
    )
    return report


def _bounded(files: dict[str, str]) -> tuple[list[AttackSurfaceFile], bool]:
    """As much source as the budget allows, whole files only.

    A file is included entirely or not at all. Half a file reads as a complete
    one to the model, and a weakness in the missing half becomes an assurance
    that it is not there.
    """
    bounded: list[AttackSurfaceFile] = []
    used = 0
    truncated = False
    for path in sorted(files):
        content = files[path]
        if used + len(content) > MAX_SOURCE_CHARS:
            truncated = True
            continue
        bounded.append(AttackSurfaceFile(path=path, content=content))
        used += len(content)
    return bounded, truncated


async def _consult(
    report: RedTeamReport,
    *,
    model_provider: ModelProvider,
    provider_id: str,
    files: dict[str, str],
    already_found: set[AttackClass],
) -> None:
    """Add what the model found, without letting it overwrite the probes."""
    bounded, truncated = _bounded(files)
    report.source_truncated = truncated
    if not bounded:
        report.summary = (
            "The migrated source was too large to attack with a model; this "
            "report covers only the deterministic probes."
        )
        return

    agent = RedTeamAgent(model_provider)
    try:
        output: RedTeamOutput = await agent.run(
            RedTeamInput(
                provider_id=provider_id,
                files=bounded,
                already_found=sorted(item.value for item in already_found),
            )
        )
    except Exception as exc:
        # The probes stand. Treating a failed model call as "nothing found"
        # would be an assurance nobody produced.
        logger.warning(
            "continuity.red_team_failed", extra={"error": type(exc).__name__}
        )
        report.summary = (
            "The Red Team agent could not be consulted; this report covers only "
            "the deterministic probes."
        )
        return

    report.model_consulted = True
    report.summary = redact(output.summary)

    attacked_paths = {item.path for item in bounded}
    for proposed in output.attacks:
        if proposed.attack in already_found:
            # A probe already landed this class and quoted the line.
            continue
        if proposed.file_path not in attacked_paths:
            # The agent cited a file it was not given. A finding whose evidence
            # points nowhere cannot be checked, and an unverifiable finding that
            # can stop a run is a way to stall one.
            logger.warning(
                "continuity.red_team_unlocatable_attack",
                extra={"attack": proposed.attack.value},
            )
            continue

        report.attacks.append(
            Attack(
                attack=proposed.attack,
                category=ATTACK_CATEGORY[proposed.attack],
                severity=proposed.severity,
                summary=redact(proposed.summary),
                evidence=Evidence(
                    kind=EvidenceKind.SOURCE,
                    # INFERRED: the model read the file and formed a view. A
                    # probe that matched produces CONFIRMED.
                    confidence=Confidence.INFERRED,
                    file_path=proposed.file_path,
                    excerpt=redact(proposed.excerpt or proposed.summary)[:1000],
                ),
                source="red_team",
            )
        )


def recommendation_for(report: RedTeamReport) -> PolicyDecision:
    """What the Red Team advises. Advisory, like every agent opinion here.

    Returned separately from the finding rows so the Security Reviewer's own
    recommendation is not overwritten by it, and so a disagreement between the
    two is visible rather than merged away.
    """
    return PolicyDecision.DENY if report.blocking else PolicyDecision.ALLOW
