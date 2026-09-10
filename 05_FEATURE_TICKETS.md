# 05 — Feature Tickets

**Status:** Phase 0 baseline. Living state — refine ticket detail as each phase
is reached, but never silently change scope.

Ticket format: **ID · Title** — purpose, files, dependencies, implementation,
acceptance criteria, tests, security, status.

Status values: `PENDING` · `IN PROGRESS` · `IN REVIEW` · `DONE` · `BLOCKED`

Every phase ends at a review gate. Only `reviewer-tester` returns PASS. A ticket
is `DONE` only after PASS.

**Dependency rule:** a ticket may only depend on tickets in the same or an
earlier phase. Any exception must be written into the ticket explicitly, naming
what stands in until the dependency lands.

---

## PHASE 0 — Project anchoring (0% → 10%)

### C0-01 · Development subagents
**Purpose:** Establish the CODER / REVIEWER-TESTER review gate.
**Files:** `.claude/agents/coder.md`, `.claude/agents/reviewer-tester.md`
**Deps:** none
**Implementation:** Project-level Claude Code agents with explicit
responsibilities, hard rules, and tool scopes. Reviewer is read/test-oriented
and is the only PASS authority.
**Acceptance:** Both files exist with valid frontmatter; coder cannot declare
completion; reviewer requires evidence per acceptance criterion.
**Tests:** n/a (process artifact)
**Security:** Both agents carry explicit anti-faking and anti-secret rules.
**Status:** DONE

### C0-02 · Anchor documentation
**Purpose:** Fix product, architecture, security, and UI decisions before code.
**Files:** `01_PRD.md`, `02_ARCHITECTURE.md`, `03_SECURITY_ACCESS.md`,
`04_FRONTEND_SPEC.md`, `05_FEATURE_TICKETS.md` (five anchor documents;
`STATUS.md` and `CLAUDE.md` are delivered by C0-03)
**Deps:** C0-01
**Implementation:** Five anchor documents including the verified Strands/Bedrock
facts table with source URLs, the deterministic state machine as an explicit
edge list, the `ProviderAdapter` interface, the Integration Intelligence Graph
schema, the migration-run schema, the ALLOW/ASK/DENY matrix, and the GitHub App
permission strategy.
**Acceptance:** All five documents exist and are mutually consistent — state
names, `ChangeType` members, agent names and count, `ProviderAdapter` methods,
the `Evidence`/`SourceRef` models, the security matrix, and the Integration
Health formula agree across documents; every displayed example figure is
producible by its documented formula; no invented credentials, model ids, or
ARNs; every upstream fact carries a source URL and a verification date.
**Tests:** n/a
**Security:** `03_SECURITY_ACCESS.md` covers all three adversaries in the
threat model.
**Status:** DONE

### C0-03 · Project scaffolding and context files
**Purpose:** Make the repository buildable and future sessions recoverable.
**Files:** `CLAUDE.md`, `STATUS.md`, `README.md`, `LICENSE`, `.gitignore`,
`.env.example`
**Deps:** C0-02
**Implementation:** Apache-2.0 license; `.gitignore` covering Python, Node, and
every secret path in `03_SECURITY_ACCESS.md` §2; `.env.example` documenting
every configuration variable; `STATUS.md` with the real completion percentage,
current phase, blockers, and context log.
**Acceptance:** `.env.example` contains no secret, credential, endpoint, or
account-specific value (non-sensitive defaults such as timeouts and limits are
permitted and documented as such); `.gitignore` excludes every §2 secret path
plus `.env*`, venvs, `node_modules`, `.next`, and build output; `LICENSE` is the
unmodified canonical Apache-2.0 text; `STATUS.md` exists and every document that
references it resolves.
**Tests:** n/a
**Security:** No secret is committed. Verified by reviewing the staged diff.
**Status:** DONE

### C0-04 · Documentation review gate
**Purpose:** Independent verification before any code is written.
**Deps:** C0-01…C0-03
**Acceptance:** `reviewer-tester` returns PASS against the Phase 0 checklist in
`CLAUDE.md` §6; all findings corrected; `STATUS.md` set to 10%; clean Phase 0
commit.
**Status:** DONE

---

## PHASE 1 — Application + backend foundation (10% → 20%)

### C1-01 · Backend application skeleton
**Purpose:** A running FastAPI app with clean layering.
**Files:** `backend/api/app.py`, `backend/api/routers/`, `backend/api/deps.py`,
`pyproject.toml`
**Deps:** Phase 0
**Implementation:** FastAPI app factory, router registration, lifespan hooks,
and a `/health` endpoint.
**Acceptance:** `uvicorn` serves the app and the OpenAPI schema generates.
`/health` returns `{version, status, components}` where `components` covers
exactly: `database` (`ok` | `error`, from a real connectivity check),
`gemini` (`configured` | `not_configured` — configuration presence only, never
a live model call), `github_app` (`configured` | `not_configured`), and
`job_queue` (`ok` | `error`). Overall `status` is `ok` only when every `ok`-class
component is `ok`; a `not_configured` optional component yields `degraded`, not
`error`, and never `ok`.
**Tests:** `tests/unit/api/test_health.py` via httpx ASGI transport, asserting
each component state and the overall roll-up.
**Security:** `/health` exposes no credentials, secret values, or environment
contents — only the four component states above.
**Status:** DONE

### C1-02 · Configuration and environment validation
**Purpose:** Fail fast and loudly on misconfiguration; never invent values.
**Files:** `backend/shared/config.py`, `.env.example`
**Deps:** C1-01
**Implementation:** `pydantic-settings` `Settings` with typed fields for
`BEDROCK_MODEL_ID`, `AWS_REGION`, database URL, GitHub App fields, auth fields,
and `MAX_REPAIR_ATTEMPTS`. Optional integrations resolve to an explicit
`NotConfigured` state rather than a default that pretends to work.
**Acceptance:** Missing required config raises a startup error naming the exact
variable; missing optional config yields `NotConfigured`, not a fake value; every
variable in `.env.example` is represented and none is undocumented.
**Tests:** unit tests for present, absent, and malformed configuration; a test
asserting `.env.example` and `Settings` fields are in sync.
**Security:** `Settings.__repr__` and `__str__` redact every secret field.
Asserted by test.
**Status:** DONE

