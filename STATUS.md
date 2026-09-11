# STATUS

**This file is authoritative.** If any other document, README badge, or commit
message disagrees with it, this file is right and the other is stale.

---

## Overall completion

Two numbers, because one of them hid a real gap before and must not again.

| Measure | Value | What it means |
| --- | --- | --- |
| **Tickets delivered** | **91%** | C0-01 … C8-06, C9-01 and C9-02 are done and reviewer-passed. Four Phase 9 tickets remain. |
| **End-to-end readiness** | **works, unattended** | A real repository can be imported through the UI and reach a pull request with no database seeding and no human in the loop except where policy demands one. |

Phases 0–8 are complete, and the loop between them is now closed. Continuity
reviews its own patch and asks a human when it must. Thirteen finding categories
are detected — most by code rather than by a model — and the policy engine, not
the reviewer, decides; a run whose tests pass still stops at
`SECURITY_REVIEW_PASSED` or `APPROVAL_PENDING` depending on what the review
found. Approving is something only an authenticated person can do, re-checked at
the instant it is relied on rather than trusted from earlier in the run.

**The whole product runs as one loop.** Connect → import → scan → map → baseline
→ monitor → correlate → assess → rehearse → migrate → repair → security review →
approval gate → deliver → merge → post-merge verification → baseline advance.
`backend/orchestration/onboarding.py` builds the graph a new project needs;
`backend/orchestration/pipeline.py` runs a pass over it;
`backend/orchestration/resume.py` picks a paused run back up after a person
answers; `backend/workers/post_merge.py` closes the loop after a merge and only
then moves the baseline forward. `backend/workers/scheduler.py` drives all of it
on an interval, started by the application itself. The UI at `/connect`,
`/projects`, and `/approvals` observes and controls it, every figure read from a
stored record. Test suite has **zero skips**.

| Field | Value |
| --- | --- |
| Current phase | Phase 9 — Production security + frontend + polish |
| Current ticket | C9-05 (evaluation harness) — next. C9-03 (TEE) is deferred by the project owner. |
| Last updated | 2026-09-12 |

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
| 8 | Security + approval + GitHub PR | 90% | **DONE** — reviewer PASS |
| 9 | Production security + frontend + polish | 100% | PENDING |

---

## Tickets

**Completed:** C0-01 … C0-04, C1-01 … C1-07, C2-01 … C2-08, C3-01 … C3-06, C4-01 … C4-04, C5-01 … C5-05, C6-01 … C6-04, C7-01 … C7-04, C8-01 … C8-06, C9-01, C9-02
**In progress:** none
**Next:** C9-05 — see `05_FEATURE_TICKETS.md`
**Pending:** C9-03 (deferred), C9-04, C9-05, C9-06

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

### B-04 · No TEE / Nitro Enclave infrastructure — **OPEN**

**Corrected 2026-09-11.** This entry previously said "the
`ConfidentialExecutionProvider` abstraction is built" and "the UI reports
`TEE Attestation: Not Configured`". **Both were false**, and they contradicted
the Security state table below, which has said "Not built" since Phase 6. The
entry was written in Phase 0 describing the intended handling and was never
corrected as the phases passed. Caught by grepping the codebase for the thing it
claimed existed.

**What actually exists today:**

| Claim | Reality |
| --- | --- |
| `ConfidentialExecutionProvider` protocol | **Does not exist.** No such name anywhere in `backend/` |
| `AttestationDocument` | **Does not exist** |
| `backend/security/attestation.py` | **Does not exist** |
| A UI reporting attestation state | **Does not exist** — the frontend is three components |
| `DevelopmentIsolatedExecutor` | **Exists and works** (C6-01). Process isolation, not sandboxing, and it says so |

**Impact:** C9-03 is entirely unstarted, not partly done. It has to build the
abstraction *and* the honesty guarantees, not just wire up hardware.

**What is true and matters:** nothing anywhere claims attestation, because
nothing mentions attestation at all. The honesty rule in
`03_SECURITY_ACCESS.md` §7 is not violated — it is simply not yet exercised.

**Handling:** C9-03 builds `ConfidentialExecutionProvider`,
`backend/security/attestation.py`, and the test that asserts no code path can
report an attested state without a verified document. Hardware-backed
attestation on AWS Nitro Enclaves requires an EC2 instance type with enclave
support (`m5.xlarge` or larger with `--enclave-options Enabled`), the
`nitro-cli` toolchain, and a KMS key policy conditioned on the enclave's PCR
measurements. None of that is provisioned, and it is not a prerequisite for the
abstraction or the honesty tests.

### B-05 · AgentCore reachable but not provisioned — **OPEN**, deliberate

Verified live in Phase 8 against account `726583840385` in `us-west-2`. All five
probed services answer their control-plane API with these credentials and hold
**zero resources**:

