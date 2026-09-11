# STATUS

**This file is authoritative.** If any other document, README badge, or commit
message disagrees with it, this file is right and the other is stale.

---

## Overall completion

**80%**

Phases 0–7 complete. Continuity now writes the patch. Each migration runs in an
isolated git worktree — the user's checkout is byte-identical afterwards — and
every proposed edit is checked before a byte reaches disk: out of scope,
credential-shaped, or removing a test, and it is discarded. Tests are discovered
from the project's own files, run through the confined executor, and parsed from
real output. When the patch fails, the repair loop responds to the evidence,
bounded by a budget counted from database rows. Test suite has **zero skips**.

| Field | Value |
| --- | --- |
| Current phase | Phase 8 — Security + approval + GitHub PR |
| Current ticket | C8-01 |
| Last updated | 2026-09-11 |

---

## Phase progress

| Phase | Scope | Target | Status |
| --- | --- | --- | --- |
| 0 | Project anchoring | 10% | **DONE** — reviewer PASS |
| 1 | Application + backend foundation | 20% | **DONE** — reviewer PASS |
| 2 | Strands + core orchestration | 30% | **DONE** — reviewer PASS |
| 3 | GitHub + safe repository ingestion | 40% | **DONE** — reviewer PASS |
| 4 | Integration Mapper + Intelligence Graph | 50% | **DONE** — reviewer PASS |
| 5 | Provider monitoring + Change Scout | 60% | **DONE** — reviewer PASS |
| 6 | Execution safety, impact analysis, rehearsal | 70% | **DONE** — reviewer PASS |
| 7 | Migration Engineer + repair loop | 80% | **DONE** — reviewer PASS |
| 8 | Security + approval + GitHub PR | 90% | PENDING |
| 9 | Production security + frontend + polish | 100% | PENDING |

---

## Tickets

**Completed:** C0-01 … C0-04, C1-01 … C1-07, C2-01 … C2-08, C3-01 … C3-06, C4-01 … C4-04, C5-01 … C5-05, C6-01 … C6-04, C7-01 … C7-04
**In progress:** none
**Next:** C8-01 — see `05_FEATURE_TICKETS.md`
**Pending:** C8-01 onward

---

## Blockers

### B-01 · Live model access — **RESOLVED** (2026-09-11)

Was: no AWS credentials, so Bedrock was unreachable and C2-07 could only skip.

**Resolved by changing the primary model, not by waiting.** Continuity now runs
on **Google Gemini through Strands** (`strands-agents[gemini]`). C2-07 passes
live and proves the full loop: Strands Agent → Gemini → tool call → tool result
→ final structured response. The tool returns a nonce generated fresh each run,
so the assertion cannot be satisfied by a plausible-sounding answer.

Amazon Bedrock remains implemented as an optional future provider.

### B-02 · GitHub App — **RESOLVED** (2026-09-11)

App `Continuity Integration Agent` (id 4900912) is installed on
`tony19053000/continuity` with exactly the permissions in
`03_SECURITY_ACCESS.md` §3.

Verified live, end to end: RS256 App JWT minting, installation discovery,
installation token minting, authorized-repository listing, repository metadata
and file reads, and — importantly — that an *unauthorized* repository is refused
and an excluded path (`.env`) is refused before any network call.

Continuity has indexed its own repository through the real App: 143 files,
764 symbols, 7 routes, 1 secret path excluded.

### B-03 · Google OAuth — **RESOLVED** (2026-09-11)

Web client configured with redirect URI `http://localhost:8000/auth/callback`.
Verified live: OIDC discovery, a real authorization redirect to
`accounts.google.com` whose `client_id` and `redirect_uri` match the registered
client, the CSRF state cookie, and the refusals (mismatched state → 401, open
redirect → 422, unauthenticated `/auth/me` and `/auth/logout` → 401).

Sign-in is verified as far as it can be without a human completing a Google
consent screen. That last step is a manual check, not an automatable one.

### B-04 · No TEE / Nitro Enclave infrastructure — **OPEN**, low priority

**Impact:** C9-03 cannot deliver hardware-backed attestation.

**Handling:** the `ConfidentialExecutionProvider` abstraction is built and
`DevelopmentIsolatedExecutor` remains functional. The UI reports
`TEE Attestation: Not Configured`. **Attestation is never faked.**

### B-05 · AgentCore not yet provisioned — **OPEN**, expected

Not a regression: AgentCore is Phase 8 (C8-05). AWS access is now verified, so
the prerequisite is met. `BedrockAgentCoreFullAccess` is attached to the
`continuity-dev` user.

### B-06 · No scheduler process — **OPEN**, recorded at the Phase 5 review

`monitor_project()` is a callable worker function. **Nothing invokes it
periodically.** There is no cron, no dispatcher, no loop — provider monitoring
runs when something calls it, and in this repository the only callers are tests.

What *is* proven: the monitor is independent of the API layer (asserted at the
import graph, and by an integration test that never constructs an HTTP client),
so nothing about it requires a user request. What is **not** built is the process
that would call it every N minutes in production.

C5-05's Files list names only `backend/workers/provider_monitor.py`, so this is
within ticket scope rather than an unfinished ticket — the same shape as
`backend/workers/repository_scan.py` from Phase 3. It is recorded here so that
"Continuity monitors providers autonomously" is never read as "a scheduler is
running". Scheduling infrastructure belongs with deployment (Phase 8, C8-05).

---

## Tests