### C1-03 · Persistence layer and domain models
**Purpose:** Durable, transactional state for the whole workflow.
**Files:** `backend/models/`, `alembic/`
**Deps:** C1-02
**Implementation:** SQLAlchemy 2.x models and an Alembic baseline for the tables
in `02_ARCHITECTURE.md` §14. Paired Pydantic domain schemas. Repository-pattern
accessors so agents never touch sessions.
**Acceptance:** `alembic upgrade head` creates every table listed in §14;
round-trip create/read tests pass for each core model; `approvals` rows require a
non-null `actor_user_id` at the database level.
**Tests:** `tests/unit/models/` round-trip and constraint tests, including a test
that an approval insert without an actor fails.
**Security:** No credential column stores plaintext.
**Status:** DONE

### C1-04 · Errors, logging, activity events
**Purpose:** Structured, secret-safe diagnostics from day one.
**Files:** `backend/shared/errors.py`, `backend/observability/`
**Deps:** C1-03
**Implementation:** Typed exception hierarchy mapped to HTTP responses;
structured JSON logging; `ActivityEvent` emission and persistence using the
vocabulary in `02_ARCHITECTURE.md` §16.
**Acceptance:** Error responses carry a typed code and message with no stack
trace, file path, or internal detail; every emitted activity event persists with
`run_id`, actor, timestamp, summary, and evidence reference; the emitted event
names are exactly the §16 vocabulary.
**Tests:** unit tests including one asserting a secret-bearing value is redacted
in a log record.
**Security:** Redaction filter applied at the logging handler level, so it cannot
be bypassed by a caller that forgets it.
**Status:** DONE

### C1-05 · User authentication and session
**Purpose:** Produce the authenticated human identity that the entire approval
guarantee depends on.
**Files:** `backend/api/auth/`, `backend/models/user.py`, `apps/web/` sign-in
route
**Deps:** C1-02, C1-03
**Implementation:** Google OAuth 2.0 authorization-code flow via Authlib;
server-side session with a signed, `HttpOnly`, `SameSite=Lax`, `Secure` cookie;
`users` record keyed by Google subject id; a `current_user` FastAPI dependency;
sign-out invalidating the session. Resolves open decision 1 in
`02_ARCHITECTURE.md` §20.
**Acceptance:** A signed-in user is resolvable via `current_user`; unauthenticated
requests to protected routes return 401; the session cookie is never readable by
JavaScript; sign-in grants **no** repository access, and the UI states this
(`04_FRONTEND_SPEC.md` §3.1); absent Google config yields `NotConfigured` and a
clear error rather than an insecure fallback.
**Tests:** unit tests for the callback, session issue/expiry/sign-out, and the
401 path; a test asserting cookie flags.
**Security:** No local password storage. CSRF state parameter validated on
callback. Tokens are never returned to the browser. This ticket is a hard
prerequisite for C2-08 and C8-02 — an approval requires a real user id.
**Status:** DONE

### C1-06 · Frontend shell
**Purpose:** Minimal Next.js app — shell only, no dashboard.
**Files:** `apps/web/`
**Deps:** C1-01
**Implementation:** Next.js App Router, TypeScript, Tailwind, base layout, error
boundary, typed API client aligned to the OpenAPI schema.
**Acceptance:** `npm run build` succeeds; the shell renders; the API client
reaches `/health` from a browser origin and renders its component states —
which requires CORS allowing exactly `FRONTEND_ORIGIN` with credentials, tested
against the real middleware rather than a stubbed `fetch`.
**Tests:** one Vitest render test; typecheck and lint pass.
**Security:** No secret is referenced in client code; only `NEXT_PUBLIC_*`
variables reach the browser. Asserted by the CI job's build-output grep over
`.next/static` and `.next/server` (`.github/workflows/ci.yml`), which runs after
a real `next build` — a check that cannot run meaningfully inside the unit test
suite because it needs the compiled bundle.
**Status:** DONE

### C1-07 · Engineering baseline
**Purpose:** The commands the reviewer runs at every gate.
**Files:** `pyproject.toml`, `apps/web/package.json`, `.github/workflows/ci.yml`
**Deps:** C1-01, C1-06
**Implementation:** Configure `pytest`, `ruff`, `mypy`, and `npm run
typecheck|lint|test|build`; CI running all of them.
**Acceptance:** All seven commands pass on a clean checkout; CI is green.
**Tests:** the suite itself.
**Security:** CI holds no secrets; the workflow sets read-only default
permissions.
**Status:** DONE

---

## PHASE 2 — Strands + core orchestration (20% → 30%)

### C2-01 · Model provider abstraction
**Purpose:** One place that constructs a model.
**Files:** `backend/shared/model_provider.py`
**Deps:** C1-02
**Implementation:** `ModelProvider` protocol; `BedrockModelProvider` wrapping
`strands.models.BedrockModel` with `model_id` and `region_name` from settings
and a per-role temperature.
**Acceptance:** `BedrockModel(` appears in exactly one non-test module; the model
id comes from `BEDROCK_MODEL_ID` and falls back to the documented Strands default
(`02_ARCHITECTURE.md` §1) rather than a hardcoded literal elsewhere; an
unavailable Bedrock raises a typed error naming the missing configuration.
**Tests:** unit tests with a stubbed model; an AST-based test enforcing the
single-construction-site rule.
**Security:** AWS credentials come from the standard credential chain only —
never from config values or code.
**Status:** DONE

