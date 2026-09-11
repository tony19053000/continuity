"""Domain enumerations.

`RunState` is the authoritative Python form of the state machine in
`02_ARCHITECTURE.md` §8. The transition table itself arrives with C2-04
(Phase 2); this module defines only the states, so that persistence can store
them now.

Adding a state here without adding its edges in §8 and in `ALLOWED_TRANSITIONS`
will fail C2-04's exact-match test. That is intentional.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    """Every state a project or run may occupy. 45 members, matching §8."""

    # --- Project lifecycle ---
    PROJECT_CREATED = "project_created"
    GITHUB_CONNECTED = "github_connected"
    REPOSITORY_SELECTED = "repository_selected"
    INITIAL_SCAN_PENDING = "initial_scan_pending"
    INITIAL_SCAN_RUNNING = "initial_scan_running"
    INITIAL_SCAN_COMPLETE = "initial_scan_complete"
    INTEGRATION_MAPPING_RUNNING = "integration_mapping_running"
    INTEGRATION_MAPPING_COMPLETE = "integration_mapping_complete"
    MONITORING_ACTIVE = "monitoring_active"

    # --- Change evaluation ---
    CHANGE_DETECTED = "change_detected"
    CHANGE_ANALYSIS_RUNNING = "change_analysis_running"
    CHANGE_ANALYSIS_COMPLETE = "change_analysis_complete"
    IMPACT_ANALYSIS_RUNNING = "impact_analysis_running"
    CHANGE_IRRELEVANT = "change_irrelevant"
    CHANGE_RELEVANT = "change_relevant"

    # --- Rehearsal ---
    REHEARSAL_PENDING = "rehearsal_pending"
    REHEARSAL_RUNNING = "rehearsal_running"
    REHEARSAL_CONFIRMED = "rehearsal_confirmed"
    REHEARSAL_UNAVAILABLE = "rehearsal_unavailable"
    REHEARSAL_FAILED = "rehearsal_failed"

    # --- Migration and validation ---
    MIGRATION_PENDING = "migration_pending"
    MIGRATION_RUNNING = "migration_running"
    PATCH_READY = "patch_ready"
    VALIDATION_RUNNING = "validation_running"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    REPAIR_RUNNING = "repair_running"

    # --- Security and approval ---
    SECURITY_REVIEW_RUNNING = "security_review_running"
    SECURITY_REVIEW_PASSED = "security_review_passed"
    SECURITY_REVIEW_FAILED = "security_review_failed"
    APPROVAL_PENDING = "approval_pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    # --- Delivery and verification ---
    FINAL_VALIDATION_RUNNING = "final_validation_running"
    FINAL_VALIDATION_PASSED = "final_validation_passed"
    PR_PENDING = "pr_pending"
    PR_CREATING = "pr_creating"
    PR_CREATED = "pr_created"
    MERGE_WAITING = "merge_waiting"
    POST_MERGE_VERIFICATION_RUNNING = "post_merge_verification_running"
    POST_MERGE_VERIFICATION_PASSED = "post_merge_verification_passed"
    POST_MERGE_VERIFICATION_FAILED = "post_merge_verification_failed"
    VERIFIED = "verified"

    # --- Escape states ---
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    RUN_FAILED = "run_failed"


class AgentRole(StrEnum):
    """Continuity's runtime agents. Not the Claude Code development subagents."""

    ORCHESTRATOR = "orchestrator"
    CHANGE_SCOUT = "change_scout"
    INTEGRATION_MAPPER = "integration_mapper"
    IMPACT_ANALYST = "impact_analyst"
    MIGRATION_ENGINEER = "migration_engineer"
    VALIDATOR = "validator"
    SECURITY_REVIEWER = "security_reviewer"
    RED_TEAM = "red_team"
    RELEASE_GUARDIAN = "release_guardian"


class Confidence(StrEnum):
    """Whether a fact was derived deterministically or proposed by a model.

    This distinction is preserved end to end and surfaced in the UI. Inferred
    data may never be presented as confirmed (`02_ARCHITECTURE.md` §5).
    """

    CONFIRMED = "confirmed"
    INFERRED = "inferred"


