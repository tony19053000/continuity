# 02 — Technical Architecture

**Status:** Phase 0 baseline. Living state — update whenever implementation
materially changes architecture.

Verified against upstream documentation on 2026-09-10. Facts marked **[V]** were
confirmed from official sources at that date and must be re-verified if they
appear stale.

---

## 1. Verified upstream facts

| Fact | Value | Source |
| --- | --- | --- |
| Strands Python package **[V]** | `strands-agents` 1.55.1, Apache-2.0, Python ≥3.10 (supports 3.10–3.14) | https://pypi.org/project/strands-agents/ |
| Strands tools package **[V]** | `strands-agents-tools` | https://strandsagents.com/docs/user-guide/quickstart/python/ |
| Agent import **[V]** | `from strands import Agent, tool` | https://strandsagents.com/docs/user-guide/quickstart/python/ |
| Gemini provider import **[V]** | `from strands.models.gemini import GeminiModel` (extra: `strands-agents[gemini]`) | verified against the installed SDK |
| Gemini model id **[V]** | `model_id` is required — no SDK default. Continuity defaults to `gemini-2.5-flash`, confirmed present in the live `models.list()` | https://ai.google.dev/gemini-api/docs/models |
| Bedrock provider import **[V]** | `from strands.models import BedrockModel` | https://strandsagents.com/docs/user-guide/concepts/model-providers/amazon-bedrock/ |
| Bedrock default model **[V]** | `global.anthropic.claude-sonnet-4-6` | https://strandsagents.com/docs/user-guide/concepts/model-providers/amazon-bedrock/ |
| BedrockModel params **[V]** | `model_id`, `region_name`, `temperature`, `boto_session`, `guardrail_id`, `guardrail_version`, `cache_config` | https://strandsagents.com/docs/user-guide/concepts/model-providers/amazon-bedrock/ |
| Region resolution order **[V]** | explicit `region_name` → boto3 session → `AWS_REGION` → default `us-west-2` | https://strandsagents.com/docs/user-guide/concepts/model-providers/amazon-bedrock/ |
| Structured output **[V]** | `agent(prompt, structured_output_model=Model)` → `result.structured_output`; async `agent.invoke_async(...)` | https://strandsagents.com/docs/user-guide/concepts/agents/structured-output/ |
| Async streaming **[V]** | `agent.stream_async(prompt)` yields events | https://strandsagents.com/docs/user-guide/quickstart/python/ |
| AgentCore SDK **[V]** | `bedrock-agentcore` 1.22.0, Apache-2.0, Python ≥3.10 | https://pypi.org/project/bedrock-agentcore/ |
| AgentCore GA **[V]** | Generally available since Oct 2025; Runtime, Memory, Gateway, Identity, Observability | https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-agentcore-available/ |

Local toolchain confirmed on this machine: Python 3.12.3, Node 22.22.1,
npm 10.9.4, git 2.43.0, `gh` 2.45.0 authenticated as `tony19053000`.

Installed in Phase 1: FastAPI 0.141.1, SQLAlchemy 2.0.52, Next.js 16.3.4,
React 19.2.8, Tailwind 4, Vitest 5.0.0. Added in Phase 2: `strands-agents`.

Verified against the installed SDK rather than the docs alone:
`Agent(model=..., system_prompt=..., tools=[...])`,
`await agent.invoke_async(prompt, structured_output_model=Model)` →
`result.structured_output`, `GeminiModel(client_args={"api_key": ...},
model_id=..., params={...})`, and `BedrockModel(model_id=..., region_name=...,
temperature=...)`.

**The primary model is Google Gemini, not Amazon Bedrock.** Strands remains the
agent framework; only the model behind it changed. That change touched one class
and one config group — no agent, contract, prompt, tool, or orchestration test
needed editing, which is the clearest evidence the provider abstraction was
worth having.

Amazon Bedrock AgentCore remains the production agent-infrastructure target
(§17). AgentCore is where agents *run*; it is independent of which model they
call, so switching the model does not affect that plan.

**Environment management uses `uv`, not `venv`.** This machine's Python has no
`ensurepip`, so `python -m venv` fails and the system interpreter is
PEP 668-managed. `uv` creates the environment without either constraint and is
what CI uses, so local and CI environments resolve identically.

Two dependency pins are deliberate rather than incidental:

- **Vitest 5.** Versions 2.1.0–4.1.10 carry a path-traversal advisory in
  `@vitest/mocker` (GHSA-82fw-gwwq-j7x9). Vitest 4 additionally crashed npm
  10.9.4's peer resolver.
- **`@types/node` ^22.** Vitest 5 requires `^22 || >=24`, and 22 matches the
  Node runtime in use.

