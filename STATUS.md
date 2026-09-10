# STATUS

**This file is authoritative.** If any other document, README badge, or commit
message disagrees with it, this file is right and the other is stale.

---

## Overall completion

**10%**

Phase 0 complete. The review gate ran twice: FAIL with 15 findings → all
corrected → **PASS**. No application code exists yet, which is correct for this
phase.

| Field | Value |
| --- | --- |
| Current phase | Phase 1 — Application + backend foundation |
| Current ticket | C1-01 — Backend application skeleton |
| Last updated | 2026-09-10 |

---

## Phase progress

| Phase | Scope | Target | Status |
| --- | --- | --- | --- |
| 0 | Project anchoring | 10% | **DONE** — reviewer PASS |
| 1 | Application + backend foundation | 20% | IN PROGRESS |
| 2 | Strands + core orchestration | 30% | PENDING |
| 3 | GitHub + safe repository ingestion | 40% | PENDING |
| 4 | Integration Mapper + Intelligence Graph | 50% | PENDING |
| 5 | Provider monitoring + Change Scout | 60% | PENDING |
| 6 | Execution safety, impact analysis, rehearsal | 70% | PENDING |
| 7 | Migration Engineer + repair loop | 80% | PENDING |
| 8 | Security + approval + GitHub PR | 90% | PENDING |
| 9 | Production security + frontend + polish | 100% | PENDING |

---

## Tickets

**Completed:** C0-01, C0-02, C0-03, C0-04
**In progress:** none
**Next:** C1-01 — Backend application skeleton
**Pending:** all of C1-01 onward — see `05_FEATURE_TICKETS.md` (50 tickets)

---

## Blockers

### B-01 · AWS credentials are not configured on this machine — OPEN

**Impact:** Blocks live Amazon Bedrock model calls, and therefore blocks C2-07
(Strands end-to-end proof) from *passing*, and C8-05 (AgentCore) entirely.
Does not block Phases 0, 1, or the deterministic majority of 2–7.

**Exact state:** `aws` CLI is not installed (`aws: command not found`) and
`~/.aws` does not exist. No credentials, profile, or region are configured.

**Resolution:** Install the AWS CLI, configure credentials for an account with
Amazon Bedrock model access enabled in the target region, and set `AWS_REGION`
(and optionally `BEDROCK_MODEL_ID`) in `.env`.

**Handling until resolved:** The `ModelProvider` abstraction (C2-01) is built
regardless. C2-07 skips with an explicit message naming this blocker — it never
silently passes. No development adapter is ever described as Bedrock.

### B-02 · GitHub App does not exist yet — OPEN

**Impact:** Blocks C3-02 from live operation and all of C8-03 / C8-06 delivery
against real repositories.

**Exact state:** No Continuity GitHub App has been registered. `GITHUB_APP_ID`,
`GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`,
`GITHUB_APP_PRIVATE_KEY_PATH`, and `GITHUB_APP_WEBHOOK_SECRET` are unset.

**Resolution:** Register a GitHub App with exactly the permissions in
`03_SECURITY_ACCESS.md` §3 (Contents: R/W, Pull requests: R/W, Metadata: R,
Checks: R) and the `pull_request` event subscription, then populate `.env`.

**Handling until resolved:** `LocalRepositoryAdapter` (C3-03) covers ingestion
development and testing. Absent config resolves to `NotConfigured`, never a fake
success.

### B-03 · Google OAuth credentials not configured — OPEN

**Impact:** Blocks C1-05 from live sign-in.

**Exact state:** `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
`SESSION_SECRET` are unset.

**Resolution:** Create OAuth credentials in Google Cloud Console and populate
`.env`.

**Handling until resolved:** C1-05 is implemented and unit-tested against the
flow; live sign-in is deferred. Absent config yields `NotConfigured`.

### B-04 · No TEE / Nitro Enclave infrastructure — OPEN, low priority

**Impact:** C9-03 cannot deliver hardware-backed attestation.

**Handling:** The `ConfidentialExecutionProvider` abstraction is built and
`DevelopmentIsolatedExecutor` remains functional. The UI reports
`TEE Attestation: Not Configured`. **Attestation is never faked.**

---

## Tests

| Suite | Command | State |
| --- | --- | --- |
| Python unit | `pytest` | Not yet created (Phase 1) |
| Integration | `pytest tests/integration` | Not yet created (Phase 1) |
| Security | `pytest tests/security` | Not yet created (Phase 3) |
| Frontend unit | `npm run test` | Not yet created (Phase 1) |
| E2E | `npm run test:e2e` | Not yet created (Phase 9) |
| Lint (py) | `ruff check .` | Not yet configured (Phase 1) |
| Typecheck (py) | `mypy backend` | Not yet configured (Phase 1) |
| Lint (web) | `npm run lint` | Not yet configured (Phase 1) |
| Typecheck (web) | `npm run typecheck` | Not yet configured (Phase 1) |
| Build (web) | `npm run build` | Not yet configured (Phase 1) |

No test has ever been reported as passing. Nothing exists to run yet.

---

## AWS / Strands state

| Item | State |
| --- | --- |
| Strands Agents SDK | Not yet installed. Target `strands-agents` (Phase 2, C2-01) |
| Amazon Bedrock | **Not configured** — see B-01 |
| `BEDROCK_MODEL_ID` | Unset; will default to the Strands documented default |
| `AWS_REGION` | Unset |
| AgentCore Runtime | Not provisioned (Phase 8, C8-05) |
| AgentCore Observability | Not provisioned |
| AgentCore Identity | Not provisioned |
| AgentCore Gateway/Policy | Not provisioned; adoption conditional (§17) |

Nothing above is claimed as working anywhere in the product or documentation.

---

## Security state

| Control | State |
| --- | --- |
| Secret filtering | Specified (`03_SECURITY_ACCESS.md` §2); implemented in C3-01 |
| Policy engine (ALLOW/ASK/DENY) | Specified §4; implemented in C2-03 / C8-02 |
| Approval integrity | Specified §4; state in C2-08, enforcement in C8-02 |
| Repository boundary | Specified §3; implemented in C3-02 / C3-03 |
| Untrusted external content | Specified §5; exercised in C5-04 |
| ExecutionProvider | Specified §6; implemented in C6-01 |
| Confidential execution / TEE | Abstraction only — see B-04. **Not attested** |
| Committed secrets | None. Verified against the staged diff at each gate |

---

## GitHub integration state

| Item | State |
| --- | --- |
| GitHub App | Not registered — see B-02 |
| Repository authorization | Not established |
| Branch/PR delivery | Not implemented (Phase 8) |
| Merge detection | Not implemented (C8-06) |
| Local `gh` CLI | Authenticated as `tony19053000` |
| Remote `origin` | `https://github.com/tony19053000/continuity.git` (empty repo) |

---

## Git

| Field | Value |
| --- | --- |
| Branch | `main` |
| Latest commit | *(none yet — Phase 0 commit pending review PASS)* |
| Push state | Not pushed. `gh` is authenticated, so push is expected to work |

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