### C2-02 · Agent base and structured output
**Purpose:** A uniform, safe contract for every runtime agent.
**Files:** `backend/agents/base.py`
**Deps:** C2-01
**Implementation:** `ContinuityAgent` base declaring role, versioned system
prompt, `allowed_tools`, `input_model`, `output_model`, `max_attempts`, and
`on_error`. Invocation passes `structured_output_model` and reads
`result.structured_output`.
**Acceptance:** Output failing `output_model` validation is retried up to
`max_attempts` and then raises an escalation error — never coerced, never
partially accepted; an agent invoking a tool outside `allowed_tools` raises
before dispatch; external content appears in prompts only inside a delimited
untrusted-data block.
**Tests:** unit tests for valid output, malformed output exhausting retries, and
a denied out-of-allowlist tool call.
**Security:** See `03_SECURITY_ACCESS.md` §5 and §8.
**Status:** DONE

### C2-03 · Tool registry and dispatcher
**Purpose:** The enforcement point between agents and capability.
**Files:** `backend/agents/tools/`, `backend/security/policy.py`
**Deps:** C2-02, C2-08
**Implementation:** Strands `@tool` functions registered with a per-role
allowlist. Every dispatch calls `PolicyEngine.classify` → ALLOW/ASK/DENY before
execution. ASK creates an `ApprovalRequest` (C2-08) and pauses the run; DENY
refuses and audits.
**Acceptance:** Every row of the matrix in `03_SECURITY_ACCESS.md` §4 has a
corresponding classification and a test; DENY records an
`unauthorized_action_attempt` audit row naming the agent, action, and context;
no registered tool is reachable except through the dispatcher, asserted by a
test that enumerates the registry.
**Tests:** `tests/security/test_policy_matrix.py` covering every matrix row.
**Security:** This ticket *is* the security boundary — no model sits in the
decision path.
**Status:** DONE

### C2-04 · Deterministic state machine
**Purpose:** Single source of truth for run progress.
**Files:** `backend/orchestration/state_machine.py`
**Deps:** C1-03
**Implementation:** `ALLOWED_TRANSITIONS` map and
`transition(run, to, evidence)` persisting state and evidence in one
transaction; `IllegalTransition` otherwise.
**Acceptance:** `ALLOWED_TRANSITIONS` matches the edge list in
`02_ARCHITECTURE.md` §8 exactly — a test compares the two edge sets and fails on
any difference in either direction; every state is reachable from
`PROJECT_CREATED`; every non-terminal state has an outgoing edge; illegal
transitions raise; each transition persists actor and evidence.
**Tests:** exhaustive legal/illegal transition tests; reachability and
no-dead-end graph tests; a concurrency test that two racing transitions cannot
both commit.
**Security:** `transition` is not exposed as an agent-callable tool.
**Status:** DONE

### C2-05 · Orchestrator agent and run coordinator
**Purpose:** Drive a run without owning policy.
**Files:** `backend/agents/orchestrator.py`,
`backend/orchestration/coordinator.py`
**Deps:** C2-02, C2-03, C2-04, C2-08
**Implementation:** Coordinator sequences agents, enforces retry budgets, pauses
on `APPROVAL_PENDING`, resumes on stored approval, and routes validation and
security failures back to the Migration Engineer.
**Acceptance:** A run paused at `APPROVAL_PENDING` resumes correctly after a
process restart; the Orchestrator cannot reach a state absent from
`ALLOWED_TRANSITIONS`; the Orchestrator has no approval-write capability,
asserted by a test over its tool allowlist.
**Tests:** integration test of a full stubbed run including pause and resume
across a restart.
**Security:** Orchestrator does not override policy.
**Status:** DONE

### C2-06 · Agent skeletons
**Purpose:** Real, tool-capable skeletons for the six specialist agents.
**Files:** `backend/agents/{change_scout,integration_mapper,impact_analyst,migration_engineer,validator,security_reviewer}.py`
**Deps:** C2-02
**Implementation:** Role, prompt, allowlist, and Pydantic input/output contracts
for each. An unimplemented capability raises `NotImplementedError` rather than
returning plausible text.
**Acceptance:** Each agent declares all seven contract properties from
`02_ARCHITECTURE.md` §6; no agent method returns a literal result value; only
`migration_engineer` holds a file-write tool, asserted by a test over all six
allowlists.
**Tests:** contract tests per agent; a test asserting no agent returns a
hardcoded result.
**Security:** Least tool privilege per role.
**Status:** DONE

### C2-07 · Strands end-to-end proof
**Purpose:** Prove Strands genuinely drives a tool and returns validated state.
**Files:** `tests/integration/test_strands_roundtrip.py`
**Deps:** C2-01, C2-02, C2-03
**Implementation:** A test in which a Strands agent calls a controlled Continuity
tool (passing through a real policy check) and returns a validated Pydantic
structure that drives a real state transition.
**Acceptance:** With Bedrock configured the test passes end to end; without it,
the test **skips with a message naming the exact missing configuration** — it
never silently passes, and `STATUS.md` records the blocker.
**Tests:** this ticket is the test.
**Security:** Exercises the policy path, not a bypass.
**Status:** DONE

### C2-08 · Approval state model
**Purpose:** Persisted approval state that the dispatcher and coordinator need in
Phase 2, ahead of the full approval product surface in Phase 8.
**Files:** `backend/approvals/models.py`, `backend/approvals/service.py`
**Deps:** C1-03, C1-05
**Implementation:** `ApprovalRequest` and `ApprovalRecord` with
`PENDING | APPROVED | REJECTED`, trigger reason, risk classification, actor user
id, and timestamps. A service that creates a request, pauses the run, and
resolves it. C8-02 later adds the HTTP API, the resume-and-recheck flow, and the
full integrity test suite; this ticket delivers only the state and service.
**Acceptance:** An approval record cannot be created or mutated from any module
under `backend/agents/`, asserted by an import-graph test; writing `APPROVED`
requires an authenticated user id from C1-05; the state survives a restart.
**Tests:** unit tests for each transition; a test that an agent-layer write
attempt fails.
**Security:** Approval is deterministic state. No model output can produce it.
**Status:** DONE

---

## PHASE 3 — GitHub + safe repository ingestion (30% → 40%)