| Service | State |
| --- | --- |
| Runtime | reachable, not provisioned |
| Identity | reachable, not provisioned |
| Gateway | reachable, not provisioned |
| Memory | reachable, not provisioned |
| Observability (CloudWatch) | reachable, not provisioned |

So `integrated_services` is empty, and **nothing in the UI, the README, or the
evidence report claims AgentCore integration**. `backend/observability/agentcore.py`
reports these three states from live calls and never rounds "reachable" up to
"integrated".

This is not a blocked prerequisite — the permissions are in place. Provisioning
any of these creates billable AWS resources, which is the account owner's
decision rather than something Continuity should do on its own. Exact setup
steps live in `SETUP_STEPS` in that module, beside the probe that reports each
service missing, so they cannot drift. In short:

- **Runtime** — build and push a container image to ECR, create an execution
  role trusting `bedrock-agentcore.amazonaws.com`, then
  `aws bedrock-agentcore-control create-agent-runtime`.
- **Identity** — `aws bedrock-agentcore-control create-workload-identity
  --name continuity-provider-tokens`, then move credentials out of `.env`.
- **Gateway** — `create-gateway --protocol-type MCP`, and only if it
  demonstrably enforces something `backend/security/policy.py` does not.
- **Memory** — `create-memory`, Phase 9 and only once approvals are in use.
- **Observability** — enable Transaction Search, then point an OTEL exporter at
  CloudWatch; log groups appear with the runtime's first span.

### B-06 · No scheduler process — **RESOLVED** (2026-09-11)

`backend/workers/scheduler.py` is the loop that was missing. It sweeps every
project on an interval and drives `run_pipeline` for each, with the properties
an unattended loop needs: it does not overlap itself for one project, one
project's failure does not stop the sweep, a failing tick does not kill the
loop, and shutdown waits for the pass in flight rather than orphaning a worktree
and its child processes. 18 tests.

**The application now starts it.** `backend/api/app.py`'s lifespan builds the
scheduler through `backend/workers/runner.py` and calls `start()` on startup and
`stop()` on shutdown, gated on `SCHEDULER_ENABLED` and on a real Gemini
configuration — so a deployment with no model credentials serves the API without
pretending to monitor anything. `test_the_application_starts_the_scheduler`
asserts this through the real ASGI lifespan rather than by calling `start()`
itself. Production may still replace the in-process loop with SQS, Step
Functions, or AgentCore Runtime without changing `run_pipeline`.

---

## Tests

| Suite | Command | State |
| --- | --- | --- |
| Python unit + integration | `uv run pytest` | **1269 passing, 0 skipped** |
| Security | `uv run pytest tests/security` | **342 passing** |
| Red Team | `uv run pytest tests/unit/security/test_red_team.py` | **38 passing** — one landing and one non-landing fixture per attack class |
| Release verification | `uv run pytest tests/unit/verification tests/integration/test_release_verification.py` | **44 passing** — manifest validation, the SSRF guard, and the before/after comparison |
| Frontend unit | `npm run test` | **47 passing** |
| Product loop (HTTP API in, pull request out) | `uv run pytest tests/integration/test_product_loop.py` | **27 passing** |
| E2E | `npm run test:e2e` | Not yet created (Phase 9) |
| Lint (py) | `uv run ruff check .` | **Passing** |
| Typecheck (py) | `uv run mypy backend` | **Passing** (99 source files) |
| Lint (web) | `npm run lint` | **Passing** |
| Typecheck (web) | `npm run typecheck` | **Passing** |
| Build (web) | `npm run build` | **Passing** — `/`, `/signin`, `/connect`, `/projects`, `/projects/[id]`, `/approvals` |
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
| Security review | `uv run pytest tests/unit/security` | **62 passing** — one fixture diff per finding category |
| Approval integrity | `uv run pytest tests/security/test_approval_integrity.py` | **16 passing** — self-approval, revoke race, restart |
| Webhook integrity | `uv run pytest tests/security/test_webhook_integrity.py` | **24 passing** — signature, replay, polling fallback |
| Live AgentCore probe | `uv run pytest tests/integration/test_agentcore.py` | **8 passing** — real AWS, all five services reachable, none provisioned |

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
| AgentCore Runtime | **Reachable, not provisioned** — verified live in C8-05, see B-05 |
| AgentCore Observability | **Reachable, not provisioned** — verified live in C8-05 |
| AgentCore Identity | **Reachable, not provisioned** — verified live in C8-05 |
| AgentCore Gateway/Policy | **Reachable, not provisioned**; adoption still conditional on adding enforcement `policy.py` lacks (§17) |

Nothing above is claimed as working beyond what the live tests prove.

---

## Security state

