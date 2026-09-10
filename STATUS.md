# STATUS

**This file is authoritative.** If any other document, README badge, or commit
message disagrees with it, this file is right and the other is stale.

---

## Overall completion

**40%**

Phases 0–3 complete. External integrations are now configured and **verified
live** (2026-09-11): the primary model moved to Google Gemini through Strands,
and AWS, the GitHub App, and Google OAuth are all proven working. Three of four
blockers are resolved; the test suite has **zero skips**.

| Field | Value |
| --- | --- |
| Current phase | Phase 4 — Integration Mapper + Intelligence Graph |
| Current ticket | C4-01 — Graph persistence and query layer |
| Last updated | 2026-09-11 |

---

## Phase progress

| Phase | Scope | Target | Status |
| --- | --- | --- | --- |
| 0 | Project anchoring | 10% | **DONE** — reviewer PASS |
| 1 | Application + backend foundation | 20% | **DONE** — reviewer PASS |
| 2 | Strands + core orchestration | 30% | **DONE** — reviewer PASS |
| 3 | GitHub + safe repository ingestion | 40% | **DONE** — reviewer PASS |
| 4 | Integration Mapper + Intelligence Graph | 50% | IN PROGRESS |
| 5 | Provider monitoring + Change Scout | 60% | PENDING |
| 6 | Execution safety, impact analysis, rehearsal | 70% | PENDING |
| 7 | Migration Engineer + repair loop | 80% | PENDING |
| 8 | Security + approval + GitHub PR | 90% | PENDING |
| 9 | Production security + frontend + polish | 100% | PENDING |

---

## Tickets

**Completed:** C0-01 … C0-04, C1-01 … C1-07, C2-01 … C2-08, C3-01 … C3-06
**In progress:** none
**Next:** C4-01 — Graph persistence and query layer
**Pending:** C4-01 onward — see `05_FEATURE_TICKETS.md`

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

---

## Tests

| Suite | Command | State |
| --- | --- | --- |
| Python unit + integration | `uv run pytest` | **474 passing, 0 skipped** |
| Security | `uv run pytest tests/security` | **145 passing** |
| Frontend unit | `npm run test` | **17 passing** |
| E2E | `npm run test:e2e` | Not yet created (Phase 9) |
| Lint (py) | `uv run ruff check .` | **Passing** |
| Typecheck (py) | `uv run mypy backend` | **Passing** (52 source files) |
| Lint (web) | `npm run lint` | **Passing** |
| Typecheck (web) | `npm run typecheck` | **Passing** |
| Build (web) | `npm run build` | **Passing** — routes `/`, `/signin` |
| Migrations | `uv run alembic check` | **In sync** with the models |
| Live integrations | `uv run pytest tests/integration/test_external_integrations.py` | **9 passing** — AWS, GitHub App, Google OAuth |
| Live Strands + Gemini | `uv run pytest tests/integration/test_strands_roundtrip.py` | **3 passing** — real tool call proven |

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
| Confidential execution / TEE | Abstraction only — see B-04. **Not attested** |
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
| Latest commit | `88e3e77` — feat: Google Gemini as primary model; external integrations verified live |
| Push state | Pushed to `origin/main` successfully |

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