### C3-01 · Secret filter
**Purpose:** The control that must exist before any file is read into context.
**Files:** `backend/security/secret_filter.py`
**Deps:** Phase 1
**Implementation:** Path exclusions and content patterns from
`03_SECURITY_ACCESS.md` §2, applied at all five enforcement points.
**Acceptance:** Every listed path pattern is excluded and every listed content
pattern is redacted, each with a fixture; the filter is invoked at all five
enforcement points, asserted by a call-site test; a secret-bearing fixture file
cannot appear in any model-bound context.
**Tests:** `tests/security/test_secret_filter.py` with a fixture per pattern plus
negative tests guarding against false positives on ordinary code.
**Security:** Blocking prerequisite for C3-04.
**Status:** DONE

### C3-02 · GitHub App integration
**Purpose:** Scoped, least-privilege repository access.
**Files:** `backend/github/`
**Deps:** C1-02, C1-05
**Implementation:** App JWT → installation token flow; repository listing;
content and branch reads.
**Acceptance:** The client class exposes no method that force-pushes, rewrites
history, writes to a default branch, or merges — asserted by a test enumerating
its public surface; tokens are never persisted or logged; every repository call
re-validates the authorization boundary; absent App config yields
`NotConfigured` rather than a crash or a fake success.
**Tests:** unit tests with a mocked GitHub API; `tests/security/` negative tests
for the prohibited operations.
**Security:** Permission set exactly matches `03_SECURITY_ACCESS.md` §3.
**Status:** DONE

### C3-03 · Local repository adapter
**Purpose:** Develop and test ingestion without GitHub.
**Files:** `backend/repository/local_adapter.py`
**Deps:** C3-01
**Implementation:** `RepositorySource` protocol with a local implementation,
clearly named as a development adapter, enforcing the same boundary and secret
rules as the GitHub source.
**Acceptance:** `..` traversal, absolute paths, and symlinks pointing outside the
root are each rejected with a typed error; the adapter satisfies the same
`RepositorySource` conformance test suite as the GitHub source.
**Tests:** boundary-escape tests in `tests/security/`; a shared conformance suite
run against both sources.
**Security:** Never presented as production GitHub access.
**Status:** DONE

### C3-04 · Repository indexer
**Purpose:** Deterministic understanding before any model involvement.
**Files:** `backend/repository/indexer.py`, `backend/repository/analyzers/`
**Deps:** C3-01, C3-03
**Implementation:** Exclusion filtering, file classification, manifest parsing,
Python AST analysis (imports, symbols with line spans, call sites, decorators),
TS/JS import graph, HTTP-client detection, webhook detection, and framework and
language detection.
**Acceptance:** Against `tests/fixtures/sample_repo/`, the index equals a
committed expected-index snapshot — files, symbols with line spans, manifests,
detected clients, and detected webhooks; excluded paths are never opened
(asserted by an open() spy); files above the size limit are skipped by `stat`,
without being read.
**Tests:** `tests/integration/test_indexer.py` with fixture repositories.
**Security:** No content leaves the process.
**Status:** DONE

### C3-05 · Relevant-context retrieval
**Purpose:** Send the model slices, never repositories.
**Files:** `backend/repository/retrieval.py`
**Deps:** C3-04
**Implementation:** Query the index for symbol- and call-site-scoped slices under
a hard byte budget; every slice secret-filtered and carrying an `Evidence`
record.
**Acceptance:** Retrieved context never exceeds `CONTEXT_BUDGET_BYTES`; a whole
file is emitted only when the requested symbol spans it; every slice carries
non-null `Evidence` with a file path and line span.
**Tests:** budget-enforcement, evidence-completeness, and secret-exclusion tests.
**Security:** A test asserts a secret-bearing fixture cannot appear in retrieved
context.
**Status:** DONE

### C3-06 · Scan orchestration and summary
**Purpose:** Wire ingestion into the run lifecycle.
**Files:** `backend/workers/repository_scan.py`
**Deps:** C3-04, C2-04
**Implementation:** Scan job driving `INITIAL_SCAN_PENDING → INITIAL_SCAN_RUNNING
→ INITIAL_SCAN_COMPLETE`, emitting activity events per step and persisting a
scan summary.
**Acceptance:** The states and emitted events match §8 and §16 exactly; the
source tree is byte-identical before and after a scan, asserted by a recursive
content hash.
**Tests:** integration test over a fixture repository, including the
read-only hash assertion in `tests/security/`.
**Security:** Read-only analysis is a security guarantee, not just a behaviour.
**Status:** DONE

---

## PHASE 4 — Integration Mapper + Intelligence Graph (40% → 50%)

### C4-01 · Graph persistence and query layer
**Purpose:** Store and query the Integration Intelligence Graph.
**Files:** `backend/integrations/graph.py`, models for
`graph_nodes`/`graph_edges`
**Deps:** C1-03
**Implementation:** Versioned, immutable-per-scan graph storage; the query
methods listed in `02_ARCHITECTURE.md` §5 including `blast_radius`.
**Acceptance:** All eight node types and seven edge types persist with non-null
`Evidence` and a `confirmed`/`inferred` flag; each query method returns the
expected result against a committed graph fixture; `blast_radius` returns the
correct transitive workflow-and-test closure, verified against a
hand-computed expected set.
**Tests:** unit tests per query method; a graph-fixture correctness test.
**Security:** Evidence excerpts are secret-filtered before storage.
**Status:** DONE

### C4-02 · Deterministic integration extraction
**Purpose:** Derive everything the index can prove, without a model.
**Files:** `backend/integrations/extraction.py`
**Deps:** C3-04
**Implementation:** From the index alone, derive provider candidates, SDK nodes,
file/symbol/call-site nodes, webhook handlers, auth configuration, and test
associations.
**Acceptance:** Against the fixture repo, extraction equals a committed snapshot
— expected SDKs, call sites with correct line spans, and webhook handlers; every
node produced is marked `confirmed`; no model is invoked, asserted by a test that
fails if the model provider is touched.
**Tests:** snapshot tests against fixtures.
**Security:** Pure local computation.
**Status:** DONE