| Control | State |
| --- | --- |
| Secret filtering | **Implemented** — path exclusion, content redaction, and repository `.gitignore` rules, at all five enforcement points |
| Policy engine (ALLOW/ASK/DENY) | **Implemented** — `backend/security/policy.py`, matrix parity-tested against §4 |
| Approval integrity | **Implemented** (C2-08 state, C8-02 HTTP surface and enforcement gate) — self-approval, the revoke-between-grant-and-execute race, and restart survival are all tested |
| Repository boundary | **Implemented** — normalize-then-resolve, symlink-safe; one conformance suite over both sources |
| Untrusted external content | **Implemented** — C5-04 changelog containment, C8-06 webhook payloads treated as pointers rather than state |
| ExecutionProvider | **Implemented** (C6-01) — argv-only, allowlisted, path-confined, credential-free environment |
| Security review of the patch | **Implemented** (C8-01) — 13 finding categories, policy decides, disagreements persisted |
| Confidential execution / TEE | **Not built.** `ExecutionProvider` exists and is process isolation, not sandboxing. No `ConfidentialExecutionProvider`, no attestation — see B-04 |
| Committed secrets | None. Verified against the staged diff at each gate |

---

## GitHub integration state

| Item | State |
| --- | --- |
| GitHub App | **Installed and verified live** — `Continuity Integration Agent`, app id 4900912 |
| Repository authorization | **Verified** — `tony19053000/continuity` only; others refused |
| Branch/PR delivery | **Implemented** (C8-03) — branch-and-PR only; no force push, merge, or default-branch write exists to call |
| Merge detection | **Implemented** (C8-06) — webhook receiver + polling fallback. Webhooks are **disabled**: no signing secret is set, so every delivery is refused and audited. Polling is the active path |
| Local `gh` CLI | Authenticated as `tony19053000` |
| Remote `origin` | `https://github.com/tony19053000/continuity.git` |

---

## Git

| Field | Value |
| --- | --- |
| Branch | `main` (tracking `origin/main`) |
| Latest commit | `fa941e1` — feat: security review, approval enforcement, and PR delivery |
| Push state | **Up to date.** `origin/main` is at `ac3fcfa` (C9-01). The backlog that had built up while the session's permission gate refused `git push` — Phase 8, the pipeline wiring, the UI, the product loop — went up with it on 2026-09-12. |

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

---

### 2026-09-11 — Phase 8, security review, approval, and delivery

**What was built:** The part that decides whether the patch should exist, asks a
person when it must, and delivers it the only safe way there is. The Security
Reviewer (C8-01), the approval API and its enforcement gate (C8-02), branch-and-PR
delivery (C8-03), the evidence report (C8-04), a live AgentCore probe (C8-05),
and merge detection (C8-06).

**The security review is two halves, and the split is the point.** Code detects
every category it can decide — a credential is a regex match, a removed
signature check is a token that was there and is not any more, a file outside
the impact set is arithmetic — and runs whether or not a model is reachable, so
the floor does not depend on one being available. The agent adds what it read.
Neither decides: `backend/security/policy.py` maps each category to an action
and classifies it, and the agent's recommendation is stored *beside* the binding
decision precisely so a disagreement is visible.

All 13 finding categories have a fixture diff, parametrized over the enum rather
than a hand-written list — a member added later fails the module until someone
decides what it costs.

**Verification:** All 8 gate checks pass. 1083 backend tests (up from 925), 322
security, 17 frontend, **0 skips**. The AgentCore probe runs against real AWS.

**Defects found during implementation:**

1. **A refused webhook delivery left no audit trail.** The endpoint wrote the
   audit row on the request's session and then raised a 401 — and the request
   transaction rolls back on the way out, discarding it. Auditing refusals is
   most of the point of auditing an unauthenticated endpoint, so the row now
   commits in its own transaction.

2. **A decision body could name its own approver and be silently ignored.** The
   field was never read, so it was safe — but a caller sending `actor_user_id`
   believes they attributed the decision to someone, and on this endpoint that
   misunderstanding matters. The schema now refuses unknown fields.

3. **Delivery skipped two states.** `PR_PENDING → PR_CREATING → PR_CREATED →
   MERGE_WAITING`, and I had it jumping straight to the end. The state machine
   declined the illegal move silently, exactly as it did for the rehearsal in
   Phase 6 and the repair loop in Phase 7.

4. **`..` survived branch-name slugging.** Git refuses such refs, so nothing
   would have broken — but relying on GitHub to reject a name Continuity built
   from an external document is the wrong place for that check.

5. **The AgentCore probe ignored the configured AWS profile.** It fell back to
   the ambient chain, so all eight tests skipped under `scripts/verify.sh` while
   passing for anyone who happened to have the profile exported — and the suite
   quietly stopped having zero skips.

6. **`boto3` was only a transitive dependency.** Continuity imports it directly
   now; a transitive dependency can disappear in a minor release of its parent.

**A Phase 3 security test had to be amended, carefully.** It asserted the GitHub
client was read-only, which it was until delivery existed. The forbidden list is
unchanged — force-push, merge, delete, rewrite, reset, squash, rebase are still
absent by construction — and the six writes delivery needs are now pinned one by
one. `update_branch` sends no `force`, so GitHub refuses a non-fast-forward
update and a failed migration cannot overwrite a reviewer's commit.

