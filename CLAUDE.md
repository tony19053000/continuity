# CLAUDE.md — Continuity

Instructions for every Claude Code session working in this repository.

---

## 1. Read before doing anything

1. `STATUS.md` — current phase, ticket, blockers, and context log
2. `01_PRD.md` — what the product is
3. `05_FEATURE_TICKETS.md` — the active ticket and its acceptance criteria
4. The relevant section of `02_ARCHITECTURE.md`
5. The relevant section of `03_SECURITY_ACCESS.md`
6. `04_FRONTEND_SPEC.md` when touching `apps/web/`
7. The source files for the active ticket

Do not restart architecture planning. Do not repeat completed work. Do not
overwrite previous decisions without evidence that they were wrong.

## 2. Use the review gate

```
[CODER] implements → [REVIEWER / TESTER] reviews
   → FAIL: [CODER] fixes → [REVIEWER / TESTER] retests
   → PASS: update docs → update STATUS.md → git commit → push → next phase
```

Agents live in `.claude/agents/`. Never skip the gate. Never raise the
completion percentage because code was written — a phase counts only after
`reviewer-tester` returns PASS.

## 3. Non-negotiable rules

1. Continuity's runtime agents use the **Strands Agents SDK**. Never replace it
   with another primary agent framework without an explicit architecture
   amendment in `02_ARCHITECTURE.md`.
2. **LLM output never equals authorization.** `backend/security/policy.py`
   decides; agents recommend.
3. **Never expose secrets** — not to prompts, logs, error messages, API
   responses, PR bodies, or the browser. Filtering runs *before* context
   construction.
4. **GitHub writes are branch-and-PR only.** No force push, no history rewrite,
   no default-branch write, no auto-merge.
5. **Never fake anything**: provider monitoring, agent execution, test results,
   GitHub operations, security findings, AgentCore integration, or TEE
   attestation. A blocked external service is recorded in `STATUS.md`, not
   simulated.
6. **Never invent** credentials, account ids, model ids, ARNs, IAM roles, KMS
   keys, or GitHub App secrets.
7. **Never dump a repository into a model.** Context goes through the
   deterministic index and bounded retrieval.
8. **Never execute a model-generated shell string.** All execution goes through
   `ExecutionProvider` with argv-only, allowlisted commands.
9. **Never weaken or delete a failing test to reach PASS.** Diagnose it, or
   escalate that the test itself is wrong.
10. **Never display raw model chain-of-thought.**
11. Update `STATUS.md` after every completed phase, including the context log.
12. Do not silently alter product scope.
13. **Provider Lab and demo applications are out of scope for this repository.**
    They are built separately by the project owner. Continuity must expose clean
    provider and test interfaces so they can plug in later — and must require no
    hardcoded knowledge of them. Test fixtures under `tests/fixtures/` are fine;
    demo companies, storefronts, and provider dashboards are not.

## 4. Verification commands

One command runs every gate check and prints a single line per check:

```bash
./scripts/verify.sh
```

Failures print their detail; successes print one line. `--full` shows
everything. **Reviewers should run this rather than the eight commands
individually** — reading pages of passing output costs far more than it proves.

The underlying commands, for focused work while developing:

```bash
uv run pytest tests/unit/agents      # or any subset
uv run ruff check .
uv run mypy backend
uv run alembic check
```

Frontend (from `apps/web/`): `npm run typecheck | lint | test | build`.

Note the environment uses `uv`, not `venv` — see `02_ARCHITECTURE.md` §1.

## 5. Git discipline

- Work in this repository; the remote is already configured — inspect it, do not
  replace it.
- One commit per phase or ticket. No giant commits spanning unrelated phases.
- Before committing: inspect `git status` and the full diff; confirm no secrets,
  `.env` files, or build output are staged.
- Never force push or rewrite history.
- Push only after a phase passes review. If push authentication is unavailable,
  commit locally, record the exact blocker in `STATUS.md`, and never claim a
  push succeeded.

## 6. Phase 0 review checklist

The reviewer must answer each of these with evidence:

- Does Continuity solve a real, repetitive professional task?
- Is Strands central rather than cosmetic?
- Are the runtime agent roles genuinely necessary, or could code do it?
- Is deterministic code used everywhere AI is unnecessary?
- Is repository context filtered and bounded before model use?
- Is the Integration Intelligence Graph a real persisted data model?
- Can provider adapters support externally-built demo providers later, without
  core changes?
- Is provider monitoring independent of any demo trigger?
- Can irrelevant provider changes be ignored?
- Can impacted workflows be traced to evidence?
- Is migration performed in isolation?
- Is the repair loop bounded?
- Are security decisions enforced outside the LLM?
- Are GitHub writes branch/PR only?
- Are secrets protected before context construction?
- Are external docs treated as untrusted data?
- Is TEE work honest and optional?
- Is every AgentCore feature justified?
- Is Provider Lab / demo-app development excluded?
- Are acceptance criteria measurable?
- Is the implementation order realistic?
- Is anything missing that would block the core Continuity workflow?

## 7. Terminology

**Development subagents** (`coder`, `reviewer-tester`) build Continuity.
**Runtime agents** (Orchestrator, Change Scout, Integration Mapper, Impact
Analyst, Migration Engineer, Validator, Security Reviewer, + Red-Team and
Release Guardian later) are the product. Never confuse the two.