### C4-03 · Integration Mapper agent
**Purpose:** Add the judgment layer the deterministic pass cannot supply.
**Files:** `backend/agents/integration_mapper.py`
**Deps:** C4-02, C2-02
**Implementation:** Consumes deterministic extraction plus retrieved slices;
infers business workflows, reconciles provider identity, and outputs a validated
graph delta.
**Acceptance:** Every node or edge the agent contributes is marked `inferred`;
applying the delta cannot modify or delete a `confirmed` node, asserted by a
test that attempts it; every inferred workflow carries `Evidence`; the agent
holds no file-write tool.
**Tests:** contract tests; the confirmed-immutability test.
**Security:** Repository content reaches the agent only via C3-05 retrieval.
**Status:** DONE

### C4-04 · Baseline establishment
**Purpose:** Record what "normal" is, so a change can be detected against it.
**Files:** `backend/integrations/baseline.py`
**Deps:** C4-01, C4-03
**Implementation:** Record the current provider/version baseline per project and
transition to `MONITORING_ACTIVE`.
**Acceptance:** The baseline stores each detected provider's API version with
`Evidence`; a repository with no detected integrations reaches
`MONITORING_ACTIVE` with an empty, well-formed baseline rather than an error; the
end-to-end fixture run produces `Provider → files → functions → workflows →
tests → permissions`.
**Tests:** integration test producing that chain for the fixture repo, plus the
empty-repository case.
**Security:** Detected permissions and scopes are recorded, never requested.
**Status:** DONE

---

## PHASE 5 — Provider monitoring + Change Scout (50% → 60%)

### C5-01 · ProviderAdapter interface and registry
**Purpose:** Make provider support pluggable.
**Files:** `backend/providers/base.py`, `backend/providers/registry.py`
**Deps:** Phase 1
**Implementation:** The protocol and capability model from
`02_ARCHITECTURE.md` §9, with id-based registration and capability gating.
**Acceptance:** Calling a method whose capability is absent raises
`CapabilityNotSupported` rather than returning data; a test registers a **new**
adapter and drives it through monitoring end to end **without modifying any
module outside `backend/providers/`** — this is the measurable proof that
external providers can plug in later.
**Tests:** registry, capability-gating, and the new-adapter plug-in test.
**Security:** All adapter output is tagged as untrusted external content.
**Status:** PENDING

### C5-02 · Spec and baseline storage
**Purpose:** Keep provider documents attributable and deduplicated.
**Files:** `backend/providers/storage.py`
**Deps:** C5-01, C1-03
**Implementation:** Persist fetched specs, changelogs, and version metadata with
a `SourceRef` (`url`, `document_hash`, `retrieved_at`).
**Acceptance:** Documents are content-addressed — storing identical content twice
yields one row; every stored document has a non-null `SourceRef`; a malformed
document is rejected with a typed error and stored as unparsed rather than
silently coerced.
**Tests:** storage, dedup, and malformed-document tests.
**Security:** Stored external content is never executed nor interpolated into an
instruction region.
**Status:** PENDING

### C5-03 · Deterministic spec diff
**Purpose:** Compute the change set in code, not in a model.
**Files:** `backend/providers/diff.py`
**Deps:** C5-02
**Implementation:** Structural OpenAPI/JSON-schema diff producing
`ProviderChange` records for the **spec-derivable** subset of `ChangeType`
defined in `02_ARCHITECTURE.md` §10. The changelog-derived subset
(`rate_limit_changed`, `sdk_deprecated`, `api_version_deprecated`,
`documentation_only`) is explicitly out of scope here and belongs to C5-04.
**Acceptance:** Given two fixture specs, the diff produces exactly the expected
change set with correct `breaking`, `security_relevant`, and
`authentication_relevant` flags; there is one fixture per spec-derivable
`ChangeType`; the differ never emits a changelog-derived type, asserted by a
test; `endpoint_renamed` is emitted only above the documented similarity
threshold, with a fixture on each side of it.
**Tests:** table-driven tests, one per spec-derivable `ChangeType`, plus the
rename-threshold pair.
**Security:** Pure computation over untrusted input; malformed specs fail closed.
**Status:** PENDING

### C5-04 · Change Scout agent
**Purpose:** Interpret prose that no differ can read.
**Files:** `backend/agents/change_scout.py`
**Deps:** C5-03, C2-02
**Implementation:** Interprets prose changelogs and SDK registry metadata,
extracts the changelog-derived `ChangeType` members, reconciles them against the
deterministic diff, and classifies severity and security/authentication
relevance.
**Acceptance:** A `ProviderChange` without a `SourceRef` fails output validation;
the agent cannot introduce a change absent from both the spec diff and the
changelog, asserted by a fixture where it is prompted with an unsupported claim;
a fixture changelog containing an injection attempt is recorded as data, flagged
`prompt_injection_suspected`, and not obeyed.
**Tests:** the injection fixture, the unsourced-change rejection, and
reconciliation against C5-03 output.
**Security:** Exercises untrusted-content containment
(`03_SECURITY_ACCESS.md` §5).
**Status:** PENDING

### C5-05 · Monitoring jobs and deduplication
**Purpose:** Detect changes autonomously, on a schedule.
**Files:** `backend/workers/provider_monitor.py`
**Deps:** C5-01…C5-04
**Implementation:** Scheduled per-provider polling driven by adapter sources,
independent of any user action or demo trigger.
**Acceptance:** Polling the same provider version repeatedly produces exactly one
change event per distinct
`(provider_id, old_version, new_version, change_type, resource)`; a new version
transitions the project to `CHANGE_DETECTED`; the monitor runs from the scheduler
with no inbound request, asserted by an integration test that never calls the
API.
**Tests:** integration test with a fixture adapter serving two spec versions.
**Security:** Adapter fetches are bounded by `PROVIDER_FETCH_TIMEOUT_SECONDS` and
a response size cap.
**Status:** PENDING

---

## PHASE 6 — Execution safety, impact analysis, rehearsal (60% → 70%)