**On AgentCore, plainly:** all five services are reachable and **none are
provisioned**. That is verified by live API calls, not assumed, and
`integrated_services` is empty — so nothing anywhere claims AgentCore
integration. The permissions are in place; provisioning creates billable AWS
resources, which is the account owner's call. See B-05 for the exact commands.

**On webhooks, plainly:** the receiver is implemented and tested, and webhooks
are **off** — no signing secret is configured, so every delivery is refused with
a 401 and audited. Accepting unsigned deliveries would let anyone who can reach
the URL advance a migration run. Merge detection therefore runs through the
polling fallback.

**Found by the reviewer, and fixed:**

- **`update_branch` used POST where GitHub requires PATCH.** POST to
  `/git/refs` creates a ref; PATCH to `/git/refs/{ref}` updates one, and POSTing
  to the update path is not an endpoint at all. Delivery would have created a
  branch and a commit against real GitHub and then silently failed to attach one
  to the other — while every mocked test passed. Exactly the class of defect a
  fake API hides. Fixed with a `_patch` helper and a regression test that reads
  the parsed code rather than the docstring.

- **The Security Reviewer was never invoked by a run.** It existed, it was well
  tested, and nothing called it — so `recommendation` and `policy_decision` were
  always written equal in the live path, and the "disagreement is persisted"
  property held only inside a unit test. `review_patch` now runs in the repair
  loop the moment a suite goes green, and a test drives a real run in which the
  agent says allow, policy says deny, and both come back out of the row.

**What is NOT built:**
- Nothing wires the stages into one continuous pass, and in particular nothing
  calls `deliver()`. `SECURITY_REVIEW_PASSED` is where a successful run now
  stops. Each stage is tested end to end on its own inputs; the pipeline is
  Phase 9.
- No frontend for approvals, findings, or the evidence report — the API exists,
  the pages do not.
- Partial delivery is not recovered. If GitHub creates the branch and the pull
  request call then fails, the transaction rolls back while the branch remains.
  The next attempt finds the ref present and refuses rather than reusing it,
  because a branch that already exists may carry someone else's commits.

**Do not accidentally change:**
- `classify_category` returning DENY for an unmapped category.
- The deterministic detectors running before, and independently of, the agent.
- `require_granted` re-reading the approval row. The revoke-between-grant-and-
  execute race is a real test, and it only passes because of that re-read.
- `hmac.compare_digest` in `signature_matches`.
- The audit row for a refused delivery committing in its own transaction.
- `settle()` being the single place a merge outcome becomes a state change.

**Next intended task:** Phase 9 — production security hardening, the frontend,
and the end-to-end pipeline that connects every stage built so far.

---

### 2026-09-11 — End-to-end: pipeline, scheduler, and the operating UI

**Why this came before Phase 9's capabilities.** Continuity was nine components
that each worked alone and a frontend of three files. Adding a Red-Team agent to
that would have made a better collection of parts, not a better product. The
project owner's call, and the right one.

**What was built:**

- **`backend/orchestration/pipeline.py`** — monitor → correlate → assess →
  rehearse → migrate → repair → security review → approval gate → deliver, in
  one call. Written as a sequence of refusals rather than a happy path: most
  provider releases reach no code, and a run ending at `CHANGE_IRRELEVANT` has
  succeeded. It chooses among moves the state machine and the policy engine
  permit and decides nothing itself — if this file ever starts re-deriving
  relevance or re-checking approvals, a stage's seam is in the wrong place.

- **`backend/workers/scheduler.py`** — closes B-06. An in-process asyncio loop
  that sweeps every project on an interval. It does not overlap itself for one
  project, one project's failure does not stop the sweep, a bad tick does not
  kill the loop, and shutdown waits for the pass in flight rather than orphaning
  a worktree and its child processes.

- **The read API and `IntegrationHealth`** — projects, integrations, changes,
  runs, activity, the graph, findings, and the evidence report. The health score
  is §18's published formula computed from rows, returned with its inputs.

- **The UI** — Projects, Overview, Integrations, Graph, Changes, Runs with the
  migration report, Approvals, and Security. Every figure read from a stored
  record.

**Verified against the running system**, not only in tests. The API serves on
:8000 and refuses unauthenticated reads; the UI renders the seeded project's
real values and declines to score it, saying why, because it has never been
scanned. The security page reports Development Isolation and
`TEE attestation: Not Configured`.

**Defects found:**

1. **The findings endpoint returned an unredacted secret.** The Security
   Reviewer redacts what it writes, but a read endpoint is the last stop before
   a browser and must not depend on every writer having remembered. Free text is
   now filtered on the way out as well as in.

2. **A Phase 8 test broke for an honest reason.** It asserted `settle()` is the
   only place a merge becomes a state change, by matching any mention of
   `VERIFIED` — and the scheduler now reads that state to decide which projects
   are idle. Narrowed to modules that both name the state and call `transition`,
   which is the property it was always trying to express.

