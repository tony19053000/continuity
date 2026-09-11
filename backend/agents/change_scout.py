"""Change Scout: interpreting prose the differ cannot read.

The deterministic differ (C5-03) finds every schema-level change exactly. What it
cannot see is what a provider *says*: that a rate limit dropped, that an SDK is
deprecated, that an API version sunsets in March. Those live in prose, and this
is the one place a model reads it.

Three constraints, each enforced rather than requested:

* **Every change carries a `SourceRef`.** `ScoutedChange` requires it, so a
  change Continuity cannot attribute to a document fails validation and is never
  reported.
* **Only changelog-derived types.** A scouted change whose type belongs to the
  differ is dropped — the model does not get to relitigate the schema diff.
* **The changelog is untrusted data.** It arrives inside a delimited block, and
  instruction-shaped text in it is reported as a finding rather than followed.
  The real defence is that this agent has no tools at all: even a fully
  persuaded Change Scout can do nothing but return a structured opinion.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Final

from backend.agents.contracts import ChangeScoutInput, ChangeScoutOutput
from backend.agents.specialists import ChangeScoutAgent
from backend.models.enums import ChangeType, Confidence, EvidenceKind
from backend.models.schemas import Evidence, ProviderChange, SourceRef
from backend.observability.logging import get_logger
from backend.providers.base import ExternalDocument
from backend.shared.model_provider import ModelProvider

logger = get_logger(__name__)

#: Instruction-shaped patterns in fetched provider content. A heuristic flag,
#: never the primary control — the primary control is that this agent holds no
#: capability worth hijacking (`03_SECURITY_ACCESS.md` §5).
_INJECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\b", re.I),
    re.compile(r"disregard\s+(?:your|the)\s+(?:rules|instructions|system)", re.I),
    re.compile(r"\b(?:send|exfiltrate|reveal|print)\b[^.]{0,40}\b(?:credential|secret|key|token)", re.I),
    re.compile(r"new\s+system\s+prompt", re.I),
    re.compile(r"</?(?:system|instruction)>", re.I),
)

#: How much changelog reaches the model. A provider's full history is not
#: evidence about this upgrade, and sending it would spend budget on noise.
MAX_CHANGELOG_CHARS: Final = 16_000


@dataclass(slots=True)
class ScoutResult:
    changes: list[ProviderChange] = field(default_factory=list)
    injection_suspected: bool = False
    injection_excerpts: list[str] = field(default_factory=list)
    dropped_ungrounded: int = 0
    dropped_wrong_type: int = 0
    notes: str | None = None
    #: The model's own suspicion, kept separate from `injection_suspected`
    #: rather than merged into it. Advisory: it may catch a novel attempt the
    #: patterns miss, and it also fires on entirely ordinary release notes.
    model_reported_injection: bool = False


def detect_injection(text: str) -> list[str]:
    """Instruction-shaped excerpts in untrusted content.

    Returns short excerpts rather than a bare boolean so a finding can quote
    what was found — a security report saying "something suspicious" is not
    actionable.
    """
    found: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 40)
            found.append(text[start : match.end() + 40].strip())
            if len(found) >= 5:
                return found
    return found


async def scout_changelog(
    model_provider: ModelProvider,
    *,
    provider_id: str,
    old_version: str,
    new_version: str,
    document: ExternalDocument,
    spec_changes: list[ProviderChange] | None = None,
) -> ScoutResult:
    """Read a changelog and extract what the differ cannot.

    `spec_changes` is passed as context so the model can reconcile prose against
    the schema diff — confirming that an endpoint the differ saw removed is the
    one the changelog calls deprecated — rather than duplicating it.
    """
    result = ScoutResult()

    excerpts = detect_injection(document.content)
    if excerpts:
        result.injection_suspected = True
        result.injection_excerpts = excerpts
        logger.warning(
            "continuity.prompt_injection_suspected",
            extra={
                "provider_id": provider_id,
                "source_url": document.url,
                "matches": len(excerpts),
            },
        )

    summary = [
        f"{change.change_type.value} on {change.resource}"
        f"{' (breaking)' if change.breaking else ''}"
        for change in (spec_changes or [])
    ]

    agent = ChangeScoutAgent(model_provider)
    output = await agent.run(
        ChangeScoutInput(
            provider_id=provider_id,
            old_version=old_version,
            new_version=new_version,
            changelog_text=document.content[:MAX_CHANGELOG_CHARS],
            spec_diff_summary=summary,
        )
    )

    result.notes = output.notes
    result.model_reported_injection = output.injection_suspected

    # Deliberately *not* OR-ed into `injection_suspected`. Live Gemini raises
    # this on a benign provider changelog on roughly a quarter of runs, and a
    # security flag that fires on ordinary releases is one nobody reads. The
    # deterministic detection above is the authoritative signal because it can
    # quote what it found; the model's suspicion is recorded and logged so a
    # novel attempt the patterns miss is still visible, but it does not by
    # itself mark a document as hostile. LLM output never equals a decision.
    if output.injection_suspected and not result.injection_suspected:
        logger.info(
            "continuity.change_scout_reported_injection",
            extra={
                "provider_id": provider_id,
                "source_url": document.url,
                "note": "model-reported only; no pattern matched",
            },
        )
    result.changes = _accept(output, provider_id, old_version, new_version, document, result)
    return result


def _accept(
    output: ChangeScoutOutput,
    provider_id: str,
    old_version: str,
    new_version: str,
    document: ExternalDocument,
    result: ScoutResult,
) -> list[ProviderChange]:
    """Keep only grounded, changelog-derived changes.

    Both filters are the model being held to its brief rather than trusted: a
    spec-derivable type is the differ's answer rather than the model's, and a
    change whose quote is not in the document is one the model composed.
    """
    changelog_types = ChangeType.changelog_derived()
    accepted: list[ProviderChange] = []

    for change in output.changes:
        if change.change_type not in changelog_types:
            result.dropped_wrong_type += 1
            logger.info(
                "continuity.scouted_change_dropped",
                extra={
                    "reason": "spec-derivable type belongs to the differ",
                    "change_type": change.change_type.value,
                    "resource": change.resource,
                },
            )
            continue

        if not _is_grounded(change.evidence_quote, document.content):
            result.dropped_ungrounded += 1
            logger.info(
                "continuity.scouted_change_dropped",
                extra={
                    "reason": "quote not found in the fetched document",
                    "change_type": change.change_type.value,
                    "resource": change.resource,
                },
            )
            continue

        # Bind provenance to the document actually fetched. Everything here is
        # observed by Continuity; none of it is taken from the model, which has
        # no way to know any of it and will invent all three if asked.
        bound = SourceRef(
            kind=document.kind,
            url=document.url,
            document_hash=document.content_hash,
            retrieved_at=document.retrieved_at,
        )

        accepted.append(
            ProviderChange(
                provider_id=provider_id,
                old_version=old_version,
                new_version=new_version,
                change_type=change.change_type,
                resource=change.resource,
                old_contract=None,
                new_contract=None,
                breaking=change.breaking,
                security_relevant=change.security_relevant,
                authentication_relevant=change.authentication_relevant,
                source=bound,
                evidence=Evidence(
                    kind=EvidenceKind.CHANGELOG,
                    confidence=Confidence.INFERRED,
                    source_ref=bound,
                    # The quote, not the rationale: an excerpt should be what
                    # the provider wrote, not what the model made of it.
                    excerpt=change.evidence_quote[:4000],
                ),
            )
        )

    return accepted


def _normalize(text: str) -> str:
    """Collapse the formatting noise a quote survives but a string match does not.

    Models reliably reproduce the words and unreliably reproduce backticks,
    line wrapping, and smart quotes. Normalising those away keeps the check on
    the claim rather than on the typography.
    """
    lowered = text.lower().replace("`", "").replace("*", "")
    for pair in ("\u2018\u2019", "\u201c\u201d"):
        for character in pair:
            lowered = lowered.replace(character, "'" if pair[0] == "\u2018" else '"')
    return " ".join(lowered.split())


#: How much of a quote must match the document. Below 1.0 because a model that
#: quotes a full sentence and trims a trailing clause has still quoted the
#: document; far enough above 0.5 that a paraphrase sharing common words does
#: not pass.
GROUNDING_THRESHOLD: Final = 0.85


def _is_grounded(quote: str, document_text: str) -> bool:
    """Whether the quoted sentence actually appears in the fetched document.

    This is the anti-invention control, and unlike asking the model for a source
    reference it is checkable: Continuity holds the document, so it can simply
    look. A change the provider never wrote is dropped no matter how confidently
    it is asserted.
    """
    normalized_quote = _normalize(quote)
    if len(normalized_quote) < 12:
        # Too short to be evidence of anything; "v1" appears in every changelog.
        return False

    normalized_document = _normalize(document_text)
    if normalized_quote in normalized_document:
        return True

    # Near-verbatim: the model reproduced the sentence but clipped or joined it.
    matcher = difflib.SequenceMatcher(None, normalized_quote, normalized_document)
    match = matcher.find_longest_match(0, len(normalized_quote), 0, len(normalized_document))
    return match.size / len(normalized_quote) >= GROUNDING_THRESHOLD