**External integrations are configured and verified live** (2026-09-11):
Google Gemini (C2-07 proves a real Strands → Gemini → tool call → tool result →
response loop), AWS via the `continuity-dev` profile in `us-west-2` (standard
credential chain; no AWS key is ever stored in Continuity's config), the GitHub
App `Continuity Integration Agent` (installation discovery, installation token,
and a real repository read), and Google OAuth (live authorization redirect with
a matching `redirect_uri`). See `STATUS.md` for what remains open.

---

## 2. Stack decisions

| Layer | Choice | Justification |
| --- | --- | --- |
| Backend language | Python 3.12 | Matches local toolchain, inside Strands' supported 3.10–3.14 range, and gives first-class AST analysis of Python target repositories via the stdlib `ast` module. |
| Backend API | FastAPI + Uvicorn | Async-native (agent runs are I/O-bound), Pydantic-native (every agent contract is already a Pydantic model), and OpenAPI generation gives the frontend a typed client for free. |
| Agent framework | Strands Agents SDK | Mandatory for the hackathon and genuinely the right shape: model-driven agents with typed tools and Pydantic structured output. |
| Model provider (primary) | **Google Gemini** via `strands.models.gemini.GeminiModel` | The active model behind Strands. Wrapped in Continuity's own provider abstraction. |
| Model provider (optional, future) | Amazon Bedrock via `strands.models.BedrockModel` | Retained because the abstraction is already clean and AgentCore remains the production agent-infrastructure target — not because it is in use. |
| AWS production agent infrastructure | Amazon Bedrock AgentCore | Runtime, Observability, Identity (Phase 8). Independent of which model Strands drives. |
| Validation | Pydantic v2 | Every agent output, provider change, and API body is a validated model. |
| Persistence | SQLite (dev) / PostgreSQL (prod) through SQLAlchemy 2.x + Alembic | Relational is the right fit: the Integration Intelligence Graph is a modest edge set best served by indexed joins, and migration runs need transactional state transitions. A graph database is unjustified complexity at this size. |
| Background execution | In-process async job runner behind a `JobQueue` interface | Simple and testable now; the interface allows an SQS/Step Functions/AgentCore Runtime backend later without touching callers. |
| Frontend | Next.js (App Router) + React + TypeScript + Tailwind CSS | Required stack; App Router for server components on read-heavy dashboard pages. |
| Frontend tests | Vitest + React Testing Library; Playwright for critical E2E | Matches the stack without overbuilding. |
| Backend tests | pytest + pytest-asyncio + httpx ASGI transport | Async-first, no live server needed for integration tests. |
| Lint / types | `ruff` + `mypy` (backend); `eslint` + `tsc` (frontend) | Already the required commands. |

### Rejected dependencies

LangChain, CrewAI, and AutoGen are **not** used. Strands is the primary agent
framework; introducing a second orchestration layer would duplicate state
management and obscure where deterministic control actually lives. Any future
change here requires an explicit architecture amendment.

---

## 3. Repository layout

```
/
├── .claude/agents/{coder,reviewer-tester}.md
├── apps/web/                     # Next.js frontend
├── backend/
│   ├── api/                      # FastAPI routers, dependencies, schemas
│   ├── agents/                   # Strands runtime agents (one module each)
│   ├── orchestration/            # State machine, run coordinator, job queue
│   ├── models/                   # SQLAlchemy models + Pydantic domain schemas
│   ├── providers/                # ProviderAdapter interface + adapters
│   ├── repository/               # Ingestion, indexing, context retrieval
│   ├── integrations/             # Integration Intelligence Graph build/query
│   ├── migrations/               # Migration workspace, patching, repair loop
│   ├── validation/               # Test discovery, execution, result parsing
│   ├── security/                 # Secret filter, policy engine, reviewers
│   ├── approvals/                # Approval state machine + API surface
│   ├── github/                   # GitHub App auth, branch/PR operations
│   ├── workers/                  # Job handlers (monitor, scan, migrate)
│   ├── observability/            # Structured activity events, tracing
│   └── shared/                   # Config, errors, execution provider, types
├── tests/{unit,integration,security,fixtures}/
├── scripts/
├── 01_PRD.md … 05_FEATURE_TICKETS.md
├── STATUS.md  CLAUDE.md  README.md  LICENSE
└── .gitignore  .env.example
```

### Layering rule

```
api → orchestration → agents → { providers, repository, integrations,
                                 migrations, validation, security }
                    ↘ models ↙
```

Dependencies point downward only. Specifically:

- `agents/` never performs its own persistence — it returns structured output.
- `security/` policy enforcement is never imported *into* an agent prompt.
- `repository/` never imports from `api/`.
- `shared/` imports nothing from Continuity above it.

---

## 4. Repository understanding pipeline

**Principle: never send an entire repository blindly to the model.**

```
Authorized repository
   ↓ boundary enforcement (path confinement to the checkout root)
   ↓ exclusion filter (.git, venvs, node_modules, .next, dist, build, binaries,
   ↓                   generated artifacts, oversized files)
   ↓ secret filter (runs BEFORE any model context is constructed)
   ↓ deterministic index (file classification, manifests, AST, import graph)
   ↓ integration extraction (SDK imports, HTTP clients, webhooks, auth config)
   ↓ relevant-context retrieval (bounded slices, not whole files where possible)
   ↓ Strands agent / Bedrock model
```

### Implementation notes (Phase 3)

- **`RepositorySource` is the only way code is read**, and it enforces the
  boundary rather than trusting callers. Paths are normalized *before*
  resolution (absolute, `..`, `~`, drive-qualified and null-byte paths are
  rejected outright), then resolved and proven to still sit inside the root —
  which is what catches a symlink whose name is innocent but whose target is
  not. Percent-encoded traversal (`..%2f..`) is deliberately treated as a
  literal filename: decoding it would create the vulnerability it resembles,
  since the filesystem never decodes it either.
- **Sources record what they pruned.** The walk skips excluded directories for
  speed, so without `skipped_paths()` the scan summary would report zero
  secrets excluded — understating a control that is working.
- **TS/JS analysis is regex-based and flagged as such.** Every result carries
  `heuristic=True` so nothing downstream treats it as equivalent to the Python
  AST. A real parse needs the TypeScript compiler, i.e. a Node subprocess per
  file; that trade is recorded rather than hidden.
- **Webhook detection requires three signals together** — signature
  verification, event dispatch, and either a route decorator or a hook-like
  name. An earlier two-signal version matching `ast.dump()` substrings flagged
  five handlers in Continuity's own backend, including the detector itself,
  because "signature" appears in `BadSignature` and "type" inside
  `account_type`. Names are now matched whole against real identifiers.
- **Oversized files are skipped by `stat`**, never read to discover they are
  too large.
- **The repository's own `.gitignore` contributes secret rules.** A team that
  ignores `deploy/live-config` is telling us that file holds credentials, and no
  static pattern list would have guessed it. Only *secret-looking* rules are
  adopted — taking the whole file would exclude `dist/` as a "secret" and make
  the count meaningless — and negations (`!`) are never adopted, since they
  re-include rather than hide. Trigger words are matched as **whole tokens**:
  a substring version adopted `monkey/` ("key") and `designtokens/` ("token"),
  excluding ordinary source directories from analysis entirely. That is the
  damaging direction — a wrongly-excluded file is invisible to the product,
  where a wrongly-included secret is still redacted downstream. Tokenization
  handles all three naming styles (separators, underscores, and camelCase), and
  is Unicode-aware: an ASCII-only split tore `envío/` into `{env, o}` and
  adopted it. One residual ambiguity is accepted and documented rather than
  hidden — `design-tokens/` is adopted, because it is lexically indistinguishable
  from `auth-tokens/`.
- **Two `RepositorySource` implementations, one conformance suite.**
  `LocalRepositoryAdapter` (development) and `GitHubRepositorySource` are tested
  by the same parametrized suite, because a boundary rule that holds locally and
  lapses over GitHub would be invisible until it mattered. The GitHub source
  loads eagerly — the protocol is synchronous and the API is not — fetching the
  tree and the contents the indexer would read anyway, with excluded paths
  filtered *before* any content request so a `.env` is never transferred.

### Deterministic index

Built with code, not a model:

- **File classification** — source / test / config / manifest / doc / generated
- **Dependency manifests** — `requirements*.txt`, `pyproject.toml`,
  `poetry.lock`, `package.json`, `package-lock.json`, `yarn.lock`
- **Python AST** (stdlib `ast`) — imports, function and class definitions with
  line spans, decorators, call sites, string literals that look like URLs or
  API paths
- **TS/JS** — import graph and call sites
- **HTTP client detection** — `requests`, `httpx`, `aiohttp`, `urllib`, `fetch`,
  `axios`
- **Webhook detection** — route handlers whose bodies verify signatures or
  switch on an event-type field
- **Framework detection** — FastAPI, Flask, Django, Express, Next.js

Only after this does anything reach a model, and then only the retrieved slices.

**Benefits:** privacy, lower cost, lower token usage, better context quality,
predictable scaling, and a security review surface that is actual code rather
than prompt text.

---

## 5. Integration Intelligence Graph

Continuity must understand more than "the `stripe` package is installed". The
graph is **real persisted structured data** consumed by the Impact Analyst — not
decorative frontend text.

```
Provider → SDK/API → Files → Functions/Services → Business Workflows
                                                → Tests
                                                → Permissions/Auth
```

Example shape:

```
PaymentProvider
    ├── payment_service.py
    │       ├── create_payment()      → Checkout
    │       └── renew_subscription()  → Subscription Renewal
    └── webhook_handler.py            → Payment Events
```

### Node types

| Node | Key fields |
| --- | --- |
| `ProviderNode` | `provider_id`, display name, detected SDK package, detected API version, base URL, auth mechanism, confidence |
| `SdkNode` | package name, version constraint, manifest file, manifest line |
| `FileNode` | repo-relative path, language, classification, content hash |
| `SymbolNode` | qualified name, kind (function/method/class/route), file, line span |
| `CallSiteNode` | file, line, resolved provider operation, HTTP method + path or SDK method |
| `WorkflowNode` | workflow name, inference basis, confidence |
| `TestNode` | test file, test id, selector command |
| `PermissionNode` | scope/permission string, where declared, provider |

### Edge types

`DECLARES_SDK`, `DEFINED_IN`, `CALLS_PROVIDER`, `IMPLEMENTS_WORKFLOW`,
`COVERED_BY_TEST`, `REQUIRES_PERMISSION`, `HANDLES_WEBHOOK_EVENT`.

### Evidence requirement

Every node and edge carries an `evidence` record:

```python
class SourceRef(BaseModel):
    """Where a piece of external information came from."""
    kind: Literal["openapi_spec", "changelog", "docs", "github_release",
                  "sdk_registry", "version_endpoint", "api_feed"]
    url: str | None
    document_hash: str | None    # content hash of the stored document
    retrieved_at: datetime | None

class Evidence(BaseModel):
    kind: Literal["source", "manifest", "provider_spec", "changelog", "test_output"]
    file_path: str | None        # repo-relative, for repository evidence
    line_start: int | None
    line_end: int | None
    source_ref: SourceRef | None # set for external (provider) evidence
    excerpt: str | None          # bounded length, secret-filtered
    confidence: Literal["confirmed", "inferred"]
```

`Evidence` answers *what supports this claim*. `SourceRef` answers *where that
came from and when we fetched it*, and is the single place external provenance
lives — every provider document stored under §9 carries one, and
`ProviderChange.source` (§10) reuses it rather than defining a parallel model.

`confirmed` means derived deterministically (AST, manifest, spec diff).
`inferred` means a model proposed it. The distinction is preserved end to end
and surfaced in the UI. **Inferred data may never be presented as confirmed.**

### Implementation notes (Phase 4)

- **`blast_radius` traverses `COVERED_BY_TEST` forwards.** The extractor writes
  that edge SYMBOL → TEST, so finding the tests covering a symbol is a forward
  walk. An earlier version listed it as reverse and silently returned *no tests*
  for every blast radius — the query looked like it worked while quietly
  answering "nothing covers this", which would have made every migration skip
  the tests that matter most.
- **Baseline seeds from call sites *and* webhook handlers.** A handler reaches
  its provider through `HANDLES_WEBHOOK_EVENT`, not through a call site, so
  seeding from call sites alone omits every workflow that only receives events —
  exactly the case a renamed webhook event breaks.
- **Webhook event names come from string literals, not call arguments.**
  `event["type"] == "payment.paid"` puts the name in a comparison, so the Python
  analyzer records short string constants with their line and enclosing symbol.
- **Agent contracts avoid `dict[str, str]` and deeply-nested optionals.**
  Gemini's structured output is an OpenAPI subset with no free-form object keys;
  asking for a dict made the model fail to produce output at all. Evidence is
  requested as flat fields and the `Evidence` object is assembled in code, which
  is better anyway — the agent has no business setting `confidence`.
- **`callback_handler=None` on every Strands agent.** The default handler prints
  streaming model output, including reasoning, to stdout — which would violate
  §16 on every single agent call.

### Storage

Relational: `graph_nodes` and `graph_edges` tables scoped by
`(project_id, graph_version)`. Each repository scan writes a new immutable
`graph_version`; queries read the latest. This gives free history and makes
"what changed in our integration surface" answerable.

Correlation between a provider change and this graph is deterministic
(`backend/integrations/correlation.py`, C6-02): endpoint paths are compared
segment-wise so a template matches a concrete value and a different arity does
not, webhook changes reach only the handlers for that event, and an enum change
reaches only call sites passing a value the enum no longer accepts. A field
change on a *renamed* endpoint is keyed to the new path while the repository
still calls the old one, so the change set's rename map is applied when
matching — without it, exactly the changes that break callers hardest would
correlate to nothing.

Query methods the Impact Analyst depends on:

```python
graph.providers(project_id)
graph.call_sites_for_provider(project_id, provider_id)
graph.workflows_touching(node_ids)
graph.tests_covering(node_ids)
graph.permissions_for_provider(project_id, provider_id)
graph.blast_radius(call_site_ids)   # transitive workflow + test closure
```

---

## 6. Strands agent implementation

Strands is the framework, not the model. Bedrock supplies the model.

```
Amazon Bedrock model
      ↓
  Strands Agent
      ↓
specialized system prompt + allowed tools + structured output contract
      ↓
validated Pydantic output
      ↓
deterministic state transition (application code)
      ↓
next agent / tool
```

Agents do **not** chat freely with each other. Every hand-off is:

```
Agent → validated structured output → deterministic transition → next step
```

### Model provider abstraction

`backend/shared/model_provider.py`:

```python
class ModelProvider(Protocol):
    def build_model(self, role: AgentRole) -> Model: ...

class GeminiModelProvider:
    """Primary provider. Wraps strands.models.gemini.GeminiModel."""
    def build_model(self, role: AgentRole) -> Model:
        return GeminiModel(
            client_args={"api_key": ...},             # env: GEMINI_API_KEY
            model_id=...,                             # env: GEMINI_MODEL
            params={"temperature": ROLE_TEMPERATURE[role]},
        )

class BedrockModelProvider:
    """Optional future provider. Wraps strands.models.BedrockModel."""
```

`build_model_provider` selects Gemini when configured and falls back to Bedrock
only if Gemini is absent *and* Bedrock is present — so a fallback is explicit
rather than accidental. If neither is configured it raises naming both, because
a silent stand-in would produce output that looks like agent reasoning but is
not.

Rules:

- No call site constructs `GeminiModel` or `BedrockModel` directly.
- `GEMINI_MODEL` defaults to `gemini-2.5-flash` and is overridable by env. The
  application is never hardwired to one model id. Strands' Gemini provider has
  no default `model_id` of its own, so Continuity supplies one that was checked
  against the live model list rather than assumed.
- Model availability is verified at implementation time, not assumed.
- If Bedrock is unreachable, the abstraction stays; a clearly-named
  development adapter may be used for deterministic tests only, and must never
  be described as Bedrock. See §19.

### Agent contract

Every runtime agent declares:

| Property | Meaning |
| --- | --- |
| `role` | enum identifying the agent |
| `system_prompt` | specialized, versioned, stored in code |
| `allowed_tools` | explicit allowlist; empty is valid |
| `input_model` | Pydantic model |
| `output_model` | Pydantic model passed as `structured_output_model` |
| `max_attempts` | finite retry budget on malformed output |
| `on_error` | escalate / fail run / mark degraded |

Malformed structured output is rejected and retried up to `max_attempts`; after
that the run escalates. It is never coerced or best-effort parsed.

---

## 7. Runtime agents

These are Continuity's product agents — distinct from the Claude Code
development subagents in `.claude/agents/`.

Seven core agents ship first. They share **one** Bedrock model provider with
per-role prompts, tools, and output contracts; separate models are not needed
and would be cost without benefit. Per-role model routing may be added later
only with justification.

| # | Agent | Purpose | Writes code? |
| --- | --- | --- | --- |
| 1 | **Orchestrator** | Coordinates the run, invokes agents, enforces order, retry budgets, and approval pauses | No |
| 2 | **Change Scout** | Detects and normalizes external provider changes with source evidence | No |
| 3 | **Integration Mapper** | Builds the Integration Intelligence Graph from the deterministic index | No |
| 4 | **Impact Analyst** | Decides whether a change actually affects this project; traces blast radius | No |

The Impact Analyst judges **relevance, severity, and migration necessity**. It
does not decide *what* is affected: that comes from the graph via C6-02
correlation, so every affected file, symbol, workflow, and test carries the
CONFIRMED evidence the extractor recorded. The agent's own list of affected
files is kept and validated against the graph, and a path it named that the
graph does not contain is dropped and counted rather than shown to a reviewer.

`ImpactAnalystOutput` therefore carries no `Evidence` field. An `Evidence`
object assembled by a model is an assertion about a file; one taken from a graph
node is a record of having read it, and only the second belongs in a report.

A change that correlates to no call site, webhook handler, or declared
permission never reaches the model at all — there is no judgment to make about
a change that touches no line of the repository, and asking would invite the
model to find impact that is not there. On a typical release that is most of
the change set.

Judgment lives in `backend/agents/impact_analyst.py`; opening a migration run
and moving the project live in `backend/orchestration/impact.py`, because only
the coordinator moves runs and no module under `backend/agents/` may import the
state machine.
| 5 | **Migration Engineer** | Plans and produces the patch; diagnoses validation failures and repairs | Yes — isolated workspace only |
| 6 | **Validator / Tester** | Runs build and tests, parses failures, produces deterministic evidence | No |
| 7 | **Security Reviewer** | Reviews the diff; recommends ALLOW/ASK/DENY with structured findings | No |

Deferred until the core seven work:

| # | Agent | Purpose |
| --- | --- | --- |
| 8 | **Red-Team Agent** | Attacks the proposed migration (malformed payloads, replayed webhooks, duplicate transactions, expired credentials, retry storms, prompt injection, invalid signatures) |
| 9 | **Release Guardian** | Post-merge verification against a real environment; recommends — never performs — rollback |

### Critical constraints

- The **Orchestrator does not override security policy.** State transition
  enforcement lives in deterministic application code
  (`backend/orchestration/state_machine.py`), not in the Orchestrator's prompt.
- The **Security Reviewer recommends**; `backend/security/policy.py` decides.
- The **Change Scout must not invent provider changes.** Every reported change
  carries a source reference, and that reference is bound from the document
  Continuity fetched rather than supplied by the model. The model instead
  returns `evidence_quote`, a sentence that must appear in that document; a
  change whose quote is not found there is discarded before it is recorded.
  Deterministic spec diffing produces the change set; the model supplies
  semantic interpretation only.

  This is an amendment. The agent originally returned its own `SourceRef`, on
  the reasoning that a change it could not attribute was one it may have
  invented. Live Gemini filled that field with plausible placeholders
  (`url="changelog"`, `document_hash="acmepay-v2-changelog"`), which satisfied
  the attribution check while attributing nothing, and on other runs returned an
  empty URL and had true findings discarded. Asking a model to supply provenance
  it has no way to know produces exactly that: a control that admits invented
  values and rejects real ones at random. A quote is checkable, because
  Continuity holds the document.
- Only the **Migration Engineer** writes files, and only inside the migration
  workspace.

### Division of labour: deterministic code vs. model

| Deterministic code | Model |
| --- | --- |
| OpenAPI/spec structural diff | Semantic meaning of a changelog entry |
| AST parsing, import graph, call-site extraction | Inferring business-workflow names |
| Secret filtering | Explaining why a change matters |
| Test execution and result parsing | Diagnosing *why* a test failed |
| Policy ALLOW/ASK/DENY enforcement | Recommending a risk classification |
| State transitions, retry budgets | Producing the migration patch |
| Version comparison, deduplication | Judging relevance of a change to code |

If ordinary code can compute it reliably, ordinary code computes it.

---

## 8. Orchestration state machine

Deterministic, persisted, and the single source of truth for run progress. Model
output never overrides a transition.

This is the authoritative edge list. `ALLOWED_TRANSITIONS` in code must match it
exactly, edge for edge — the list, not the prose, is the specification.

**Project lifecycle**

| From | To |
| --- | --- |
| `PROJECT_CREATED` | `GITHUB_CONNECTED` |
| `GITHUB_CONNECTED` | `REPOSITORY_SELECTED` |
| `REPOSITORY_SELECTED` | `INITIAL_SCAN_PENDING` |
| `INITIAL_SCAN_PENDING` | `INITIAL_SCAN_RUNNING` |
| `INITIAL_SCAN_RUNNING` | `INITIAL_SCAN_COMPLETE`, `RUN_FAILED` |
| `INITIAL_SCAN_COMPLETE` | `INTEGRATION_MAPPING_RUNNING` |
| `INTEGRATION_MAPPING_RUNNING` | `INTEGRATION_MAPPING_COMPLETE`, `RUN_FAILED` |
| `INTEGRATION_MAPPING_COMPLETE` | `MONITORING_ACTIVE` |
| `MONITORING_ACTIVE` | `CHANGE_DETECTED`, `INITIAL_SCAN_PENDING` (re-scan) |

**Change evaluation**

| From | To |
| --- | --- |
| `CHANGE_DETECTED` | `CHANGE_ANALYSIS_RUNNING` |
| `CHANGE_ANALYSIS_RUNNING` | `CHANGE_ANALYSIS_COMPLETE`, `RUN_FAILED` |
| `CHANGE_ANALYSIS_COMPLETE` | `IMPACT_ANALYSIS_RUNNING` |
| `IMPACT_ANALYSIS_RUNNING` | `CHANGE_IRRELEVANT`, `CHANGE_RELEVANT`, `RUN_FAILED` |
| `CHANGE_IRRELEVANT` | `MONITORING_ACTIVE` |
| `CHANGE_RELEVANT` | `REHEARSAL_PENDING` |

**Rehearsal**

| From | To |
| --- | --- |
| `REHEARSAL_PENDING` | `REHEARSAL_RUNNING`, `REHEARSAL_UNAVAILABLE` |
| `REHEARSAL_RUNNING` | `REHEARSAL_CONFIRMED`, `REHEARSAL_FAILED`, `REHEARSAL_UNAVAILABLE` |
| `REHEARSAL_CONFIRMED` | `MIGRATION_PENDING` |
| `REHEARSAL_UNAVAILABLE` | `MIGRATION_PENDING` |
| `REHEARSAL_FAILED` | `HUMAN_REVIEW_REQUIRED`, `MONITORING_ACTIVE` |

`REHEARSAL_UNAVAILABLE` means the provider exposes no usable spec, so the
incompatibility could not be reproduced. The run continues on impact analysis
alone, and the missing rehearsal is recorded with a reason. `REHEARSAL_FAILED`
is different: the rehearsal ran and did *not* reproduce the expected
incompatibility, which undermines the case for migrating at all — so it
escalates rather than proceeding.

**Migration and validation**

| From | To |
| --- | --- |
| `MIGRATION_PENDING` | `MIGRATION_RUNNING` |
| `MIGRATION_RUNNING` | `PATCH_READY`, `HUMAN_REVIEW_REQUIRED`, `RUN_FAILED` |
| `PATCH_READY` | `VALIDATION_RUNNING` |
| `VALIDATION_RUNNING` | `VALIDATION_PASSED`, `VALIDATION_FAILED`, `RUN_FAILED` |
| `VALIDATION_FAILED` | `REPAIR_RUNNING`, `HUMAN_REVIEW_REQUIRED` |
| `REPAIR_RUNNING` | `VALIDATION_RUNNING`, `HUMAN_REVIEW_REQUIRED` |
| `VALIDATION_PASSED` | `SECURITY_REVIEW_RUNNING` |

`VALIDATION_FAILED → REPAIR_RUNNING` is taken while attempts remain;
`VALIDATION_FAILED → HUMAN_REVIEW_REQUIRED` when `MAX_REPAIR_ATTEMPTS` is
exhausted. The branch is decided by application code, not by a model.

**Security and approval**

| From | To |
| --- | --- |
| `SECURITY_REVIEW_RUNNING` | `SECURITY_REVIEW_PASSED`, `SECURITY_REVIEW_FAILED`, `APPROVAL_PENDING` |
| `SECURITY_REVIEW_FAILED` | `REPAIR_RUNNING`, `HUMAN_REVIEW_REQUIRED` |
| `SECURITY_REVIEW_PASSED` | `FINAL_VALIDATION_RUNNING`, `APPROVAL_PENDING` |
| `APPROVAL_PENDING` | `APPROVED`, `REJECTED` |
| `APPROVED` | `FINAL_VALIDATION_RUNNING` |
| `REJECTED` | `MONITORING_ACTIVE` |

`APPROVAL_PENDING` is entered whenever the policy engine returns ASK — which may
happen during security review or at any earlier ALLOW/ASK/DENY checkpoint.
`SECURITY_REVIEW_FAILED` routes back to `REPAIR_RUNNING` so the Migration
Engineer can address the findings, and escalates when the retry budget is gone.

**Delivery and verification**

| From | To |
| --- | --- |
| `FINAL_VALIDATION_RUNNING` | `FINAL_VALIDATION_PASSED`, `VALIDATION_FAILED`, `RUN_FAILED` |
| `FINAL_VALIDATION_PASSED` | `PR_PENDING` |
| `PR_PENDING` | `PR_CREATING` |
| `PR_CREATING` | `PR_CREATED`, `RUN_FAILED` |
| `PR_CREATED` | `MERGE_WAITING` |
| `MERGE_WAITING` | `POST_MERGE_VERIFICATION_RUNNING`, `VERIFIED`, `MONITORING_ACTIVE` |
| `POST_MERGE_VERIFICATION_RUNNING` | `POST_MERGE_VERIFICATION_PASSED`, `POST_MERGE_VERIFICATION_FAILED` |
| `POST_MERGE_VERIFICATION_PASSED` | `VERIFIED` |
| `POST_MERGE_VERIFICATION_FAILED` | `HUMAN_REVIEW_REQUIRED` |
| `VERIFIED` | `MONITORING_ACTIVE` |

`MERGE_WAITING → VERIFIED` is taken when the PR merges and no post-merge
verification environment is configured — Continuity does not claim verification
it did not perform, so this transition records `verification: not_configured`.
`MERGE_WAITING → MONITORING_ACTIVE` covers a PR closed without merging.

Merge detection is covered by §15 and ticket C8-06.

**Escape states**

`HUMAN_REVIEW_REQUIRED` and `RUN_FAILED` are reachable from every non-terminal
state listed above; the tables name them only where they are the *expected*
outcome of that step. `HUMAN_REVIEW_REQUIRED` means Continuity stopped
deliberately and a person must decide; `RUN_FAILED` means the run hit an
infrastructure or internal error.

| From | To |
| --- | --- |
| `HUMAN_REVIEW_REQUIRED` | `MIGRATION_PENDING` (person redirects), `MONITORING_ACTIVE` (person abandons) |
| `RUN_FAILED` | `MONITORING_ACTIVE` (retry or abandon) |

Terminal-per-run states: `CHANGE_IRRELEVANT`, `REJECTED`, `VERIFIED`,
`HUMAN_REVIEW_REQUIRED`, `RUN_FAILED`. Each returns the *project* to
`MONITORING_ACTIVE`; monitoring never stops because one run ended.

Implementation:

```python
ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {...}

def transition(run: Run, to: RunState, *, evidence: TransitionEvidence) -> Run:
    """Raises IllegalTransition if `to` is not reachable from run.state.
    Persists the transition with its evidence in one database transaction."""
```

Every transition is persisted with a timestamp, the actor (agent role or
`system` or `user`), and its evidence. The run timeline in the UI is a direct
read of this table — not a reconstruction.

---

## 9. Provider adapter architecture

Continuity is never hardcoded to one provider. Adapters are discovered through a
registry; adding a provider must not require changing orchestration.

```python
class ProviderCapability(StrEnum):
    CURRENT_VERSION = "current_version"
    VERSION_HISTORY = "version_history"
    CHANGELOG = "changelog"
    OPENAPI_SPEC = "openapi_spec"
    DOCS = "docs"
    SDK_RELEASES = "sdk_releases"
    HEALTH_CHECK = "health_check"

class ProviderAdapter(Protocol):
    provider_id: str
    capabilities: frozenset[ProviderCapability]

    async def get_identity(self) -> ProviderIdentity: ...
    async def get_current_version(self) -> ProviderVersion: ...
    async def get_version_history(self) -> list[ProviderVersion]: ...
    async def fetch_changelog(self, since: ProviderVersion | None) -> ChangelogDocument: ...
    async def fetch_openapi_spec(self, version: ProviderVersion) -> OpenApiDocument: ...
    async def fetch_docs(self, topic: str) -> DocsDocument: ...
    async def fetch_sdk_release_info(self) -> list[SdkRelease]: ...
    async def health_check(self) -> ProviderHealth: ...
```

Rules:

- Callers check `capabilities` before invoking; unsupported capabilities raise
  `CapabilityNotSupported` rather than returning fabricated data.
- Every returned document records `retrieved_at`, `source_url`, and
  `confidence` (`confirmed` vs `inferred`). Adapters never hallucinate provider
  facts they could not retrieve.
- Source types supported: OpenAPI documents, version endpoints, changelog
  pages, GitHub releases, SDK registry metadata, structured API feeds, official
  documentation.
- Adapter content is **untrusted external input** (see `03_SECURITY_ACCESS.md`).

Adapters are registered by id, so an externally-built demo provider can plug in
later by implementing this interface and registering itself — with **no change**
to Continuity's core logic. Provider monitoring is driven by the scheduler and
the adapter's own sources; it is never triggered by demo-specific hooks.

---

## 10. Change normalization

```python
class ProviderChange(BaseModel):
    provider_id: str
    old_version: str
    new_version: str
    change_type: ChangeType
    resource: str                      # endpoint path, event name, or scope
    old_contract: dict | None
    new_contract: dict | None
    breaking: bool
    security_relevant: bool
    authentication_relevant: bool
    source: SourceRef
    evidence: Evidence
```

### ChangeType, split by derivation

Not every change type can be recovered from a structural spec diff. The split is
explicit so that ownership is unambiguous: the deterministic differ (C5-03) is
responsible for the first group, the Change Scout agent (C5-04) for the second.

**Spec-derivable** — produced deterministically by `backend/providers/diff.py`
whenever both versions expose a machine-readable spec. No model involvement.

`endpoint_removed`, `endpoint_added`, `endpoint_renamed`,
`request_field_removed`, `request_field_added`, `request_field_required`,
`response_field_removed`, `response_shape_changed`, `enum_changed`,
`webhook_event_changed`, `authentication_changed`, `oauth_scope_changed`,
`header_requirement_changed`, `error_contract_changed`.

`endpoint_renamed` is the one heuristic member of this group: the differ emits a
removal/addition pair and proposes a rename only above a path-and-schema
similarity threshold. Below the threshold it stays two separate changes.

**Changelog-derived** — no reliable spec representation; extracted by the Change
Scout from prose changelogs, release notes, or SDK registry metadata, and always
carrying a `SourceRef` bound from the fetched document plus a verified
`evidence_quote` (see the Change Scout rule above).

`rate_limit_changed`, `sdk_deprecated`, `api_version_deprecated`,
`documentation_only`.

**Deterministic first.** When both versions have a spec, the change set comes
from the structural diff — the model is never asked to compare JSON. The model
interprets prose, judges severity nuance, and reconciles a changelog entry
against the spec diff (for example, confirming that a removed endpoint the differ
found is the one the changelog describes as deprecated). Change events are
deduplicated by
`(provider_id, old_version, new_version, change_type, resource)`.

---

## 11. API rehearsal

Before touching user code, Continuity tries to *prove* the incompatibility.

```
current integration + existing provider contract → expected PASS
current integration + new provider contract      → expected FAIL
```

The delta is the evidence that migration is genuinely required. Rehearsal runs
the affected tests (selected via `graph.tests_covering(...)`) against a
contract simulation built from the provider spec.

```
Current contract:  47 / 47 PASS
New contract:      39 / 47 PASS
Affected:          Checkout, Subscription Renewal, Webhook Handling
```

Rehearsal is adapter-based and capability-gated: when a provider exposes no
usable spec, the run records `REHEARSAL_UNAVAILABLE` with a reason and proceeds
on impact analysis alone. It does not fabricate a rehearsal result, and it is
not hardcoded to any specific provider.

Implemented in `backend/validation/rehearsal.py` as `rehearse(...)` plus a
`RehearsalAdapter` protocol, rather than the `RehearsalHarness` class this
section originally named — the seam that matters is the adapter, and a class
wrapping a single function would have been ceremony. The adapter is what an
externally-built provider simulator plugs into; Continuity holds no knowledge of
any provider's simulation mechanism (`CLAUDE.md` §3.13).

`NoSimulationAdapter` is the honest default for a deployment with no simulator.
It reports that it cannot simulate, producing `REHEARSAL_UNAVAILABLE`. It does
**not** run the same suite twice and report the inevitable non-difference: that
would be a fabricated result, and it would read as `REHEARSAL_FAILED` — "we
checked and found nothing" — for a check that never happened.

Counts come from parsed process output through `ExecutionProvider`. A suite that
times out or fails to collect produces no counts at all, and is reported as
unavailable rather than as zero failures, because zero failures reads as a pass.

---

## 12. Migration workspace and repair loop

### Workspace

Migration agents never modify the user's default branch or working tree. Each
migration gets an isolated workspace — a temporary git worktree or clean
checkout at a pinned source commit — and records:

`source_commit`, `target_branch`, `files_changed`, `commands_executed`,
`tests_executed`, `agent_attempts`, `security_findings`, `patch_diff`.

Implemented in `backend/migrations/workspace.py` as a `git worktree --detach` at
the pinned commit. `git worktree` is allowlisted two levels deep (`add`,
`remove`, `prune`, `list`) rather than `git` being opened up: none of those
reach the network, and the paths they receive are constructed in that module,
never by a model.

Writes go through `workspace.write_file`, which resolves the target and refuses
anything landing outside the root — including through a symlink planted inside
it, which is the case a string-prefix check accepts. The patch itself comes from
`git diff`, not from the writes Continuity happened to record, so a change made
by a command a migration ran is in the diff too.

Cleanup is deterministic: workspaces are removed on run completion, failure, and
process restart (orphan sweep on startup). The sweep touches only directories
carrying the `continuity-ws-` prefix — the workspace root may be shared, and
deleting something Continuity did not create would be far worse than leaking a
directory.

### Repair loop

```
Migration Engineer → patch → Validator
      ↑                          ↓
      └── diagnose ── failure evidence   (bounded)
                                 ↓ pass
                          Security Review
```

- `MAX_REPAIR_ATTEMPTS` (default 3, configurable) is enforced in application
  code, not by the model. Counted from persisted `migration_attempts` rows, so
  a restart mid-run cannot hand the loop a fresh budget, and the table's unique
  constraint on `(migration_run_id, attempt_number)` refuses a duplicate even
  under a race.
- **A model failure is a spent attempt, never a crashed run.** An engineer that
  errors, or returns a patch with no applicable edit, consumes one attempt and
  is retried. Letting either propagate would abandon the migration mid-flight
  with the workspace half-patched, no attempt row explaining why, and the budget
  bypassed entirely — nothing recorded means nothing spent.
- **Rejected edits are fed back.** An edit discarded for being outside the
  impact set tells the next attempt so, by path and reason. Without it the next
  attempt learns only that tests failed, and can propose the same rejected edit
  until the budget is gone.
- A finding that *blocks* is one a human must decide: a credential in the patch
  (DENY), a new dependency or a test modification (ASK). An edit discarded
  before it reached disk is recorded but does not block — nothing happened, so
  there is nothing to approve.
- Each attempt records: attempt number, failure evidence, diagnosis summary,
  files modified, tests executed, outcome.
- On exhaustion the run enters `HUMAN_REVIEW_REQUIRED`. It never loops
  indefinitely.
- A failing test may not be deleted or weakened to reach PASS. If the Migration
  Engineer believes a test itself is invalid, it must say so explicitly; that
  becomes a review finding, not a silent edit.

---

## 13. Validation

- **Test discovery** — deterministic: detect pytest/vitest/jest configuration
  from manifests and config files.
- **Execution** — through `ExecutionProvider` only (see
  `03_SECURITY_ACCESS.md` §6): timeout, cwd boundary, filtered environment,
  output caps, cancellation, audit log.
- **Parsing** — machine-readable output preferred over scraping human output.
  vitest and jest both emit a JSON report, and it is located within the output
  rather than assumed to be the whole of stdout, because both print warnings
  around it. pytest is read from its summary line.
- **Evidence** — pass/fail counts, failing test ids, and captured failure output
  are persisted; the UI and the PR body read from these records. Output is
  secret-filtered before storage: repository output is untrusted, and these
  excerpts are read into a browser.

Discovery **fails loudly**. A project with no evidence of a runner raises
`TestCommandNotFound` rather than defaulting to a command: "we could not find
your tests" and "your tests failed" must never look the same. Output that will
not parse raises `UnparseableTestOutput` for the same reason — returning zeroes
would make a suite that never ran indistinguishable from a clean one, because
"0 failed" reads as a pass. A timeout carries no counts at all.

Test results are never produced by a model. The guarantee is structural:
`store_result` accepts a `ParsedTestResult`, and the only way to obtain one is
`parse_result`, whose sole input is a `CommandResult` — the output of a process.
There is no signature in the path that a model's output fits.

---

## 14. Persistence model

Core tables: `users`, `projects`, `repositories`, `github_installations`,
`integrations`, `graph_nodes`, `graph_edges`, `providers`,
`provider_baselines`, `provider_specs`, `change_events`, `agent_runs`,
`agent_steps`, `tool_invocations`, `jobs`, `state_transitions`,
`migration_runs`, `migration_attempts`, `test_results`, `security_findings`,
`approvals`, `pull_requests`, `activity_events`, `audit_events`.

`jobs` backs the queue in §15 and `state_transitions` backs the audited history
in §8; both are named in prose there and listed here so the table set has a
single authoritative home.

### Implementation notes (Phase 1)

- **Enums are stored as text, not native database enums.** `StrEnumType`
  (`backend/models/base.py`) is a `TypeDecorator` that binds a `StrEnum` as a
  string and converts it back on load. A native ENUM would require a migration
  for every new member, and `RunState` has 45 that will grow; text storage keeps
  those changes free while `Mapped[RunState]` still loads a real `RunState`.
- **Migrations never import application code.** Alembic's `render_item` hook
  renders `StrEnumType` columns as `sa.String(length=n)`, which is what the
  database actually sees. Rendering the decorator would couple frozen schema
  history to code that keeps changing.
- **SQLite foreign keys are explicitly enabled** via a `PRAGMA foreign_keys=ON`
  connect listener. SQLite disables them by default, which would let a
  development database accept rows PostgreSQL would reject — including an
  approval referencing a non-existent user.
- **`alembic.ini` contains no connection string.** `env.py` reads the URL from
  `Settings`, so no database URL is ever committed.

### Constraints that enforce documented guarantees

These constraints are load-bearing, not incidental:

| Constraint | Guarantee |
| --- | --- |
| `approvals.actor_user_id` → `users.id` | An approval cannot name a user who does not exist |
| `ck_approval_resolved_requires_actor` | A row whose status is APPROVED or REJECTED must name a user — an unattributed approval cannot be stored at all |
| `ck_approval_resolved_requires_timestamp` | A resolved approval must record when, so the audit trail has no holes |
| `uq_change_event_dedup` on `(provider_id, old_version, new_version, change_type, resource)` | Polling a provider repeatedly cannot manufacture duplicate change events |
| `uq_attempt_number` on `(migration_run_id, attempt_number)` | The repair retry budget holds at the database level, not only in application code |

**What the approval constraints do and do not prove.** They make an
unattributed or untimed approval unstorable, which is the half of
`03_SECURITY_ACCESS.md` §4 a schema can enforce. They cannot prove *consent* —
a caller could supply any valid user id. Binding `actor_user_id` to the
authenticated approver is the approval service's job (C8-02); the constraints
are what stop that service from being bypassed silently rather than loudly.

### Session revocation

`users.session_version` is an integer bumped on sign-out. Session cookies are
signed, stateless bearer tokens, so deleting the browser's copy cannot revoke a
copy captured elsewhere; the token carries the version it was minted at, and
`current_user` refuses any token whose version does not match the stored value.
One increment therefore invalidates every outstanding session for that user.

### Agent execution seam

`ContinuityAgent` builds a model from the provider, renders a prompt, and calls
an injected `AgentRunner`. `StrandsAgentRunner` is the production implementation
and drives a real `strands.Agent`; tests substitute a stub to exercise retry,
malformed output, and escalation without a live model.

The seam exists because the behaviour worth testing — that invalid output is
retried a bounded number of times and then *escalates* rather than being coerced
— is contract enforcement in `ContinuityAgent`, not model behaviour. Testing it
through a live model would be slow, non-deterministic, and would still not prove
the contract holds. The genuineness of the Strands path is proven separately and
only by C2-07, which never passes without Bedrock.

### Cross-origin access

The frontend is served from its own origin, so browser requests to the API are
cross-origin. `CORSMiddleware` allows exactly `FRONTEND_ORIGIN` with
`allow_credentials=True` — required because the session is an HttpOnly cookie,
and the reason the allowed origin is one exact value rather than a wildcard:
the CORS spec forbids combining the two, and a wildcard would let any site read
authenticated responses. `FRONTEND_ORIGIN` defaults to `http://localhost:3000`
in development and is **required** in production, so CORS is never guessed.

### Migration run schema

```python
class MigrationRun(BaseModel):
    id: UUID
    project_id: UUID
    change_event_id: UUID
    provider_id: str
    from_version: str
    to_version: str
    state: RunState
    workspace_id: str | None
    source_commit: str
    target_branch: str | None
    attempts: list[MigrationAttempt]
    rehearsal: RehearsalResult | None
    validation: ValidationResult | None
    security_review: SecurityReviewResult | None
    approval: ApprovalRecord | None
    pull_request: PullRequestRecord | None
    evidence_report_id: UUID | None
    created_at: datetime
    completed_at: datetime | None
```

```python
class MigrationAttempt(BaseModel):
    attempt_number: int
    plan_summary: str
    files_changed: list[str]
    patch_diff: str
    commands_executed: list[CommandRecord]
    tests_executed: list[TestSelector]
    failure_evidence: list[TestFailure]
    diagnosis_summary: str | None
    outcome: Literal["passed", "failed", "escalated"]
```

Approvals are stored explicitly as `PENDING | APPROVED | REJECTED` with actor
and timestamp. A model can never write an approval record.

---

## 15. Background execution

```python
class JobQueue(Protocol):
    async def enqueue(self, job: Job) -> JobId: ...
    async def cancel(self, job_id: JobId) -> None: ...
```

Job kinds: `provider_monitor`, `repository_scan`, `integration_map`,
`change_analysis`, `impact_analysis`, `rehearsal`, `migration`, `validation`,
`security_review`, `pr_create`, `pr_status_poll`, `post_merge_verify`.

### Merge detection

`MERGE_WAITING` advances only when Continuity learns the PR's outcome. Two
mechanisms, in order of preference (ticket C8-06):

1. **GitHub App webhook** — the App subscribes to the `pull_request` event
   (`closed`, with `merged: true|false`). This requires only the Pull requests:
   Read permission we already hold; the subscription is configured on the App,
   not per repository. Deliveries are authenticated with
   `GITHUB_APP_WEBHOOK_SECRET` (HMAC-SHA256 over the raw body, constant-time
   compared) and are otherwise treated as untrusted input: the payload is used
   only to *look up* Continuity's own PR record by number and repository id,
   never as a source of truth about state.
2. **`pr_status_poll` job** — a fallback for deployments that cannot receive
   inbound webhooks. Polls the PR state on a backoff schedule until it is merged
   or closed.

Both paths converge on the same transition, so the state machine does not care
which delivered the news. When neither is available, the run stays in
`MERGE_WAITING` and the UI says so — Continuity does not assume a merge.

Development uses an in-process asyncio worker with a database-backed job table
(so jobs survive restart). Production may swap in SQS + workers, Step Functions,
or AgentCore Runtime without changing callers.

Provider monitoring runs on a scheduler per provider, independent of any user
action or demo trigger.

---

## 16. Observability

Structured activity events, persisted and streamed to the UI:

`repository_scan_started`, `integration_detected`,
`integration_mapping_complete`, `provider_change_detected`,
`change_classified`, `impact_analysis_started`, `impacted_workflow_detected`,
`rehearsal_started`, `rehearsal_confirmed`, `migration_started`,
`patch_generated`, `validation_started`, `validation_failed`, `repair_started`,
`validation_passed`, `security_review_started`, `approval_required`,
`approval_received`, `migration_verified`, `pull_request_created`.

Each event carries `run_id`, `actor`, `timestamp`, a concise summary, and an
evidence reference.

**Raw model chain-of-thought is never persisted, logged, or exposed.** What is
exposed: task descriptions, tool invocations and their arguments (secret
filtered), status, durations, errors, result summaries, and verifiable evidence.

Tracing uses OpenTelemetry (already a Strands dependency), exportable to
CloudWatch via AgentCore Observability.

---

## 17. AgentCore integration

AgentCore is production infrastructure *around* the agent system. Strands
remains the agent framework. Every AgentCore dependency needs a stated technical
reason; unavailable features are recorded as blockers, never faked.

| Priority | Service | Reason | Phase |
| --- | --- | --- | --- |
| 1 | **Runtime** | Session isolation and long execution windows suit migration runs, which are long-lived and must not share state across tenants. | 8 |
| 2 | **Observability** | OTEL-compatible CloudWatch dashboards over agent runs, tool calls, failures, and latency — replaces building our own tracing backend. | 8 |
| 3 | **Identity** | Secure vault storage for provider/GitHub refresh tokens, keeping long-lived credentials out of Continuity's own database. | 8 |
| 4 | **Gateway / Policy** | Enforces tool access outside the model, reinforcing the deterministic policy layer. | 8, if it demonstrably adds enforcement we do not already have |
| 5 | **Evaluations** | Systematic agent evaluation once metrics exist. | 9 |
| 6 | **Memory** | Persistent organizational decisions, e.g. "this team previously rejected `customers.write` for this provider" — genuinely useful, but only after approvals work. | 9, optional |
| 7 | **Browser / Code Interpreter** | Only if they measurably improve provider-doc inspection or sandboxed execution beyond our own `ExecutionProvider`. | Evaluate in 9 |

---

## 18. Evaluation

Continuity is not evaluated by asking a model "how good was this migration?"
Every metric is deterministic and traceable to recorded execution:

**Detection** — provider update detection latency; missed updates.
**Classification** — breaking-change classification accuracy; relevance accuracy
(against labelled fixtures); unnecessary migration rate (runs started for
irrelevant changes).
**Localization** — affected file accuracy; affected workflow accuracy.
**Execution** — migration success rate; repair iterations per success; build
success; contract-test success; regression-test success.
**Safety** — security violations; unauthorized action attempts blocked; correct
approval escalation rate.
**Delivery** — PR creation success.
**Cost** — total execution time, tool calls, token usage.

If an **Integration Health** score is displayed, its formula is documented here
and every input traces to a stored record. Phase 4 baseline formula:

```
health = 100
       − 15 × (relevant unresolved breaking changes)
       −  5 × (relevant unresolved non-breaking changes)
       − 20 × (pending high-risk approvals)
       − coverage_penalty

coverage_penalty = 0                            if total_integration_points == 0
                 = round(20 × uncovered / total) otherwise

clamped to [0, 100]
```

where `uncovered` is the number of integration points with no `COVERED_BY_TEST`
edge and `total` is the number of integration points in the current graph
version. The coverage term is a proportion, so it is bounded at 20 by
construction — a project is not punished for being large.

The result is a 0–100 score, rendered with a `%` suffix in the UI. Worked
example matching `04_FRONTEND_SPEC.md` §3.6: 23 integration points, 5 of them
untested, one active but non-breaking relevant change, no pending high-risk
approvals →
`100 − 5 − round(20 × 5/23) = 100 − 5 − 4 = 91`.

Every input is a stored record: unresolved changes come from `change_events`,
approvals from `approvals`, and coverage from `graph_edges`. **No score is
displayed before its inputs are actually recorded** — a project whose graph has
not been built shows no health value at all rather than a default.

---

## 19. Failure handling for external services

Credentials, account ids, model ids, AgentCore resources, IAM roles, KMS keys,
and GitHub App secrets are **never invented**.

If Bedrock is unavailable: document the exact blocker in `STATUS.md`, preserve
the `ModelProvider` abstraction, continue all deterministic work, and use only a
clearly-labelled development test adapter — never described as Bedrock.

If AgentCore requires manual infrastructure: document the exact setup steps,
mark the blocker, and continue independent development.

A single blocked cloud feature never halts the project.

---

## 20. Open decisions

Recorded so they are resolved deliberately, not by accident:

1. ~~**Google sign-in mechanism**~~ — **resolved.** Google OAuth 2.0
   authorization-code flow via Authlib with server-side sessions, kept entirely
   separate from GitHub App installation authorization. Owned by ticket C1-05 in
   Phase 1, because the approval guarantee in `03_SECURITY_ACCESS.md` §4 requires
   a real authenticated user id and cannot be built on a placeholder.
2. **Postgres cutover point** — SQLite until concurrency demands otherwise;
   SQLAlchemy + Alembic keep the switch cheap.
3. **Rehearsal harness depth** — full contract simulation vs. spec-driven
   assertion checking. Decide in Phase 6 against a real fixture.
4. **AgentCore Gateway** — adopt only if it adds enforcement beyond
   `backend/security/policy.py`. Decide in Phase 8.