3. **A test assertion, not the code, was wrong twice.** The pipeline's specific
   stop reason was being overwritten by the generic one (real, fixed), and a
   frontend assertion matched the "0 pending approvals" stat while checking that
   no health placeholder was rendered (test fixed; the screen was right).

**What is NOT built:**
- Phase 9's capabilities: Red-Team (C9-01), Release Guardian (C9-02),
  confidential execution (C9-03, and B-04 now describes accurately what does not
  exist), the evaluation harness (C9-05), and the final gate (C9-06).
- Repository import and scan-progress screens. The pipeline assumes a scanned
  project; onboarding one through the UI is not yet possible.
- Nothing starts the scheduler automatically. A deployment wires `start()` into
  its own lifecycle.
- No Playwright E2E. The end-to-end proof is
  `tests/integration/test_pipeline.py`, which drives the real thing.

**Do not accidentally change:**
- The pipeline returning rather than raising for ordinary stops. A caller that
  had to catch exceptions to tell "nothing was affected" from "the run failed"
  would treat both as failures.
- `_assess` setting a specific stop reason, and the caller not overwriting it.
- The scheduler's in-flight set, and `stop()` awaiting the pass.
- `IntegrationHealth.available` being false rather than a score of 0.
- `lib/noMocks.test.ts`, and its two tests that feed the matchers planted code.

**Next intended task:** Phase 9 proper — C9-01 Red-Team, then C9-02 Release
Guardian, C9-05 evaluation, and C9-06 the final gate. C9-03 (TEE) stays deferred
at the owner's direction.

---

### 2026-09-11 — End-to-end: one pipeline, a scheduler, and an operating UI

**Why this came before Phase 9's remaining tickets.** Continuity had nine stages
that each worked and nothing that connected them. A Red-Team agent guarding a
pipeline nobody could run would have been the wrong thing to build next, so the
product was made to work end to end first, on the project owner's instruction.
TEE (C9-03) is explicitly deferred.

**What was built:**

- `backend/orchestration/pipeline.py` — monitor → correlate → assess → rehearse
  → migrate → repair → security review → approval gate → deliver, as one call.
  It is a sequence of refusals rather than a happy path: most provider releases
  reach no code, and a run ending at `CHANGE_IRRELEVANT` has succeeded. No stage
  is skipped because the previous one was confident — the gates belong to the
  state machine and `backend/security/policy.py`, and the pipeline chooses among
  moves they permit rather than deciding anything itself.

- `backend/workers/scheduler.py` — closes B-06. Sweeps every project on an
  interval. It does not overlap itself for one project, one project's failure
  does not stop the sweep, a bad tick does not kill the loop, and shutdown waits
  for the pass in flight rather than orphaning a worktree and its children.

- `backend/api/routers/projects.py`, `backend/api/health_score.py` — the read
  surface. The Integration Health score is the published §18 formula computed
  from rows, returned with its inputs. A project with no inputs reports
  `available: false`.

- `apps/web` — Projects, Overview, Integrations, Graph, Changes, Runs and the
  migration report, Approvals, and Security. Every value read from the API.

**Verified against the running system, not only in tests.** The API served on
:8000, refused unauthenticated reads, and the UI rendered the seeded project's
real values — including declining to score it, because it has never been
scanned, and reporting `TEE attestation: Not Configured`.

**Defects found:**

1. **The findings endpoint returned an unredacted secret.** The Security
   Reviewer redacts what it writes, but a read endpoint is the last stop before
   a browser and must not depend on every writer having remembered. Free text is
   now filtered on the way out as well as in.

2. **A Phase 8 structural test was too broad.** It asserted `settle()` is the
   only place a merge becomes a state change by matching any mention of
   `VERIFIED` — which the scheduler now legitimately reads to decide which
   projects are idle. It now matches modules that both name the state *and* call
   `transition`, which is the property it always meant.

3. **The generic stop reason overwrote the specific one.** A project with no
   integration graph reported "no change affects this project" instead of "scan
   it first" — telling an operator the wrong thing about why nothing happened.

**What is NOT built:**
- C9-01 Red-Team, C9-02 Release Guardian, C9-03 confidential execution, C9-05
  evaluation harness, C9-06 final gate.
- Nothing *starts* the scheduler automatically. A deployment wires `start()` into
  its own lifecycle, because how and where it runs is a deployment decision.
- Sign-in, GitHub connection, and repository import screens (§3.1–§3.4). The
  backend supports them; the pages do not exist, so a project is created by
  seeding rather than through the UI.
- Playwright E2E (C9-04 names it) — the pipeline's end-to-end test covers the
  same path at the API level.

**Do not accidentally change:**
- The pipeline returning rather than raising for every ordinary stop. A caller
  that had to catch exceptions to tell "irrelevant" from "failed" would treat
  both as failures.
