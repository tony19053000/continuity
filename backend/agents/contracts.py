"""Input and output contracts for every runtime agent.

These are the typed hand-off points between agents. Each is a Pydantic model
passed to Strands as `structured_output_model`, so an agent physically cannot
return prose where the orchestrator expects a decision.

Evidence is required, not optional, almost everywhere. An agent that cannot cite
a file, a line, or a source document has not produced a usable finding — and
making that a schema requirement rather than a convention is what stops
unsourced claims reaching the UI.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import ChangeType, FindingCategory, PolicyDecision, Severity
from backend.models.schemas import Evidence


class _Contract(BaseModel):
    """Strict base: unknown fields are an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


# --- Change Scout --------------------------------------------------------


class ChangeScoutInput(_Contract):
    provider_id: str
    old_version: str
    new_version: str
    changelog_text: str | None = Field(
        default=None, description="Untrusted external content"
    )
    spec_diff_summary: list[str] = Field(
        default_factory=list,
        description="Changes the deterministic differ already found (C5-03)",
    )


class ScoutedChange(_Contract):
    """One change the changelog states and the differ could not see.

    Note what is *not* asked for: a `SourceRef`. An earlier version required one,
    on the reasoning that a change the agent cannot attribute is one it may have
    invented. In practice the agent reads exactly one document and has no way to
    know its URL or hash, so it filled the field with plausible placeholders --
    live Gemini returned `url="changelog"`, `document_hash="acmepay-v2-changelog"`
    -- which passed the attribution check while attributing nothing, and on other
    runs returned an empty URL and had real findings discarded. A control that
    admits invented values and rejects true ones at random is worse than none.

    `evidence_quote` replaces it with something code can actually check: the
    sentence must appear in the document Continuity fetched, or the change is
    dropped. Provenance is then bound from that fetch, where it is a fact.
    """

    change_type: ChangeType
    resource: str
    breaking: bool
    security_relevant: bool
    authentication_relevant: bool
    rationale: str = Field(max_length=1000)
    evidence_quote: str = Field(
        max_length=500,
        description=(
            "A sentence copied word for word from the changelog that states "
            "this change. It is checked against the document, so it must be "
            "quoted exactly rather than paraphrased."
        ),
    )


class ChangeScoutOutput(_Contract):
    changes: list[ScoutedChange]
    injection_suspected: bool = Field(
        default=False,
        description="Set when the changelog contained instruction-shaped text",
    )
    notes: str | None = Field(default=None, max_length=2000)


# --- Integration Mapper --------------------------------------------------


class IntegrationMapperInput(_Contract):
    project_id: str
    detected_providers: list[str]
    file_summaries: list[str]
    call_site_summaries: list[str]


class InferredWorkflow(_Contract):
    """One business workflow the agent believes these symbols implement.

    Evidence is requested as flat fields rather than a nested `Evidence` model,
    for two reasons. First, Gemini's structured-output schema is an OpenAPI
    subset that rejects the nested-optional shape `Evidence` has — asking for it
    made the model fail to produce output at all. Second, the nested model
    carries `confidence` and `source_ref`, which the agent has no business
    setting: code fixes confidence to INFERRED and source evidence has no
    external source. The `Evidence` object is assembled in `mapper.py`.
    """

    name: str = Field(description="A short business workflow name, e.g. 'Checkout'")
    basis: str = Field(max_length=500, description="Why these symbols form this workflow")
    symbol_keys: list[str] = Field(
        description="Exact symbol keys from the input, formatted 'path::qualified_name'"
    )
    evidence_file: str = Field(description="Repository-relative path supporting this")
    evidence_line_start: int = Field(ge=1, description="First line of the supporting code")
    evidence_line_end: int = Field(ge=1, description="Last line of the supporting code")