### C6-01 · ExecutionProvider
**Purpose:** The only way any command runs. Delivered here because C6-04
(rehearsal) is the first ticket that executes anything.
**Files:** `backend/shared/execution.py`
**Deps:** Phase 1
**Implementation:** `CommandSpec`/`CommandResult` and
`DevelopmentIsolatedExecutor` with argv-only execution, an executable allowlist,
cwd confinement, explicit environment construction, timeout, output caps,
cancellation, and audit logging.
**Acceptance:** The API accepts only `list[str]` argv — there is no code path
that passes a string to a shell, asserted by a test and by `shell=False` being
the only spawn form; a `cwd` outside the workspace root is rejected, including
via symlink; the child environment contains only allowlisted variables and
provably none of Continuity's AWS, GitHub, database, or session secrets; a
command exceeding its timeout is killed and reported as such; output beyond
`EXECUTION_MAX_OUTPUT_BYTES` is truncated with a marker; every invocation writes
an audit row.
**Tests:** `tests/security/test_execution.py` — escape, credential-leak, timeout,
truncation, and allowlist-violation cases.
**Security:** Repository code is untrusted; this is its containment.
**Status:** PENDING

### C6-02 · Change-to-graph correlation
**Purpose:** Deterministically connect a provider change to code.
**Files:** `backend/integrations/correlation.py`
**Deps:** C4-01, C5-03
**Implementation:** Match `ProviderChange.resource` against call sites, webhook
event handlers, and permission nodes.
**Acceptance:** Against a fixture graph and change set, correlation equals a
committed expected mapping; a change touching no call site correlates to an empty
set without error or warning; no model is invoked.
**Tests:** correlation tests over fixture graph + change sets.
**Security:** Pure computation.
**Status:** PENDING

### C6-03 · Impact Analyst agent
**Purpose:** Decide whether a change actually matters here.
**Files:** `backend/agents/impact_analyst.py`
**Deps:** C6-02, C2-02
**Implementation:** Judges relevance, severity, migration necessity, and
authentication/permission consequences from correlation output plus retrieved
slices.
**Acceptance:** On a fixture set where most changes are irrelevant, the agent
identifies exactly the expected relevant subset, the rest reach
`CHANGE_IRRELEVANT`, and **no migration run is created for them** — asserted by
counting `migration_runs` rows; every affected file, function, workflow, and test
carries `Evidence`; the agent holds no write tools.
**Tests:** the majority-irrelevant fixture, including the run-count assertion.
**Security:** Cannot modify code.
**Status:** PENDING

### C6-04 · Rehearsal harness
**Purpose:** Prove the incompatibility before touching user code.
**Files:** `backend/validation/rehearsal.py`
**Deps:** C6-01, C6-03, C5-02
**Implementation:** Adapter-based harness selecting affected tests via
`graph.tests_covering`, executing them against old and new contract simulations
through `ExecutionProvider`, and storing the delta as evidence.
**Acceptance:** Produces old-versus-new pass counts and affected workflows from
real execution output; when the provider exposes no usable spec the run reaches
`REHEARSAL_UNAVAILABLE` with a stored reason and proceeds to
`MIGRATION_PENDING`; when the rehearsal runs but reproduces no difference the run
reaches `REHEARSAL_FAILED` and escalates rather than migrating; no result is ever
model-generated.
**Tests:** fixture rehearsal producing a reproducible delta, plus the
unavailable and no-difference cases.
**Security:** Executes only through `ExecutionProvider`.
**Status:** PENDING

---

## PHASE 7 — Migration Engineer + repair loop (70% → 80%)

### C7-01 · Migration workspace
**Purpose:** Isolate every write.
**Files:** `backend/migrations/workspace.py`
**Deps:** C6-01, C3-02
**Implementation:** Isolated worktree/checkout at a pinned source commit,
recording source commit, target branch, files changed, commands, tests, attempts,
findings, and diff. Deterministic cleanup including an orphan sweep at startup.
**Acceptance:** The user's default branch and original working tree are
byte-identical after a migration, asserted by a recursive hash; workspaces are
removed on success, on failure, and by the startup sweep after a simulated crash;
a write targeting a path outside the workspace root is rejected.
**Tests:** lifecycle tests, the unchanged-source assertion, and the orphan sweep.
**Security:** Writes are confined to the workspace root.
**Status:** PENDING

### C7-02 · Migration Engineer agent
**Purpose:** Produce the patch.
**Files:** `backend/agents/migration_engineer.py`
**Deps:** C7-01, C6-03
**Implementation:** Produces a migration plan and a patch via bounded file tools;
adapts provider calls, schema handling, and webhook logic; preserves unrelated
code; produces a diff.
**Acceptance:** Only files inside the workspace are modified; **no file outside
the correlated impact set from C6-02 is modified** unless the agent records an
explicit justification for that file — this replaces any subjective notion of a
"minimal" diff and is checked by comparing the diff's file list against the
impact set; no test file is deleted, and any test modification is declared in the
attempt record; a patch that introduces a secret or a new dependency produces a
blocking finding (`new_dependency` → ASK).
**Tests:** fixture migration producing a correct patch; an out-of-impact-set
modification test; a secret-in-patch test.
**Security:** See `03_SECURITY_ACCESS.md` §9.
**Status:** PENDING

### C7-03 · Validator agent and test execution
**Purpose:** Produce test evidence that is real.
**Files:** `backend/agents/validator.py`, `backend/validation/`
**Deps:** C6-01, C7-01
**Implementation:** Deterministic test-command discovery from manifests and
config; execution via `ExecutionProvider`; machine-readable result parsing (JSON
report / JUnit XML); structured failure evidence.
**Acceptance:** Pass/fail counts and failing test ids are parsed from real
process output, and a test asserts that no code path can populate a
`test_results` row from a model response; discovery finds the correct command for
pytest, vitest, and jest fixtures; unparseable output produces a typed error
rather than a guessed count.
**Tests:** parser tests across pytest, vitest, and jest output fixtures.
**Security:** Output is size-capped and secret-filtered before storage.
**Status:** PENDING