- `_assess` setting a specific `stopped_at`, and `run_pipeline` not overwriting
  it.
- The scheduler's `_in_flight` set. Two pipelines on one project would fight over
  the same run and the same workspace.
- `IntegrationHealth.available`. A score with no inputs must not render.
- `lib/noMocks.test.ts`, including the two tests that feed its matchers planted
  code — a structural test that has stopped matching looks exactly like a
  passing one.

**Next intended task:** the project owner's call. The remaining Phase 9 tickets
are C9-01, C9-02, C9-03, C9-05, C9-06, plus the sign-in and import screens that
would let a project be created without seeding.

---

### 2026-09-11 — The product loop: closing the gaps a direction audit found

**Why this happened:** an audit compared the code against the product story
step by step and found Continuity was assembled as components rather than as a
loop. 1130 tests passed while six modules had **zero production callers** and no
real repository could have started a run. This entry records the repair.

**What was broken, and what now closes it:**

1. **The front half was never wired.** `run_repository_scan`, `map_integrations`,
   and `establish_baseline` existed and were only ever called by tests, so a
   project reached monitoring by database seeding or not at all.
   `backend/orchestration/onboarding.py` now walks a project from
   `GITHUB_CONNECTED` to a real graph and a real baseline, and
   `backend/api/routers/onboarding.py` exposes it as
   `GET /repositories`, `POST /repositories/import`, `POST /projects/{id}/scan`.
   Repository authorization is checked against the App's live installation
   listing, not against what the request claims.

2. **Approval was a dead end.** The pipeline set `APPROVAL_PENDING` without
   creating an `Approval` row, and the validated patch died with the workspace.
   `repair.py` now creates a real approval request through
   `backend/approvals/service.py`, the patch and its digest are persisted before
   the run pauses, and `backend/orchestration/resume.py` resumes the *same*
   logical run: re-applies the stored diff, re-walks final validation, and
   delivers. Rejection ends the run at `REJECTED` and returns the project to
   monitoring without touching GitHub. No agent can write approval state — only
   `POST /approvals/{id}` behind a session can.

3. **Delivery's security gate was fed literals.** `validation_passed=True` and
   `security_decision=ALLOW` were hardcoded at the call site, which meant the
   gate could never refuse. `backend/orchestration/delivery_gate.py` now derives
   every precondition from persisted evidence — the last `MigrationAttempt`
   outcome, the recorded security review, the strictest `SecurityFinding`
   decision, the `Approval` rows — and **refuses when evidence is missing**, so
   "reviewed and clean" is distinguishable from "nobody looked". It also compares
   a digest of the files about to be delivered against the files that were
   reviewed, and refuses if they differ.

4. **`check_preconditions` treated ASK as a hard refusal**, which made approval
   pointless: a run that asked, and was answered yes, still could not deliver.
   ASK now refuses only when no approval was requested; each approval is still
   re-read at the instant it is relied upon. **DENY remains absolute** and no
   approval can lift it.

5. **Nothing started the scheduler.** See B-06 — the application's lifespan does
   now, and a test asserts it through the real ASGI lifecycle.

6. **The loop never closed after a merge.** `backend/workers/post_merge.py`
   verifies that a merged pull request is genuinely this run's — Continuity's own
   branch, matching the run's target branch, with a recorded target version — and
   the baseline advances **only** after `POST_MERGE_VERIFICATION_PASSED`. An
   inconsistent merge goes to `HUMAN_REVIEW_REQUIRED` and the baseline stays
   where it was. `poll_open_pull_requests` had no caller at all; with webhooks
   disabled, nothing ever noticed a merge. The scheduler's follow-up pass now
   resumes approvals, polls for merges, and verifies them.

**Honest scope of post-merge verification:** it checks merge consistency. It does
not contact a deployed application. It is not the Release Guardian of C9-02, and
is not described as one.

**Evidence:** `tests/integration/test_product_loop.py` — 18 tests that enter
through the HTTP API and seed nothing a real user could not produce, with GitHub
stubbed only at the external boundary. One of them,
`test_a_new_repository_reaches_monitoring_without_any_seeding`, is the direct
answer to the audit's sharpest finding. `tests/support/product_repo.py` builds a
real git repository with a real provider client so the extractor has genuine code
to read.

**Two changes worth flagging deliberately:**
- `git apply` was added to the executable allowlist so a resumed run can rebuild
  its own patch. The argv is fixed and not model-controlled, and it runs inside
  the confined workspace.
- `CHANGE_RELEVANT → MONITORING_ACTIVE` was added to the state machine and
  documented in `02_ARCHITECTURE.md` §8. Without it a project that saw a relevant
  change but stopped short of migrating was stuck forever and the scheduler
  skipped it. The bidirectional parity guard caught the undocumented edge, which
  is what it is for.