| Suite | Command | State |
| --- | --- | --- |
| Python unit + integration | `uv run pytest` | **925 passing, 0 skipped** |
| Security | `uv run pytest tests/security` | **277 passing** |
| Frontend unit | `npm run test` | **17 passing** |
| E2E | `npm run test:e2e` | Not yet created (Phase 9) |
| Lint (py) | `uv run ruff check .` | **Passing** |
| Typecheck (py) | `uv run mypy backend` | **Passing** (77 source files) |
| Lint (web) | `npm run lint` | **Passing** |
| Typecheck (web) | `npm run typecheck` | **Passing** |
| Build (web) | `npm run build` | **Passing** — routes `/`, `/signin` |
| Migrations | `uv run alembic check` | **In sync** with the models |
| Live integrations | `uv run pytest tests/integration/test_external_integrations.py` | **9 passing** — AWS, GitHub App, Google OAuth |
| Live Strands + Gemini | `uv run pytest tests/integration/test_strands_roundtrip.py` | **3 passing** — real tool call proven |
| Live workflow inference | `uv run pytest tests/integration/test_phase4_live.py` | **3 passing** — Gemini names real workflows |
| Live changelog interpretation | `uv run pytest tests/integration/test_phase5_live.py` | **3 passing** — Gemini extracts changelog-derived changes; injection contained |
| Provider monitoring (end to end) | `uv run pytest tests/integration/test_provider_monitoring.py` | **14 passing** — new adapter plugged in, dedup proven |
| Execution containment | `uv run pytest tests/security/test_execution.py` | **70 passing** — real processes, real kills |
| Rehearsal | `uv run pytest tests/unit/validation` | **23 passing** — real pytest runs, real deltas |
| Live impact judgment | `uv run pytest tests/integration/test_phase6_live.py` | **3 passing** — Gemini judges a real change set |
| Migration workspace | `uv run pytest tests/unit/migrations` | **27 passing** — real worktrees, source proven untouched |
| Patch rules | `uv run pytest tests/unit/agents/test_migration_engineer.py` | **40 passing** — scope, secrets, tests, dependencies |
| Repair loop | `uv run pytest tests/unit/orchestration/test_repair.py` | **18 passing** — real pytest runs, budget counted from rows |
| Live migration | `uv run pytest tests/integration/test_phase7_live.py` | **3 passing** — Gemini patches a real project; 15/15 consecutive |

Counts above are from real runs, not estimates.

---

## Model / agent infrastructure state

| Item | State |
| --- | --- |
| Agent framework | **Strands Agents SDK** — unchanged, and the reason the model swap cost one class |
| Primary model | **Google Gemini** via `strands.models.gemini.GeminiModel` — **live and proven** (C2-07) |
| `GEMINI_MODEL` | Defaults to `gemini-2.5-flash`, confirmed present in the live model list |
| Optional future provider | Amazon Bedrock — implemented, not in use |
| AWS credentials | **Verified** — profile `continuity-dev`, region `us-west-2`, standard chain. No AWS key in `.env` |
| AgentCore Runtime | Not provisioned — Phase 8 (C8-05), see B-05 |
| AgentCore Observability | Not provisioned — Phase 8 |
| AgentCore Identity | Not provisioned — Phase 8 |
| AgentCore Gateway/Policy | Not provisioned; adoption conditional (§17) |

Nothing above is claimed as working beyond what the live tests prove.

---

## Security state

| Control | State |
| --- | --- |
| Secret filtering | **Implemented** — path exclusion, content redaction, and repository `.gitignore` rules, at all five enforcement points |
| Policy engine (ALLOW/ASK/DENY) | **Implemented** — `backend/security/policy.py`, matrix parity-tested against §4 |
| Approval integrity | **State implemented** (C2-08); HTTP surface + resume flow in C8-02 |
| Repository boundary | **Implemented** — normalize-then-resolve, symlink-safe; one conformance suite over both sources |
| Untrusted external content | Specified §5; exercised in C5-04 |
| ExecutionProvider | Specified §6; implemented in C6-01 |
| Confidential execution / TEE | **Not built.** `ExecutionProvider` exists and is process isolation, not sandboxing. No `ConfidentialExecutionProvider`, no attestation — see B-04 |
| Committed secrets | None. Verified against the staged diff at each gate |

---

## GitHub integration state

| Item | State |
| --- | --- |
| GitHub App | **Installed and verified live** — `Continuity Integration Agent`, app id 4900912 |
| Repository authorization | **Verified** — `tony19053000/continuity` only; others refused |
| Branch/PR delivery | Not implemented (Phase 8) |
| Merge detection | Not implemented (C8-06) |
| Local `gh` CLI | Authenticated as `tony19053000` |
| Remote `origin` | `https://github.com/tony19053000/continuity.git` |

---

## Git