### C7-04 · Autonomous repair loop
**Purpose:** The product's central behaviour.
**Files:** `backend/orchestration/repair.py`
**Deps:** C7-02, C7-03
**Implementation:** `PATCH → TEST → FAIL → DIAGNOSE → REPAIR → RETEST` with
`MAX_REPAIR_ATTEMPTS` enforced in application code; per-attempt records;
escalation to `HUMAN_REVIEW_REQUIRED` on exhaustion.
**Acceptance:** A fixture that fails on attempt 1 and passes on attempt 2 is
genuinely repaired, with both attempts recorded including failure evidence and a
diagnosis; a fixture that can never pass stops after exactly
`MAX_REPAIR_ATTEMPTS` attempts and reaches `HUMAN_REVIEW_REQUIRED`; the loop
cannot execute more attempts than the budget, asserted by counting
`migration_attempts` rows.
**Tests:** the failing-then-passing fixture and the never-passing fixture.
**Security:** Weakening or deleting a test to reach PASS triggers ASK, never
silent acceptance.
**Status:** PENDING

---

## PHASE 8 — Security + approval + GitHub PR (80% → 90%)

### C8-01 · Security Reviewer agent
**Purpose:** Structured review of the generated diff.
**Files:** `backend/agents/security_reviewer.py`
**Deps:** C7-02, C2-03
**Implementation:** Reviews the diff and changed dependencies for every
`FindingCategory`; returns structured findings with an advisory ALLOW/ASK/DENY
recommendation.
**Acceptance:** Each of the 13 `FindingCategory` members has a fixture diff that
produces the expected finding; every finding carries `Evidence`; the stored row
records both the agent's `recommendation` and the policy engine's
`policy_decision`, and a disagreement between them is persisted and surfaced.
**Tests:** one fixture diff per finding category, plus a disagreement case.
**Security:** The agent recommends; it never decides.
**Status:** PENDING

### C8-02 · Approval API and enforcement flow
**Purpose:** Complete the human-control surface begun in C2-08.
**Files:** `backend/security/policy.py`, `backend/approvals/api.py`
**Deps:** C2-03, C2-08, C1-05
**Implementation:** Authoritative ALLOW/ASK/DENY enforcement; approval HTTP API
writing `APPROVED`/`REJECTED` with actor and timestamp; run pause and resume; a
re-check of stored approval state immediately before the protected action runs.
**Acceptance:** A protected action attempted while its approval is `PENDING` is
refused; an approval revoked between grant and execution is caught by the
re-check; no path under `backend/agents/` can write an approval, asserted by an
import-graph test and by an explicit self-approval attempt; approval state
survives a restart; only the authenticated approver's user id is recorded.
**Tests:** `tests/security/test_approval_integrity.py` including the
self-approval attempt and the revoke-between-grant-and-execute race.
**Security:** This is the human-control guarantee.
**Status:** PENDING

### C8-03 · GitHub branch and PR delivery
**Purpose:** Deliver the migration the safe way.
**Files:** `backend/github/delivery.py`
**Deps:** C3-02, C8-02
**Implementation:** Branch creation under
`continuity/migrate-<provider>-<version>`, commit of the validated patch, and PR
creation with an evidence-backed description and changed-file summary.
**Acceptance:** Delivery is refused unless validation passed, security review
passed, and any required approval is `APPROVED` — each refusal tested
separately; branch names always match the documented pattern; a default-branch
target is rejected; every value in the PR body is read from a stored record,
asserted by a test that mutates a record and observes the body change.
**Tests:** mocked-API delivery tests plus the three refusal cases and the
default-branch rejection.
**Security:** Installation tokens are minted per operation and never logged.
**Status:** PENDING

### C8-04 · Migration evidence report
**Purpose:** Make the claim auditable.
**Files:** `backend/migrations/evidence.py`
**Deps:** C7-04, C8-01
**Implementation:** Structured report assembled entirely from stored records:
detected changes, breaking count, relevant count, affected workflows and files,
attempts, build and test results, security review, permission expansion,
approvals, PR, and status.
**Acceptance:** Every field traces to a named database column, listed in the
module docstring; a test mutating each source record changes the corresponding
report field; no field is derived from a model response.
**Tests:** report generation over a completed fixture run, plus the
field-provenance test.
**Security:** Report content is secret-filtered before rendering or posting.
**Status:** PENDING

### C8-05 · AgentCore integration
**Purpose:** Production infrastructure around the agent system.
**Files:** `backend/observability/agentcore.py`, deployment configuration
**Deps:** C2-05
**Implementation:** Wire Runtime, Observability, and Identity, and — only if it
adds enforcement beyond `policy.py` — Gateway/Policy, per
`02_ARCHITECTURE.md` §17.
**Acceptance:** Each integrated feature is demonstrated live against a real AWS
account; any feature that cannot be provisioned is recorded in `STATUS.md` with
its exact setup steps and is claimed nowhere in the UI, the README, or the
evidence report; no ARN, role name, or resource id is invented.
**Tests:** integration tests that skip with an explicit named reason when AWS is
unconfigured.
**Security:** No fabricated cloud resources.
**Status:** PENDING

### C8-06 · Merge detection
**Purpose:** Let `MERGE_WAITING` advance without assuming an outcome.
**Files:** `backend/github/webhooks.py`, `backend/workers/pr_status_poll.py`
**Deps:** C8-03
**Implementation:** GitHub App `pull_request` webhook receiver with HMAC-SHA256
signature verification using `GITHUB_APP_WEBHOOK_SECRET`, plus a
`pr_status_poll` fallback job, per `02_ARCHITECTURE.md` §15.
**Acceptance:** A merged PR transitions `MERGE_WAITING → VERIFIED` (no
verification environment) or `→ POST_MERGE_VERIFICATION_RUNNING`; a closed
unmerged PR returns the project to `MONITORING_ACTIVE`; an unsigned or
mis-signed delivery is rejected with 401 and audited; signature comparison is
constant-time; the payload is used only to look up Continuity's own PR record,
never as authoritative state; with neither webhook nor polling configured, the
run stays in `MERGE_WAITING` and the UI says so.
**Tests:** signature verification (valid, invalid, missing), merged and closed
paths, replayed delivery, and the polling fallback.
**Security:** A verified webhook is still untrusted input and can never trigger
an action that would otherwise require approval.
**Status:** PENDING