**What is NOT built:** C9-01 Red-Team, C9-02 Release Guardian, C9-03 confidential
execution, C9-05 evaluation harness, C9-06 final gate, Playwright E2E. The
frontend is the minimum needed to operate the loop, not the full 18 screens of
`04_FRONTEND_SPEC.md`.

**Do not accidentally change:**
- `delivery_gate.preconditions_for` **raising** `DeliveryRefused` rather than
  returning a failing set. A caller that could ignore the return value would
  silently deliver unreviewed code.
- The patch digest being recorded *before* the run pauses. Recorded after, it
  would attest to whatever the resume happened to produce.
- `advance_baseline` living only behind `POST_MERGE_VERIFICATION_PASSED`.
- The scheduler's start being gated on a real `GeminiConfig` — a scheduler with
  no model would sweep projects and fail every tick.

**Next intended task:** the project owner's call. Phase 9 proper — C9-01, C9-02,
C9-03, C9-05, C9-06.

---

### 2026-09-12 — Phase 9, C9-01: the Red Team

**What was built:** the agent that tries to break a migration before a person is
asked to trust it — and, more importantly, the thing it reads.

`backend/security/categories.py` reads the **diff**: what did this patch change
that is dangerous? `backend/security/attacks.py` reads the **result**: given the
integration code exactly as it will exist after merge, what does a hostile
provider or an attacker do to it? That distinction is the entire reason C9-01 is
not a second copy of C8-01. A migration that rewrites a payment client and ships
it without an idempotency key **removes** nothing, so the diff is clean; the
tests pass, because no test retries a checkout; and the result double-charges.
Only attacking the finished code finds it.

Seventeen attack classes, each with a deterministic probe, a fixture that lands
and a fixture that does not: malformed response, missing field, unexpected field,
unexpected null, expired credential, invalid token, webhook replay, webhook
duplication, duplicate transaction, timeout, retry storm, rate limit, malicious
external text, prompt injection, unauthorized tool, permission escalation,
invalid signature. `backend/agents/red_team.py` merges the agent's reading into
the probes' — INFERRED beside CONFIRMED, never overwriting it, and an attack
citing a file the agent was not given is discarded.

**Where it runs, and what it can do:** inside `SECURITY_REVIEW_RUNNING`, after
validation passes and before anything is delivered. A HIGH or CRITICAL attack
moves the run to `SECURITY_REVIEW_FAILED` and the loop routes it back to
`REPAIR_RUNNING` carrying the attack as evidence, so the next attempt is a
response to a specific failure. **No new run states were added** — that edge
already existed. The same repair budget bounds it, and exhaustion ends at
`HUMAN_REVIEW_REQUIRED`.

It can stop a run and cannot start one. `allowed_tools` is empty, asserted by
test. Refusing is not authorizing: nothing in the Red Team can permit a delivery,
and `backend/security/policy.py` still decides.

**One structural change it forced.** A run makes several patches. A finding
against a patch the Red Team rejected was still governing the patch that replaced
it — the run stayed ASK forever, blocked by a defect that no longer existed.
`security_findings.attempt_number` (migration `a39d335d7f8a`, additive and
nullable) records which patch a finding is about, and `backend/security/findings.py`
is now the single definition of "findings about the patch that is shipping". The
delivery gate reads only those. The API, the evidence report, and the security
page show **all** findings — deleting evidence of a rejected attempt would be
worse — labelled `superseded`, so history is never mistaken for a live defect.

**The delivery gate gained one more precondition:** a run with no recorded
red-team attack is refused. Same distinction as the security review, one stage
earlier — an empty attack list means "attacked and held" only if something
actually attacked it.

**Two review rounds failed before this passed.** Both findings were mine and both
were real:

1. **A comment could block a correct migration.** `# scope = "full" was required
   by the old SDK; we now request scope="read"` raised a blocking escalation
   finding — with no line and no excerpt, so it was an unanswerable complaint
   that would have looped a correct patch through repair until the budget ran
   out. The first fix skipped comment lines, which introduced the opposite bug:
   real code sharing a line with a docstring close became invisible, silently
   disabling any probe gated on it. The fix that held uses Python's own
   `tokenize` to blank comments and standalone string statements **character for
   character**, so line numbers stay aligned and code beside prose is still seen.
   String *literals* are deliberately untouched: `SCOPES = "charges.admin"` is
   the escalation, not a description of one.
2. **The frontend never got the `superseded` flag.** The API and the report
   carried it; `SecurityPage.tsx` rendered every row identically, so a CRITICAL
   finding from a rejected patch looked exactly like a defect in the code about
   to ship. That was the whole point of the fix, stopping one layer short.

**What is NOT built:** C9-02 Release Guardian, C9-03 confidential execution,
C9-04 full frontend, C9-05 evaluation harness, C9-06 final gate.

**Do not accidentally change:**
- `_text_probe` returning `[]` when no code line can be quoted. A
  CONFIRMED-confidence finding with no excerpt is not evidence, and at a
  blocking severity it is a run that can never be fixed.