| Field | Value |
| --- | --- |
| Branch | `main` (tracking `origin/main`) |
| Latest commit | `3aaf6af` — feat: execution containment, impact analysis, and rehearsal |
| Push state | **Phase 6 is committed locally but NOT pushed.** Phases 0–5 are on `origin/main` (Phase 5 was pushed manually after the coding session's permission gate blocked it). The gate is not a git or GitHub authentication problem — `gh` is authenticated and the remote is unchanged. Run `git push origin main` to publish Phase 6. |

---

## Context state log

Append after each meaningful phase. **Do not delete previous entries.**

### 2026-09-10 — Phase 0, first pass

**What was built:** The two development subagents (`.claude/agents/coder.md`,
`reviewer-tester.md`), five anchor documents, `CLAUDE.md`, `README.md`,
Apache-2.0 `LICENSE`, `.gitignore`, `.env.example`, and this file. No
application code — correct for Phase 0.

**Environment verified:** Python 3.12.3, Node 22.22.1, npm 10.9.4, git 2.43.0,
`gh` 2.45.0 (authenticated). AWS CLI absent, `~/.aws` absent. Git initialized on
`main`; `origin` set to the empty repository above.

**Upstream facts verified from official sources** and recorded with URLs in
`02_ARCHITECTURE.md` §1: `strands-agents` 1.55.1 (Python ≥3.10, Apache-2.0);
`from strands import Agent, tool`; `from strands.models import BedrockModel`;
default model `global.anthropic.claude-sonnet-4-6`; structured output via
`structured_output_model` → `result.structured_output`; `bedrock-agentcore`
1.22.0; AgentCore GA since Oct 2025.

**Key architecture decisions:**
- Python 3.12 + FastAPI; SQLAlchemy 2.x + Alembic on SQLite (dev) → PostgreSQL
  (prod). A graph database was rejected as unjustified at this size.
- Relational, versioned Integration Intelligence Graph (`graph_nodes` /
  `graph_edges` per `graph_version`), giving free scan history.
- Strands is the only agent framework. LangChain, CrewAI, and AutoGen are
  explicitly rejected in `02_ARCHITECTURE.md` §2.
- One shared Bedrock model provider across all seven runtime agents, with
  per-role prompts, tool allowlists, and output contracts.
- Deterministic code owns spec diffing, AST analysis, test execution, state
  transitions, and policy. The model owns semantic judgment only
  (`02_ARCHITECTURE.md` §7 division-of-labour table).
- `ChangeType` is split into spec-derivable (owned by the differ, C5-03) and
  changelog-derived (owned by the Change Scout, C5-04).
- Apache-2.0 chosen over MIT for its patent grant and to match Strands and
  AgentCore licensing.

**Review outcome:** `reviewer-tester` returned **FAIL** with 15 findings on the
first pass — a genuine gate, not a rubber stamp. All 15 were corrected:

- F1 this file did not exist (26 dangling references) — created.
- F2 Phase 0 self-declared DONE at 10% before review — reset to IN REVIEW / 0%.
- F3 Integration Health formula could not produce the 96% shown in the UI, and
  its cap was ambiguous — reformulated with a proportional coverage term; UI
  example changed to a producible 91% with a worked derivation.
- F4 backward dependencies — `ExecutionProvider` moved from Phase 7 to Phase 6
  (C6-01), and approval state split out into Phase 2 (C2-08). Phases 6 and 7
  were renumbered accordingly.
- F5 no ticket implemented user authentication, which the approval guarantee
  depends on — added C1-05.
- F6 `REHEARSAL_UNAVAILABLE` was used but absent from the state machine — added,
  and distinguished from `REHEARSAL_FAILED`.
- F7 the state machine was a layout-implied diagram with unreachable states and
  dead ends — rewritten as an explicit, complete edge list.
- F8 `SourceRef` was referenced but never defined — defined alongside
  `Evidence`, which gained `source_ref` and dropped its duplicate `url`.
- F9 C5-03 promised spec-diffing change types no spec diff can produce — scoped
  to the spec-derivable subset.
- F10 `.gitignore` was missing `*.crt` — added.
- F11 `.env.example` claimed "no values" while showing eight non-sensitive
  defaults — rule reworded honestly (no secrets; defaults documented).
- F12 upstream facts cited source names, not URLs — URLs added.
- F13 C0-02 said "six anchor documents" and listed five — corrected.
- F14 nothing detected a PR merge, so `MERGE_WAITING` could never advance —
  documented the webhook + polling mechanism and added ticket C8-06; this also
  justifies `GITHUB_APP_WEBHOOK_SECRET`.
- F15 Phase 9 had no acceptance criteria, "minimal diff" was unmeasurable,
  `/health` components were unspecified, and `Status:` lines were missing —
  all fixed; "minimal" replaced with a checkable impact-set rule.

**Do not accidentally change:**
- The `ALLOWED_TRANSITIONS` edge list in `02_ARCHITECTURE.md` §8 — C2-04 asserts
  code and document match exactly, in both directions.
- The ALLOW/ASK/DENY matrix in `03_SECURITY_ACCESS.md` §4 — C2-03 tests every
  row.
- The rule that policy enforcement and approval writes live outside the agent
  layer. This is the product's core safety property.
- The exclusion of Provider Lab and demo applications from this repository.

**Re-review outcome: PASS.** All 15 findings verified cleared with evidence. The
reviewer additionally machine-checked the §8 state machine (45 states, no
unreachable states, no dead ends), all 54 ticket `Deps:` lines (no
later-phase dependencies, no cycles), the `ChangeType` split (14 + 4 = 18,
disjoint), and the Integration Health arithmetic. It applied one in-scope
trivial fix: a cross-reference in `02_ARCHITECTURE.md` pointing at §14 instead
of §19.

Three non-blocking notes were recorded for later: §9 prose says `source_url`
where the `SourceRef` model says `url`; §8's escape-edge rule is stated in prose
rather than as a generated edge set; and C0-04 has no `Tests:`/`Security:` lines
because it *is* the gate.

**Next intended task:** Phase 1, ticket C1-01 — backend application skeleton
(FastAPI app factory, router registration, lifespan hooks, `/health` with the
four specified components). Then C1-02 configuration, C1-03 persistence, C1-04
errors/logging/events, C1-05 authentication, C1-06 frontend shell, C1-07
engineering baseline — then the Phase 1 review gate.

### 2026-09-10 — Phase 1, application + backend foundation

**What was built:** FastAPI application factory with typed error handling and a
four-component `/health`; `pydantic-settings` configuration with an explicit
`NotConfigured` sentinel per integration group; 24-table SQLAlchemy schema with
an Alembic baseline; structured JSON logging with handler-level secret
redaction; Google OAuth sign-in with signed, revocable sessions; a Next.js
shell rendering real backend state; and CI running all seven verification
commands plus a build-output secret grep.

**Environment note:** this machine's Python has no `ensurepip`, so `python -m
venv` fails and the system interpreter is PEP 668-managed. `uv` is used instead,
locally and in CI. Run commands as `uv run <cmd>`.

**Key implementation decisions:**
- `StrEnumType` (`backend/models/base.py`) stores `StrEnum` columns as text and
  converts back on load. Native database ENUMs would need a migration per new
  member, and `RunState` has 45 that will grow.
- Migrations render `StrEnumType` as `sa.String` via Alembic's `render_item`,
  so frozen schema history never imports application code.
- SQLite foreign keys are explicitly enabled by a connect listener; SQLite
  disables them by default, which would let a development database accept rows
  PostgreSQL rejects.
- `alembic.ini` carries no connection string; `env.py` reads it from `Settings`.
- Sessions are signed stateless tokens carrying `users.session_version`. Sign-out
  bumps the counter, which revokes every outstanding token rather than only the
  browser's copy.
- CORS allows exactly `FRONTEND_ORIGIN` with credentials — required for the
  HttpOnly session cookie, and why a wildcard is impossible.

**Review outcome:** the gate ran twice and failed the first time with 10
findings, several of them false-green: `/auth/login` raised
`SessionMiddleware must be installed` whenever Google was actually configured
(only the unconfigured 503 path had tests, so the real sign-in flow had never
run); log redaction missed anything nested inside an `extra` dict, list, or
object `__repr__`; `approvals.actor_user_id` was nullable with no CHECK, so an
`APPROVED` row with no approver could be inserted; the §16 activity-event
vocabulary was documented but never implemented; an open redirect via
`?redirect_to=`; sign-out that did not invalidate anything; a hardcoded green
"Job queue: Ready" backed by nothing; a no-op empty router; a test that could
not fail; and no sign-in UI.

The second pass cleared all ten and found eight more, of which one was
functional: **no CORS middleware existed**, so the browser could never reach the
API — every frontend test stubbed `fetch`, leaving the suite green while the
real path was blocked. The rest were honesty defects: docstrings claiming
guarantees the code did not provide (the approval CHECK proves *attribution*,
not consent), two more tests that could not fail, and documentation that had not
caught up with the schema.

**Verified by hand, not only by tests:** ran both servers and loaded the app in
a browser. `/` renders real backend state (`degraded`, database connected, three
integrations `Not configured`) and `/signin` states that signing in does not
grant repository access.

**Do not accidentally change:**
- The two approval CHECK constraints, or the claim boundary around them: they
  prove a resolved approval names a user and a time, not that a human consented.
  Consent is C8-02's job.
- `session_version` comparison in `current_user` — it is what makes sign-out
  mean anything.
- `allow_origins=[FRONTEND_ORIGIN]` with `allow_credentials=True`. A wildcard
  here would let any site read authenticated responses, and the CORS spec
  forbids combining the two anyway.
- The activity-event vocabulary test, which parses `02_ARCHITECTURE.md` §16
  rather than hardcoding the list. It is mutation-tested.

**Next intended task:** Phase 2, C2-01 — the Bedrock model provider abstraction,
then the Strands agent base and the deterministic tool dispatcher.

**Post-review note (secret fixtures):** GitHub push protection rejected the
first Phase 1 push because `tests/unit/shared/test_redaction.py` contained
Slack- and Stripe-shaped literals. They were synthetic, but clicking "allow the
secret" is the wrong reflex here — and committing scanner-tripping strings
trains everyone to ignore scan results, which is how a real leak gets missed.
All such fixtures now live in `tests/support/secret_samples.py` and are
assembled from parts at import time, so the complete token exists only in
memory during a test. Import from there rather than pasting a token into a new
test.

### 2026-09-10 — Phase 2, Strands + core orchestration

**What was built:** The Bedrock model provider (the only module that constructs
a model), `ContinuityAgent` with structured-output contracts, the seven runtime
agents' declared contracts, the tool registry and dispatcher, the deterministic
policy engine, the 45-state machine, the run coordinator, and the approval state
model.

**Key decisions:**
- `AgentRunner` is an injection seam. `StrandsAgentRunner` drives a real
  `strands.Agent`; tests substitute a stub. The seam exists because the property
  worth testing — invalid output is retried a bounded number of times and then
  *escalates* rather than being coerced — is contract enforcement in
  `ContinuityAgent`, not model behaviour. The genuineness of the Strands path is
  proven only by C2-07, which never passes without Bedrock.
- Escape edges (`HUMAN_REVIEW_REQUIRED`, `RUN_FAILED`) are added
  programmatically to every non-terminal state rather than repeated on 40 rows.
- The policy engine escalates by *context*: a branch write targeting the default
  branch becomes `WRITE_PROTECTED_BRANCH` and is denied, whatever the agent
  believed it was doing. Unknown actions default to DENY.
- `ToolDispatcher` checks the role allowlist *before* consulting policy, so a
  capability a role never had is not even evaluated.
- Per-role temperature: 0.0 for agents that must not invent (Change Scout,
  Impact Analyst, Validator, Security Reviewer), 0.2 for the Migration Engineer,
  0.4 for the Red-Team agent.

**Two parity guards are the phase's strongest claims**, and the reviewer
mutation-tested both: `ALLOWED_TRANSITIONS` is compared against
`02_ARCHITECTURE.md` §8 in *both directions*, and the policy matrix against
`03_SECURITY_ACCESS.md` §4. Each has a vacuity guard so a renamed heading cannot
make the test pass silently.

`03_SECURITY_ACCESS.md` §4 gained one row this phase: "Force push or rewrite
history" was split into two, because the code models them as two distinct
actions. The reviewer specifically checked this was precision rather than
gaming the test.

**Process change:** `scripts/verify.sh` runs all eight gate checks and prints one
line each. Reviewers read that instead of running the commands individually —
paging through pages of passing output was costing far more than it proved.
Review now runs on a smaller model against pre-captured output.

**Do not accidentally change:**
- The parity guards or their vacuity guards. They are what stop the documented
  state machine and security matrix from becoming fiction.
- `apply_proposal` in the coordinator — it is the concrete point where a model's
  proposed state change is validated rather than obeyed. An illegal or unknown
  proposal escalates to a human.
- The import-graph tests in `tests/security/test_agent_boundaries.py`. They hold
  for code nobody has written yet, which is the point.

**Known-honest gap:** the tool registry is empty. Tools land in Phases 3+ with
the capabilities they wrap. A test asserts the registry is empty rather than
implying tools work.

**Next intended task:** Phase 3, C3-01 — the secret filter's path-exclusion
half, then the GitHub App client and the deterministic repository indexer.

### 2026-09-10 — Phase 3, GitHub + safe repository ingestion

**What was built:** The secret filter's path-exclusion half, the
`RepositorySource` protocol with boundary enforcement, a local development
adapter and a GitHub-backed source behind one conformance suite, the
deterministic indexer (Python AST, TS/JS heuristics, manifest parsing), bounded
context retrieval, and the scan worker.

**The central guarantee is now real:** a repository is never dumped into a
model. Everything a model sees is selected from the index by `retrieval.py`
under `CONTEXT_BUDGET_BYTES`, symbol-scoped, secret-filtered, and carrying
`Evidence` with a file path and line span.

**Key decisions:**
- Paths are normalized *before* resolution, then resolved and proven inside the
  root — which is what catches a symlink whose name is innocent but whose target
  is not. Percent-encoded traversal is treated as a literal filename: decoding
  it would create the vulnerability it resembles.
- Sources record what they pruned. Without that, the scan summary would report
  zero secrets excluded and understate a control that is working.
- TS/JS analysis is regex-based and every result carries `heuristic=True`. A
  real parse needs the TypeScript compiler — a Node subprocess per file — and
  that trade is recorded rather than hidden.
- Webhook detection requires three signals together. A two-signal version
  matching `ast.dump()` substrings flagged five handlers in Continuity's own
  backend, including the detector itself.
- The GitHub source loads eagerly because the protocol is synchronous and the
  API is not. Excluded paths are filtered before any content request, so a
  `.env` is never transferred.

**Review took four passes.** Rounds 1–2 found scope silently unmet: no
GitHub-backed `RepositorySource` existed despite C3-03 requiring a shared
conformance suite, and `03_SECURITY_ACCESS.md` §2 promised honouring the
repository's own `.gitignore` secret rules, which was never implemented. Both
were built rather than descoped.

Rounds 3–4 were both about one function. `.gitignore` trigger words were matched
as substrings, so `monkey/` ("key") and `designtokens/` ("token") were adopted
as secret rules and those directories vanished from analysis entirely. Fixing
that with whole-token matching then broke the other direction: `authToken.json`
stopped being adopted, because splitting on non-alphanumerics never decomposes
camelCase. The final tokenizer handles separators, underscores, and case
transitions, and is Unicode-aware — an ASCII-only split tore `envío/` into
`{env, o}` and matched a trigger token from the wreckage.

**The asymmetry worth remembering:** a secret wrongly *included* is redacted
downstream; a source directory wrongly *excluded* is never indexed, never
retrieved, never analysed — the product silently ignores part of the codebase.
Both failure modes now have regression tests naming the exact inputs that broke.

**Do not accidentally change:**
- `_tokenize` in `secret_filter.py`. Both directions are locked by tests naming
  real inputs; loosening or tightening it will break one of them.
- The one accepted ambiguity: `design-tokens/` IS adopted, because it is
  lexically indistinguishable from `auth-tokens/`. Documented and asserted, not
  discovered later.
- The conformance suite. A boundary rule that holds locally and lapses over
  GitHub would be invisible until it mattered.
- `tests/fixtures/expected_index.json`. Regenerate deliberately with
  `uv run python -m scripts.regenerate_index_snapshot` and read the diff.

**Next intended task:** Phase 4, C4-01 — graph persistence and query layer, then
deterministic integration extraction and the Integration Mapper agent.

### 2026-09-11 — External integrations live; primary model moved to Gemini

**What changed:** the primary model is now **Google Gemini through Strands**
(`strands-agents[gemini]`), not Amazon Bedrock. Strands remains the agent
framework — that was the point of the provider abstraction, and the swap proved
it: one new class (`GeminiModelProvider`) and one config group. **No agent,
contract, prompt, tool, state machine, or orchestration test changed.**

Amazon Bedrock stays implemented as an optional future provider. Amazon Bedrock
**AgentCore** remains the production agent-infrastructure target for Phase 8 —
AgentCore is where agents *run*, which is independent of which model they call.

**Verified live, not asserted:**
- **Gemini (C2-07).** Proves `Strands Agent → Gemini → tool call → tool result →
  final structured response`. The tool returns a nonce generated fresh per run,
  so the assertion cannot be satisfied by a plausible answer. 3 tests.
- **AWS.** Profile `continuity-dev`, `us-west-2`, resolved through the standard
  credential chain. A test asserts no AWS key is even a Continuity setting.
- **GitHub App.** JWT → installation discovery → installation token →
  authorized-repo listing → metadata → file read. Unauthorized repos and
  excluded paths are refused. Continuity indexed **its own repository** through
  the real App: 143 files, 764 symbols, 7 routes, 1 secret path excluded.
- **Google OAuth.** Live authorization redirect whose `client_id` and
  `redirect_uri` match the registered client, plus every refusal path.

**Test suite has zero skips.** 474 backend tests, all running. The ad-hoc
verification scripts were converted into
`tests/integration/test_external_integrations.py` — a verification that ran once
by hand is an anecdote, not a verification.

**Incidental fixes this round, each a real defect:**
- A blank value in `.env` crashed startup on the first non-string field. Copying
  `.env.example` — the documented way to start — was therefore broken by
  default. Blank now means "unset", so the field default applies.
- `/health` reported a `bedrock` component after Gemini became primary. A health
  page naming a provider the system no longer uses is worse than none, so it is
  now `gemini`, propagated through the frontend and CI.
- Redaction learned the Gemini (`AQ.`), Google API (`AIza`), and Google OAuth
  (`GOCSPX-`) key shapes. It did not know them before, so those secrets would
  have passed through logs and activity events unredacted.
- `GitHubAppClient` had no installation *discovery* — it required an
  installation id it had no way to obtain. Added `mint_app_jwt` and
  `discover_installations` at module level, since discovery necessarily precedes
  knowing an installation id.
- PyJWT was used but undeclared; now `pyjwt[crypto]` is a real dependency.
- **The model-construction guard only covered Bedrock.** When Gemini became
  primary, the class carrying live traffic was the one left unguarded — an agent
  module could have constructed `GeminiModel` directly and no test would have
  noticed. Found at review. Fixed by parametrizing over every constructor *and*
  adding a rule that cannot go stale: no module except the provider may import
  from `strands.models` at all, so a provider added tomorrow is covered without
  anyone remembering to list it. Both forms are mutation-tested.

  Documented limitation: the pair catches *accidental* construction, not
  deliberate obfuscation (bare `import strands` plus `getattr` chains). The
  reviewer found that bypass and judged it a different threat class; it is now
  stated in the test docstring rather than left as an implicit gap.

**Do not accidentally change:**
- `build_model_provider`'s ordering. Gemini is primary; Bedrock is used only if
  Gemini is absent *and* Bedrock is present, so a fallback is explicit rather
  than accidental. If neither is configured it raises naming both.
- C2-07's nonce. Replacing it with a fixed string would let a model pass by
  guessing, and the test would silently stop proving tool use.
- The `requires_*` pytest markers. They are what let live tests skip honestly in
  CI without ever implying an integration works.

**Credential hygiene:** `.env` is mode 600 and gitignored. No secret value is
printed by any script, test, or log path in this change.

**Next intended task:** Phase 4, C4-01 — graph persistence and query layer.

### 2026-09-11 — Phase 4, Integration Intelligence Graph

**What was built:** the graph persistence and query layer, deterministic
extraction, the Integration Mapper agent pipeline, and baseline establishment.
Against the fixture repository a live Gemini run produces:

    acmepay (API v1)
      integration points : 4
      workflows          : Checkout, Subscription Renewal, Refund Processing,
                           AcmePay Webhook Handling
      tests              : tests/test_payments.py

That chain — `Provider → files → functions → workflows → tests → permissions` —
is the thing the whole product rests on, and it now exists end to end.

**The confirmed/inferred split is the phase's core property.** Extraction
produces `CONFIRMED` facts from manifests and the AST; the agent produces
`INFERRED` judgment. Confidence is fixed *by code* — `InferredWorkflow` has no
confidence field at all, so an agent cannot declare its own output confirmed —
and `ConfirmedNodeOverwrite` raises if inferred data would replace a confirmed
node. A model naming a symbol that does not exist has its edge dropped while its
workflow survives, because the workflow may still be right.

**Bugs found and fixed during the build, each real:**
- `blast_radius` traversed `COVERED_BY_TEST` in the wrong direction, silently
  returning *no tests* for every query. It looked like it worked while answering
  "nothing covers this" — which would have made every migration skip exactly the
  tests that matter.
- The baseline seeded only from call sites, so workflows reached only through a
  webhook handler were invisible — precisely the case a renamed webhook event
  breaks.
- Webhook event names live in comparisons (`event["type"] == "payment.paid"`),
  not call arguments, so the analyzer now records string literals with their
  line and enclosing symbol.
- Gemini could not produce `IntegrationMapperOutput` at all: its structured
  output is an OpenAPI subset with no free-form object keys, and the contract
  asked for a `dict[str, str]` plus a deeply-nested optional. Contracts now use
  flat evidence fields and lists of pairs. The agent is asked for judgment, not
  bookkeeping.
- Strands' default callback handler prints streaming model output — including
  reasoning — to stdout. `callback_handler=None` is now set on every agent, or
  §16 would have been violated on every single call.

**Review found three more:** a `blast_radius` docstring still describing the
traversal backwards (dangerous, given the bug above); a `provider_identities`
contract field that was never consumed or prompted for, claiming a capability
the code lacked; and "every node carries evidence" being true only by
convention. `evidence` is now a required field with no default, so the guarantee
is structural.

**Do not accidentally change:**
- `_FORWARD_EDGES` membership. `COVERED_BY_TEST` is written SYMBOL → TEST and
  must be traversed forwards.
- The rule that code, never the agent, sets `Confidence.INFERRED`.
- `NodeSpec.evidence` / `EdgeSpec.evidence` having no default.

**Next intended task:** Phase 5, C5-01 — the `ProviderAdapter` interface and
registry, then provider monitoring and the Change Scout.

---

### 2026-09-11 — Phase 5, provider monitoring and the Change Scout

**What was built:** The half of Continuity that notices things. `ProviderAdapter`
plus a registry (C5-01), content-addressed document storage (C5-02), a
deterministic OpenAPI differ (C5-03), the Change Scout (C5-04), and the
monitoring worker that drives them (C5-05).

The division of labour from `02_ARCHITECTURE.md` §10 is now real code: the differ
emits the 14 spec-derivable `ChangeType` members and physically cannot emit a
changelog-derived one — `_change()` raises if asked. The Change Scout emits only
the 4 changelog-derived members and drops the rest. Live Gemini finds all four
from a prose changelog, reproducibly.

**Verification:** All 8 gate checks pass. 656 backend tests (up from 516), 198
security, 17 frontend, 0 skips. Live Gemini proves the Change Scout against the
real model, and the Phase 5 live test was run five consecutive times before being
accepted, because the first version of it was flaky for a reason that turned out
to be a defect (below).

**Defects found by the review gate — all four were green before they were found:**

1. **A rename hid every field change that came with it.** `SpecDiffer.diff`
   popped a renamed pair from `removed`/`added` and emitted one
   `endpoint_renamed`; the operation-level diff then ran only over
   `set(old_ops) & set(new_ops)`, which the pair was no longer in. A provider
   that renamed a path *and* made a field required reported only the rename. The
   migration would have moved the path, missed the field, compiled, shipped, and
   400'd in production. Fixed by diffing the renamed pair, keyed to the new
   operation.

2. **Deduplication crashed on the second poll.** `_record_changes` called
   `session.add()` *outside* the savepoint, so a duplicate's failed flush left
   the insert pending on the outer session and every later operation raised
   `PendingRollbackError`. The savepoint existed, was reviewed, read correctly,
   and did nothing — polling an unchanged provider twice was exactly the case it
   was written for. Fixed by opening the savepoint before adding the row.

3. **`CHANGE_DETECTED` was logged but never written.** `_record_changes` mutated
   the `Project` it was handed, which may be attached to a different session.
   The transition record was correct and the project row never moved: a run that
   looked started and was not. Fixed by resolving the session-tracked instance.

4. **The Change Scout's anti-invention control admitted invented values.** It
   required the *model* to supply a `SourceRef`. Live Gemini returned
   `url="changelog"`, `document_hash="acmepay-v2-changelog"` — fabricated, and
   passing the check — while on other runs it returned an empty URL and four
   true findings were discarded. A control that accepts invention and rejects
   truth at random is worse than none, and it was nondeterministic, so it looked
   fine roughly half the time.

   Replaced with `evidence_quote`: the model must quote a sentence, and code
   checks that sentence against the document Continuity actually fetched. The
   `SourceRef` is now bound from the fetch, where it is a fact rather than a
   claim. This is the C5-04 criterion "the agent cannot introduce a change absent
   from the changelog" implemented rather than approximated. Amendment recorded
   in `02_ARCHITECTURE.md`.

   Related, found by the same flaky live test: the model's `injection_suspected`
   was OR-ed into the authoritative security flag, and live Gemini raises it on a
   completely benign changelog on roughly a quarter of runs. A security flag that
   fires on ordinary provider releases is one nobody reads. It is now recorded
   separately as `model_reported_injection` — advisory, logged, and unable to
   mark a document hostile on its own. The deterministic detection is
   authoritative because it can quote what it found.

**Found by the reviewer:** `ChangeType.spec_derivable()` was dead code, and the
enum docstring claimed the two halves were "asserted disjoint and complete by
test" when nothing asserted it — both test files re-derived the set by exclusion
instead. A member added to the enum and to neither half would have been silently
un-emittable by the differ and silently un-acceptable from the Scout, with every
test green. Now asserted in `tests/unit/models/test_enums.py`, and both call
sites use the method.

**What is NOT built:** no scheduler. See B-06. `monitor_project()` is a callable;
nothing calls it periodically. Its independence from the API layer is proven; its
periodic invocation does not exist.

**Do not accidentally change:**
- `_change()`'s changelog-derived guard in `diff.py`, or the differ silently
  becomes a second, unattributable source of changelog claims.
- The savepoint opening *before* `session.add()` in `_record_changes`.
- `evidence_quote` being checked against the fetched document. Asking the model
  for provenance instead was tried, and it fabricated it.
- `injection_suspected` being deterministic-only.
- The `ChangeEvent` unique constraint — it is the dedup key, enforced by the
  database rather than by application logic.

**Next intended task:** Phase 6, C6-01 — `ExecutionProvider`, argv-only and
allowlisted, then impact analysis and migration rehearsal.

---

### 2026-09-11 — Phase 6, execution safety, impact analysis, rehearsal

**What was built:** The half of Continuity that decides whether a change matters
here, and proves it before touching anything. `ExecutionProvider` (C6-01),
deterministic change-to-graph correlation (C6-02), the Impact Analyst (C6-03),
and the rehearsal harness (C6-04).

The selectivity is the product. On the fixture release, nine of twelve changes
reach no code at all — settled by correlation, with **no model call**, because
there is no judgment to make about a change that touches no line of the
repository. Only the three that reach code are judged, and only what warrants a
migration opens a run. That ratio is asserted in model calls and in
`migration_runs` row counts, not described.

**Verification:** All 8 gate checks pass. 799 backend tests (up from 656), 271
security, 17 frontend, 0 skips. The execution tests spawn real processes and
assert real kills; the rehearsal tests run real pytest and compare real pass
counts. Live Gemini judges the real change set, run three times before being
accepted.

**Defects found during implementation:**

1. **Boundary violation, caught by a Phase 2 security test.** I put migration-run
   creation and state transitions inside `backend/agents/impact_analyst.py`.
   `test_no_agent_module_can_reach_the_state_machine` failed on the import:
   only the coordinator moves runs. The module was split — judgment stayed in
   `backend/agents/impact_analyst.py`, orchestration moved to
   `backend/orchestration/impact.py`. The rule was written four phases ago and
   it is what caught this.

2. **The rehearsal skipped a state.** `rehearse()` moved a run straight from
   `REHEARSAL_PENDING` to a terminal rehearsal state. `ALLOWED_TRANSITIONS` has
   no such edge, and the transition was silently declined — the run sat in
   PENDING as though the rehearsal had never started, and the next move then
   raised `IllegalTransition`. The state machine was right: the run now enters
   `REHEARSAL_RUNNING` before anything executes, so a crash mid-rehearsal is
   visibly stuck rather than invisible.

3. **A racy process-death assertion.** `os.kill(pid, 0)` succeeds on a zombie
   until its parent reaps it, and the parent here is the process just killed.
   Sampling once passed alone and failed inside the full suite. Now polled with
   a bounded wait — the property under test is "the grandchild does not
   survive", and that is what is now asserted.

4. **A shell scan that matched its own prose.** The first version of
   `test_no_module_anywhere_can_spawn_a_shell` grepped source text for
   `shell=True`, `os.system`, and friends — and failed on `execution.py`, whose
   docstring names every one of them. Rewritten to parse the AST and match real
   calls, with a second test that feeds it planted code so the scan cannot
   silently stop detecting anything.

**Contract amendment:** `ImpactAnalystInput.change` was a `ScoutedChange`, which
after Phase 5 requires an `evidence_quote`. Most changes reaching this agent come
from the deterministic differ, where there is no changelog sentence to quote —
so a spec-derived change was unrepresentable. Replaced with `AnalyzedChange`,
carrying the facts both sources share. `ImpactAnalystOutput` also lost its
`Evidence` field: affected items come from graph nodes, which carry the
extractor's CONFIRMED evidence, and an `Evidence` object a model assembles is an
assertion about a file rather than a record of having read it.

**What is NOT built:**
- No provider simulator. `NoSimulationAdapter` reports that it cannot simulate,
  producing `REHEARSAL_UNAVAILABLE`. It does not run the same suite twice and
  call the inevitable non-difference a result. Simulators plug in through
  `RehearsalAdapter` and are out of scope here (`CLAUDE.md` §3.13).
- No TEE. `DevelopmentIsolatedExecutor` is **process isolation, not sandboxing**
  — it confines paths, strips credentials, and bounds time and output. It does
  not contain a process that escapes the kernel's boundaries. See B-04.
- Nothing wires monitoring → correlation → impact → rehearsal into one pass yet.
  Each stage is tested end to end on its own inputs; the run pipeline that calls
  them in sequence arrives with the Migration Engineer in Phase 7.

**Do not accidentally change:**
- `argv[0]` rejecting path separators. Without it, `/tmp/attacker/pytest` is an
  allowlisted basename pointing at anything.
- The output reader draining past the cap. Stopping at the cap deadlocks the
  child on its next write, and it presents as a timeout — which reads as the
  repository's fault.
- `start_new_session=True` and the `killpg` on timeout.
- The empty-correlation short-circuit in `assess_change`. It is the difference
  between one model call per release and twelve.
- The `relevant and migration_required` conjunction in code. "Irrelevant, but
  migrate" must not be able to open a run.
- The absence of an edge from `REHEARSAL_FAILED` to `MIGRATION_PENDING`.

**Next intended task:** Phase 7, C7-01 — the Migration Engineer and the bounded
repair loop.

---

### 2026-09-11 — Phase 7, Migration Engineer and the bounded repair loop

**What was built:** The part that actually changes code. Isolated migration
workspaces (C7-01), the Migration Engineer and the rules its patches must
survive (C7-02), deterministic test discovery, execution, and parsing (C7-03),
and the bounded repair loop (C7-04).

**The isolation is real.** Each run gets a `git worktree --detach` at a pinned
commit. The user's checkout is byte-identical afterwards, asserted by a
recursive hash over content *and* layout, and `git status` in the source repo
stays clean. Writes resolve through the workspace root and are refused
otherwise, including through a symlink planted inside it.

**Verification:** All 8 gate checks pass. 925 backend tests (up from 799), 277
security, 17 frontend, 0 skips. Live Gemini writes a real patch against a real
project and the loop repairs it end to end — run **15 consecutive times with no
failures** before being accepted, because the first version of it failed roughly
one run in six.

**Defects found during implementation:**

1. **The Migration Engineer was never told what changed.** `MigrationEngineerInput`
   carried the impact — which files, which symbols — and not the provider change
   itself. Live Gemini refused, correctly: *"Cannot proceed without acmepay v2
   contract details."* The agent was being told where to edit and never what to
   edit for. Found only because the live test ran; every scripted test passed.

2. **A model failure crashed the whole run.** An exception from the engineer
   propagated out of the loop, abandoning the migration with the workspace
   half-patched, no attempt row explaining why, and the budget bypassed entirely
   — nothing recorded means nothing spent. Now a spent attempt, retried.

3. **An empty patch escalated on the first occurrence.** Gemini intermittently
   returns a patch with no applicable edit. Stopping the run there left two
   attempts unspent, and the budget exists for exactly this transient failure.
   This was the cause of a live failure rate of roughly one run in six.

4. **Rejected edits were never fed back.** The next attempt learned only that
   tests failed, not that its edits had been discarded and why — so it could
   propose the same rejected edit until the budget was gone. Rejections are
   evidence, and this loop is supposed to respond to evidence.

5. **A discarded edit halted the run.** An out-of-scope edit raised an ASK
   finding, which blocked. But the edit never reached disk — there was nothing
   for a human to approve. Now recorded and fed back without blocking. A
   credential (DENY), a new dependency or a test change (ASK) still block,
   because those are real decisions.

6. **The engineer could not see its callers.** The impact set is `app/client.py`;
   the test calling `charge(100)` was not provided. Asked to fix a `TypeError`
   in a call it could not read, Gemini made `currency` a required positional
   argument — correct in isolation, and it broke every existing caller. The
   tests covering the impacted code are now supplied as explicitly read-only
   context.

7. **The test command suppressed its own output.** Discovery passed `-q`, and a
   project whose `addopts` already carries it gets `-q -q` — which suppresses
   pytest's summary line entirely, leaving a suite that ran perfectly with no
   counts to parse. Verbosity is the project's choice; the summary line is not
   optional for us.

8. **The rehearsal-shaped state error, again.** `PATCH_READY` is a first-pass
   state; `ALLOWED_TRANSITIONS` has no `repair_running -> patch_ready` edge. A
   retry patches *inside* `REPAIR_RUNNING`. The state machine was right and the
   loop was wrong.

**A Phase 1 test also caught** two new settings added without documenting them
in `.env.example`.

**Contract amendments:** `FileEdit` gained `out_of_impact_justification` —
`justification` is required on every edit, so it could not be what marks an edit
as out of scope, and the acceptance criterion would have been vacuous. Every
optional field on `MigrationEngineerOutput` and `FileEdit` is `""` rather than
`None`: Gemini's structured-output schema is an OpenAPI subset that rejects an
optional nested inside a list, and asking for one made the model fail to produce
any output at all — the same failure `Evidence` caused in C4-03.

**What is NOT built:**
- Nothing wires monitoring → correlation → impact → rehearsal → migration into
  one pass. Each stage is tested end to end on its own inputs.
- No security review of the finished patch, no branch, no pull request. Phase 8.
- Test-command discovery covers pytest, vitest, and jest. A project using
  anything else raises `TestCommandNotFound` rather than guessing.

**Do not accidentally change:**
- `store_result` taking a `ParsedTestResult`. It is what makes "no code path can
  populate `test_results` from a model" structural rather than a convention.
- Discovery raising instead of defaulting to a command, and parsing raising
  instead of returning zeroes. "We could not check" must never read as a pass.
- The empty-patch branch not running the suite. Validating an unchanged
  workspace reports a pass and calls the migration done.
- Attempts counted from rows rather than held in memory.
- `git worktree` being allowlisted only for `add`, `remove`, `prune`, `list`.

**Next intended task:** Phase 8, C8-01 — the Security Reviewer, the approval
gate, and branch-and-PR delivery.