class ChangeType(StrEnum):
    """Provider change classification (`02_ARCHITECTURE.md` §10).

    Split by derivation: `SPEC_DERIVABLE` members come from the deterministic
    differ (C5-03); `CHANGELOG_DERIVED` members come from the Change Scout
    (C5-04). `spec_derivable()` and `changelog_derived()` below are the
    authoritative partition and are asserted disjoint and complete by test.
    """

    # Spec-derivable
    ENDPOINT_REMOVED = "endpoint_removed"
    ENDPOINT_ADDED = "endpoint_added"
    ENDPOINT_RENAMED = "endpoint_renamed"
    REQUEST_FIELD_REMOVED = "request_field_removed"
    REQUEST_FIELD_ADDED = "request_field_added"
    REQUEST_FIELD_REQUIRED = "request_field_required"
    RESPONSE_FIELD_REMOVED = "response_field_removed"
    RESPONSE_SHAPE_CHANGED = "response_shape_changed"
    ENUM_CHANGED = "enum_changed"
    WEBHOOK_EVENT_CHANGED = "webhook_event_changed"
    AUTHENTICATION_CHANGED = "authentication_changed"
    OAUTH_SCOPE_CHANGED = "oauth_scope_changed"
    HEADER_REQUIREMENT_CHANGED = "header_requirement_changed"
    ERROR_CONTRACT_CHANGED = "error_contract_changed"

    # Changelog-derived
    RATE_LIMIT_CHANGED = "rate_limit_changed"
    SDK_DEPRECATED = "sdk_deprecated"
    API_VERSION_DEPRECATED = "api_version_deprecated"
    DOCUMENTATION_ONLY = "documentation_only"

    @classmethod
    def spec_derivable(cls) -> frozenset[ChangeType]:
        """Types the deterministic differ may emit."""
        return frozenset(
            {
                cls.ENDPOINT_REMOVED,
                cls.ENDPOINT_ADDED,
                cls.ENDPOINT_RENAMED,
                cls.REQUEST_FIELD_REMOVED,
                cls.REQUEST_FIELD_ADDED,
                cls.REQUEST_FIELD_REQUIRED,
                cls.RESPONSE_FIELD_REMOVED,
                cls.RESPONSE_SHAPE_CHANGED,
                cls.ENUM_CHANGED,
                cls.WEBHOOK_EVENT_CHANGED,
                cls.AUTHENTICATION_CHANGED,
                cls.OAUTH_SCOPE_CHANGED,
                cls.HEADER_REQUIREMENT_CHANGED,
                cls.ERROR_CONTRACT_CHANGED,
            }
        )

    @classmethod
    def changelog_derived(cls) -> frozenset[ChangeType]:
        """Types only prose or registry metadata can reveal."""
        return frozenset(
            {
                cls.RATE_LIMIT_CHANGED,
                cls.SDK_DEPRECATED,
                cls.API_VERSION_DEPRECATED,
                cls.DOCUMENTATION_ONLY,
            }
        )


class NodeKind(StrEnum):
    """Integration Intelligence Graph node types (`02_ARCHITECTURE.md` §5)."""

    PROVIDER = "provider"
    SDK = "sdk"
    FILE = "file"
    SYMBOL = "symbol"
    CALL_SITE = "call_site"
    WORKFLOW = "workflow"
    TEST = "test"
    PERMISSION = "permission"


class EdgeKind(StrEnum):
    """Integration Intelligence Graph edge types."""

    DECLARES_SDK = "declares_sdk"
    DEFINED_IN = "defined_in"
    CALLS_PROVIDER = "calls_provider"
    IMPLEMENTS_WORKFLOW = "implements_workflow"
    COVERED_BY_TEST = "covered_by_test"
    REQUIRES_PERMISSION = "requires_permission"
    HANDLES_WEBHOOK_EVENT = "handles_webhook_event"


class PolicyDecision(StrEnum):
    """Deterministic authorization outcome (`03_SECURITY_ACCESS.md` §4)."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalStatus(StrEnum):
    """Approval is persisted state. No model output can produce APPROVED."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingCategory(StrEnum):
    """Security finding categories (`03_SECURITY_ACCESS.md` §10)."""

    # S105 suppressed: this names a finding category, not a credential.
    SECRET_EXPOSURE = "secret_exposure"  # noqa: S105
    PRIVILEGE_EXPANSION = "privilege_expansion"
    OAUTH_SCOPE_CHANGE = "oauth_scope_change"
    AUTHENTICATION_CHANGE = "authentication_change"
    AUTHORIZATION_WEAKENED = "authorization_weakened"
    WEBHOOK_VERIFICATION = "webhook_verification"
    UNSAFE_PARAMETER = "unsafe_parameter"
    DANGEROUS_RETRY = "dangerous_retry"
    DUPLICATE_TRANSACTION_RISK = "duplicate_transaction_risk"
    NEW_DEPENDENCY = "new_dependency"
    TOOL_MISUSE = "tool_misuse"
    PROMPT_INJECTION_SUSPECTED = "prompt_injection_suspected"
    TEST_WEAKENED = "test_weakened"


