# Continuity

**An autonomous integration reliability platform.**

Continuity continuously monitors the third-party APIs and SDKs your application
depends on, determines whether a provider change actually affects *your* code,
prepares and validates a safe migration, and delivers a reviewable pull request.

Built on the [Strands Agents SDK](https://strandsagents.com) with Amazon Bedrock.

> **Status: Phase 0 of 10 complete — project anchoring (10%).**
> The anchor documentation and engineering process are established and have
> passed an independent review gate. No application code exists yet; it begins
> at Phase 1. See [`STATUS.md`](STATUS.md) for real, current state — it is
> authoritative, and this line is not.

---

## The problem

Your application depends on payment providers, auth services, messaging APIs,
cloud SDKs, and internal partner APIs. Those providers change constantly —
endpoints, schemas, webhook events, OAuth scopes, error contracts, deprecations.

Every change forces the same loop: notice it, read the changelog, decide whether
it affects you, find the code, work out blast radius, migrate, fix tests, review
permissions, open a PR.

The work is repetitive but not scriptable — the right action depends on your
repository, the provider's semantics, which business workflows are involved, and
your security constraints. A dependency bot can tell you a version moved. It
cannot tell you your checkout flow breaks because a webhook event was renamed.

Most teams find out in production.

## What Continuity does

```
Repository → Integration Mapping → Provider Monitoring → Provider Change
  → Impact Analysis → Rehearsal → Migration → Validation → Repair if needed
  → Security Review → Human Approval if needed → Evidence → Pull Request
```

The developer's only required actions: connect a repository, approve anything
risky, and merge.

### Design commitments

- **Deterministic where it can be.** AST parsing, spec diffing, test execution,
  state transitions, and policy enforcement are ordinary code. The model is used
  for judgment — semantic interpretation, relevance, diagnosis, and patching.
- **Repositories are never dumped into a model.** A deterministic index and
  bounded, secret-filtered retrieval build the context.
- **Model output is never authorization.** A deterministic policy engine returns
  ALLOW / ASK / DENY, and human approval is persisted state no agent can write.
- **Evidence or it didn't happen.** Every number shown — changes detected, tests
  passed, files affected — traces to a stored execution record.
- **Branch and PR only.** No force pushes, no history rewrites, no writes to
  your default branch, no auto-merge.

## Architecture

Seven runtime agents, each a Strands agent with a specialized prompt, an
explicit tool allowlist, and a Pydantic structured-output contract:

| Agent | Role |
| --- | --- |
| Orchestrator | Coordinates the run; enforces order, retry budgets, approval pauses |
| Change Scout | Detects and normalizes provider changes, with source evidence |
| Integration Mapper | Builds the Integration Intelligence Graph |
| Impact Analyst | Decides whether a change affects this project; traces blast radius |
| Migration Engineer | Plans and produces the patch; diagnoses and repairs failures |
| Validator / Tester | Runs builds and tests; produces deterministic evidence |
| Security Reviewer | Reviews the diff; recommends ALLOW/ASK/DENY |

Red-Team and Release Guardian agents follow once the core seven work.

Agents never chat freely. Every hand-off is
`agent → validated structured output → deterministic state transition → next step`.

**Stack:** Python 3.12 · FastAPI · Strands Agents SDK · Amazon Bedrock ·
Bedrock AgentCore · SQLAlchemy · Next.js · React · TypeScript · Tailwind CSS.

## Documentation

| Document | Contents |
| --- | --- |
| [`01_PRD.md`](01_PRD.md) | Product requirements, user journey, MVP scope |
| [`02_ARCHITECTURE.md`](02_ARCHITECTURE.md) | Stack, state machine, graph schema, provider adapters, evaluation |
| [`03_SECURITY_ACCESS.md`](03_SECURITY_ACCESS.md) | Threat model, ALLOW/ASK/DENY matrix, GitHub permissions, execution safety |
| [`04_FRONTEND_SPEC.md`](04_FRONTEND_SPEC.md) | UI specification |
| [`05_FEATURE_TICKETS.md`](05_FEATURE_TICKETS.md) | Executable tickets across all ten phases |
| [`STATUS.md`](STATUS.md) | Live project state, blockers, context log |
| [`CLAUDE.md`](CLAUDE.md) | Engineering process for AI-assisted development |

## Getting started

Setup instructions will be added with Phase 1, when there is an application to
run. Until then, `.env.example` documents every configuration variable the
system will require.

Requirements (planned): Python ≥ 3.10 (3.12 recommended), Node ≥ 20, an AWS
account with Amazon Bedrock model access, and a GitHub App installation.

## License

[Apache-2.0](LICENSE) — chosen over MIT for its explicit patent grant, and to
match the licensing of the Strands Agents SDK and the Bedrock AgentCore SDK.