class IntegrationMapperOutput(_Contract):
    """Workflows only.

    An earlier version also asked for `provider_identities`, to "reconcile
    provider identity". Nothing consumed it and the prompt never requested it —
    a field that claims a capability the code does not have. Provider identity
    is better resolved deterministically by the adapter registry in Phase 5
    than inferred, so the field is gone rather than left as a promise.
    """

    workflows: list[InferredWorkflow]


# --- Impact Analyst ------------------------------------------------------


class ImpactAnalystInput(_Contract):
    project_id: str
    change: ScoutedChange
    correlated_call_sites: list[str]
    correlated_workflows: list[str]
    relevant_source_slices: list[str]


class ImpactAnalystOutput(_Contract):
    relevant: bool
    severity: Severity
    migration_required: bool
    affected_files: list[str]
    affected_symbols: list[str]
    affected_workflows: list[str]
    affected_tests: list[str]
    authentication_consequence: str | None = Field(default=None, max_length=1000)
    reasoning_summary: str = Field(max_length=2000)
    evidence: list[Evidence]


# --- Migration Engineer --------------------------------------------------


class MigrationEngineerInput(_Contract):
    provider_id: str
    from_version: str
    to_version: str
    impact: ImpactAnalystOutput
    file_contents: dict[str, str]
    previous_failure: str | None = Field(
        default=None, description="Validator evidence from the last attempt"
    )
    attempt_number: int = Field(default=1, ge=1)


class FileEdit(_Contract):
    path: str
    new_content: str
    justification: str = Field(max_length=1000)


class MigrationEngineerOutput(_Contract):
    plan_summary: str = Field(max_length=2000)
    edits: list[FileEdit]
    # Declared explicitly so a weakened test is a visible decision rather than a
    # silent diff. The policy engine classifies this as ASK.
    modifies_tests: bool = False
    test_modification_justification: str | None = Field(default=None, max_length=1000)
    new_dependencies: list[str] = Field(default_factory=list)
    diagnosis: str | None = Field(
        default=None, description="Why the previous attempt failed", max_length=2000
    )


# --- Validator -----------------------------------------------------------


class ValidatorInput(_Contract):
    """Parsed test output. The Validator interprets; it does not run anything.

    Execution happens through `ExecutionProvider` (C6-01) and parsing is
    deterministic, so no test result can originate from a model.
    """

    suite: str
    command: str
    exit_code: int
    passed: int
    failed: int
    skipped: int
    failing_test_ids: list[str]
    output_excerpt: str


class ValidatorOutput(_Contract):
    build_ok: bool
    tests_passed: bool
    failure_summary: str | None = Field(default=None, max_length=2000)
    likely_causes: list[str] = Field(default_factory=list)
    related_to_provider_change: bool = False


# --- Security Reviewer ---------------------------------------------------


class SecurityReviewerInput(_Contract):
    provider_id: str
    patch_diff: str
    changed_dependencies: list[str]
    modifies_tests: bool


class ProposedFinding(_Contract):
    category: FindingCategory
    severity: Severity
    summary: str = Field(max_length=1000)
    evidence: Evidence
    # Advisory only. `backend/security/policy.py` produces the authoritative
    # decision, and a disagreement between the two is recorded.
    recommendation: PolicyDecision


class SecurityReviewerOutput(_Contract):
    findings: list[ProposedFinding]
    overall_recommendation: PolicyDecision
    summary: str = Field(max_length=2000)


# --- Orchestrator --------------------------------------------------------


class OrchestratorInput(_Contract):
    migration_run_id: str
    current_state: str
    available_evidence: list[str]


class OrchestratorOutput(_Contract):
    """A *proposal*. The state machine decides whether it is legal.

    The Orchestrator never moves a run itself — `transition()` is not a tool,
    and the coordinator validates this proposal against `ALLOWED_TRANSITIONS`
    before acting on it.
    """

    proposed_next_state: str
    rationale: str = Field(max_length=1000)
    blocking_reason: str | None = Field(default=None, max_length=1000)