- `code_lines` blanking rather than dropping. Dropping the line is what disabled
  the probes the first time.
- Standalone strings being prose and string literals being code. Collapsing that
  distinction breaks the escalation probe in one direction or the docstring
  false positive in the other.
- `attempt_scope` being the only definition of which findings count. Three
  readers depend on agreeing.
- The Red Team's empty `allowed_tools`.

**Next intended task:** C9-02, Release Guardian — post-merge verification against
a configured environment, with no code path that performs a rollback.

---

### 2026-09-12 — Phase 9, C9-02: the Release Guardian

**What was built:** the answer to a question records cannot answer. Post-merge
verification already checked that a merge was consistent with the run — right
branch, right pull request, right version — all of it derivable from what
Continuity already held. This asks whether the application still works.

It can only ask because the project says what working means. Continuity does not
know what the code it migrated does, and a check it invented would be the fake
verification `CLAUDE.md` rule 5 forbids. So the project declares its own in
`.continuity/verification.json`: a base URL, and synthetic requests with the
status each should return.

**The comparison is the substance.** The checks run twice — when the pull request
opens, while the old code is still deployed, and again after the merge. A check
failing in both is the project's existing problem; only one that passed before
and fails now is this migration's regression. Without the before-observation the
Guardian would blame the migration for what was already broken, and a team would
learn to ignore the reports.

| situation | recorded as | what happens |
| --- | --- | --- |
| no manifest | `not_configured` | the run reaches `VERIFIED`, claiming nothing about any deployment |
| manifest, checks hold | `passed` | `VERIFIED`, baseline advances |
| manifest, something regressed | `failed` | `POST_MERGE_VERIFICATION_FAILED → HUMAN_REVIEW_REQUIRED`, baseline stays |
| manifest with a typo | `manifest_invalid` | recorded and reported; it does not hold the merge hostage and it does not read as a pass |

`VerificationResult.scope` is derived rather than asserted, so the claim grows
only when something was actually checked.

**No rollback, and the test proves the absence.** The Guardian is the one part of
Continuity with a motive for a destructive action — it is looking at a broken
deployment and knows which commit caused it. That is exactly why it only
recommends. `tests/security/test_release_guardian_surface.py` walks the AST of
all three modules and asserts that no `revert`, `reset`, `force_push`,
`delete_branch`, or `redeploy` call exists, that none of them writes to a
repository, and that nothing branches on `rollback_recommended` except to log it.

**The one new risk, and what closes it.** This is the only place Continuity makes
an outbound request from its own host to an address someone else wrote. It is off
unless a deployment sets `RELEASE_VERIFICATION_ENABLED`, both observations are
gated on it, only `GET`/`HEAD`/`POST` can be declared, paths are relative to the
declared base URL, redirects are not followed, responses are size-capped and
redacted, and no credential is attached.

**The reviewer failed this once, for a good reason.** The first version validated
the scheme and nothing else, so a manifest reading
`"base_url": "http://169.254.169.254"` with a path of
`/latest/meta-data/iam/security-credentials/` would have had Continuity fetch its
own instance credentials and excerpt them into an evidence report. Instance
metadata endpoints are now refused outright and cannot be enabled by any setting;
loopback, private, and link-local addresses are refused unless
`RELEASE_VERIFICATION_ALLOW_PRIVATE_HOSTS` says the environment really is
internal. The host is resolved and checked immediately before the request, not
only when the manifest is parsed. The residual DNS-rebinding window is documented
in `03_SECURITY_ACCESS.md` §5 rather than papered over.

The second finding was quieter and just as real: `_changed_files` read
`file_path` out of the stored security review, and the review had never written
it. It returned nothing on every real run, so the Guardian was always told the
migration changed no files. `ReviewFinding.summary_dict()` now records where a
finding is, which also means a reader of the stored report can follow one to a
line.

**What is NOT built:** C9-03 confidential execution (deferred by the project
owner), C9-04 full frontend, C9-05 evaluation harness, C9-06 final gate.

**Do not accidentally change:**
- `resolve_target` running before the client opens, and the metadata block being
  unconditional. `RELEASE_VERIFICATION_ALLOW_PRIVATE_HOSTS` must never reach it.
- The opt-in gating **both** observations. Gating only the post-merge one would
  leave delivery making outbound requests with the feature switched off.
- `observe_before_merge` staying wrapped in `deliver()`. A staging host that is
  down must not refuse a pull request.
- `already_failing` being reported and not counted as a regression, and
  `unattributable` failing closed. They are the two halves of not lying about
  cause.
- The `environment` callable on `verify_merge` defaulting to the real
  `verify_environment`. A default of `None` would silently skip verification in
  production while every test still passed.

**Next intended task:** C9-05, the evaluation harness — every metric in
`02_ARCHITECTURE.md` §18 computed from stored records over labelled fixtures.