class AttackClass(StrEnum):
    """The attack classes the Red-Team agent tries (`05_FEATURE_TICKETS.md` C9-01).

    These are not the same axis as `FindingCategory`. A finding category names
    *what is wrong with a change*; an attack class names *what an attacker or a
    misbehaving provider does*. The two are related by
    `backend/agents/red_team.py`'s `ATTACK_CATEGORY`, because the policy engine
    classifies categories and nothing else — an attack class never reaches
    policy directly.
    """

    MALFORMED_RESPONSE = "malformed_response"
    MISSING_FIELD = "missing_field"
    UNEXPECTED_FIELD = "unexpected_field"
    UNEXPECTED_NULL = "unexpected_null"
    EXPIRED_CREDENTIAL = "expired_credential"
    INVALID_TOKEN = "invalid_token"  # noqa: S105
    WEBHOOK_REPLAY = "webhook_replay"
    WEBHOOK_DUPLICATION = "webhook_duplication"
    DUPLICATE_TRANSACTION = "duplicate_transaction"
    TIMEOUT = "timeout"
    RETRY_STORM = "retry_storm"
    RATE_LIMIT = "rate_limit"
    MALICIOUS_EXTERNAL_TEXT = "malicious_external_text"
    PROMPT_INJECTION = "prompt_injection"
    UNAUTHORIZED_TOOL = "unauthorized_tool"
    PERMISSION_ESCALATION = "permission_escalation"
    INVALID_SIGNATURE = "invalid_signature"


class ActivityEventKind(StrEnum):
    """User-facing activity events (`02_ARCHITECTURE.md` §16).

    This is a closed vocabulary, and a test asserts the member set matches §16
    exactly. Keeping it closed is what lets the UI render a known set of states
    instead of guessing at free-form strings, and what stops a stray event name
    from silently never appearing in the timeline.

    These are *summaries*, never model reasoning. Raw chain-of-thought is never
    persisted or displayed.
    """

    REPOSITORY_SCAN_STARTED = "repository_scan_started"
    INTEGRATION_DETECTED = "integration_detected"
    INTEGRATION_MAPPING_COMPLETE = "integration_mapping_complete"
    PROVIDER_CHANGE_DETECTED = "provider_change_detected"
    CHANGE_CLASSIFIED = "change_classified"
    IMPACT_ANALYSIS_STARTED = "impact_analysis_started"
    IMPACTED_WORKFLOW_DETECTED = "impacted_workflow_detected"
    REHEARSAL_STARTED = "rehearsal_started"
    REHEARSAL_CONFIRMED = "rehearsal_confirmed"
    MIGRATION_STARTED = "migration_started"
    PATCH_GENERATED = "patch_generated"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_FAILED = "validation_failed"
    REPAIR_STARTED = "repair_started"
    VALIDATION_PASSED = "validation_passed"
    SECURITY_REVIEW_STARTED = "security_review_started"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RECEIVED = "approval_received"
    MIGRATION_VERIFIED = "migration_verified"
    PULL_REQUEST_CREATED = "pull_request_created"


class JobKind(StrEnum):
    """Background job kinds (`02_ARCHITECTURE.md` §15)."""

    PROVIDER_MONITOR = "provider_monitor"
    REPOSITORY_SCAN = "repository_scan"
    INTEGRATION_MAP = "integration_map"
    CHANGE_ANALYSIS = "change_analysis"
    IMPACT_ANALYSIS = "impact_analysis"
    REHEARSAL = "rehearsal"
    MIGRATION = "migration"
    VALIDATION = "validation"
    SECURITY_REVIEW = "security_review"
    PR_CREATE = "pr_create"
    PR_STATUS_POLL = "pr_status_poll"
    POST_MERGE_VERIFY = "post_merge_verify"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"


class EvidenceKind(StrEnum):
    SOURCE = "source"
    MANIFEST = "manifest"
    PROVIDER_SPEC = "provider_spec"
    CHANGELOG = "changelog"
    TEST_OUTPUT = "test_output"


class SourceKind(StrEnum):
    """Where external provider information came from."""

    OPENAPI_SPEC = "openapi_spec"
    CHANGELOG = "changelog"
    DOCS = "docs"
    GITHUB_RELEASE = "github_release"
    SDK_REGISTRY = "sdk_registry"
    VERSION_ENDPOINT = "version_endpoint"
    API_FEED = "api_feed"