---

## PHASE 9 — Production security + frontend + polish (90% → 100%)

### C9-01 · Red-Team agent
**Purpose:** Attack the migration before a human sees it.
**Files:** `backend/agents/red_team.py`
**Deps:** C7-04, C8-01
**Implementation:** Adversarial validation of the proposed migration: malformed
and missing fields, unexpected fields, unexpected nulls, expired credentials,
invalid tokens, webhook replay and duplication, duplicate transactions,
timeouts, retry storms, rate limiting, malicious external text, prompt
injection, unauthorized tool requests, permission escalation, and invalid
signatures.
**Acceptance:** Each attack class has a fixture and produces a structured
finding; a finding at `high` or `critical` severity returns the run to
`REPAIR_RUNNING` rather than proceeding to delivery; the agent holds no
write tools; it runs after validation passes and before PR delivery.
**Tests:** one fixture per attack class, plus the return-to-repair path.
**Security:** Runs entirely against the isolated workspace.
**Status:** PENDING

### C9-02 · Release Guardian
**Purpose:** Verify the migration in a real environment after merge.
**Files:** `backend/agents/release_guardian.py`,
`backend/workers/post_merge_verify.py`
**Deps:** C8-06
**Implementation:** Post-merge verification abstraction — synthetic integration
checks against a configured environment, baseline comparison, regression
evidence, `VERIFIED` marking, and rollback *recommendation*.
**Acceptance:** With a configured environment, a healthy migration reaches
`POST_MERGE_VERIFICATION_PASSED → VERIFIED` and a regressed one reaches
`POST_MERGE_VERIFICATION_FAILED → HUMAN_REVIEW_REQUIRED`; with no environment
configured the run reaches `VERIFIED` recording `verification: not_configured`
and claims no verification anywhere; **no code path performs a rollback** —
asserted by a test over the module's public surface.
**Tests:** healthy, regressed, and unconfigured paths; the no-rollback surface
test.
**Security:** A destructive rollback is never automatic.
**Status:** PENDING

### C9-03 · Confidential execution
**Purpose:** Honest handling of the TEE story.
**Files:** `backend/shared/execution.py`,
`backend/security/attestation.py`
**Deps:** C6-01
**Implementation:** Complete `ConfidentialExecutionProvider` and the secure
credential boundary. Investigate the real Nitro Enclave + KMS attestation path.
**Acceptance:** The abstraction exists and `DevelopmentIsolatedExecutor` remains
fully functional; the security page reports the **active provider class name**,
and reports `TEE Attestation: Not Configured` unless a verified
`AttestationDocument` exists; a test asserts that no code path can report an
attested state without a verified document; if enclave infrastructure is
unavailable, the exact blocker and setup steps are recorded in `STATUS.md` and no
attestation claim appears anywhere.
**Tests:** provider-selection tests; the cannot-fake-attestation test.
**Security:** `03_SECURITY_ACCESS.md` §7 honesty rule is enforced by test, not by
convention.
**Status:** PENDING

### C9-04 · Full frontend
**Purpose:** Ship the product surface.
**Files:** `apps/web/`
**Deps:** C8-04, C8-06
**Implementation:** Every screen in `04_FRONTEND_SPEC.md` §3: sign-in, GitHub
connection, repository import, scan progress, projects dashboard, project
overview, integration health, Integration Intelligence Graph, provider and change
events, agent-run visualization, migration timeline, validation status, approval
cards, security page, migration report, PR history, settings, and Ask Continuity.
**Acceptance:** Every screen in §3 exists and renders from live API data; no
component contains a hardcoded provider, change, workflow, test count, or status;
scan-progress steps appear only on their activity event; the security page rows
each read the backend source named in `03_SECURITY_ACCESS.md` §11; inferred data
carries its visible marker; a health score is absent when its inputs are absent;
approval buttons write backend state and the run stays paused until it changes.
**Tests:** Vitest component tests per screen; a repository-wide test asserting no
mock fixture is imported by production code; Playwright E2E for sign-in →
import → scan → change → approval → PR.
**Security:** No secret reaches the browser; chat cannot bypass workflow state.
**Status:** PENDING

### C9-05 · Evaluation harness
**Purpose:** Measure the product deterministically.
**Files:** `backend/evaluation/`, `tests/fixtures/labelled/`
**Deps:** C8-04
**Implementation:** Implement the metrics in `02_ARCHITECTURE.md` §18 against
labelled fixtures. Optionally wire AgentCore Evaluations.
**Acceptance:** Every §18 metric is computed from stored records over a labelled
fixture set and reported by a single command; the Integration Health formula is
published alongside any displayed score; no metric is a model's opinion.
**Tests:** the harness runs in CI over the labelled fixtures.
**Security:** Fixtures contain no real credentials.
**Status:** PENDING

### C9-06 · Final quality gate
**Purpose:** The last review before calling it done.
**Files:** `README.md`, all
**Deps:** C9-01…C9-05
**Implementation:** Responsive and accessible UI; polished loading, empty, and
error states; README with architecture diagram and setup instructions; final
security review; full test suite.
**Acceptance:** All of the following pass — backend, integration, frontend, and
critical E2E tests; typecheck; lint; build. Plus: Strands workflow verified;
repository analysis verified; provider monitoring verified; impact analysis
verified; migration repair loop verified; approval enforcement verified; GitHub
PR flow verified; no committed secrets; every anchor document current; every
external blocker explicitly documented in `STATUS.md`; the README setup
instructions succeed on a clean checkout.
**Tests:** the complete suite, run by `reviewer-tester` at the final gate.
**Security:** Full `tests/security/` suite green; final review against
`03_SECURITY_ACCESS.md` end to end.
**Status:** PENDING
