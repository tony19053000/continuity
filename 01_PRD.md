# 01 — Product Requirements Document

**Product:** Continuity

**Status:** Phase 0 baseline. This document is living state — update it when
product scope materially changes.

---

## 1. One-line product statement

Continuity is a Strands-powered autonomous integration reliability platform that
continuously monitors the third-party APIs and SDKs an application depends on,
determines whether provider changes affect the application, prepares and
validates safe migrations, and delivers reviewable fixes to developers.

---

## 2. Core problem

Modern applications depend on third-party services: payment providers,
messaging providers, authentication services, cloud APIs, email services,
shipping providers, AI APIs, analytics platforms, storage providers,
communication platforms, and internal partner APIs.

Those providers change continuously — API versions, endpoints, request and
response schemas, SDK behaviour, authentication methods, OAuth scopes, webhook
formats, required headers, rate limits, error contracts, and deprecation
policies.

Every time a provider changes, a software team repeats the same loop:

1. Notice the change.
2. Read the changelog and documentation.
3. Decide whether it affects *this* application.
4. Find the relevant code.
5. Determine blast radius.
6. Update the integration.
7. Update tests.
8. Run tests, debug failures.
9. Review permission and authentication implications.
10. Prepare a pull request.
11. Verify the migration after deploy.

This work is repetitive, but it is not scriptable. The correct action depends on
repository context, provider documentation semantics, which business workflows
touch the integration, and security constraints. A dependency bot can tell you a
version number moved; it cannot tell you that your checkout flow breaks because
a webhook event was renamed.

**The failure mode is silent.** Teams typically discover provider breakage in
production, through a failed payment or a dropped webhook, rather than at the
moment the provider published the change.

Continuity closes that gap.

---

## 3. Target users

**Primary**

- software developers maintaining external integrations
- SaaS and startup engineering teams
- backend, platform, and integration engineers
- DevOps/platform teams responsible for integration uptime

**Secondary**

- technical founders without a dedicated platform team
- agencies maintaining integrations across many client codebases
- organisations whose core workflows depend heavily on external APIs

---

## 4. Product principle

Continuity behaves like **an autonomous integration reliability engineer**.

It does not behave like **a developer chatbot that explains how to update an
API**.

The difference is concrete and testable:

| Chatbot behaviour | Continuity behaviour |
| --- | --- |
| "Stripe renamed this event; you should update your handler." | Opens a PR that updates the handler, with 47/47 tests passing as evidence. |
| Answers when asked | Detects the change without being asked |
| Produces advice | Produces a validated diff and a reproducible evidence report |
| Confident prose | Structured findings traced to file, line, and provider source |

---

## 5. Core user journey

1. Developer signs into Continuity.
2. Developer connects GitHub (installs the Continuity GitHub App).
3. Developer authorizes selected repositories only.
4. Developer imports a project.
5. Continuity analyzes the repository under a read-only boundary.
6. Continuity detects external API/SDK integrations.
7. Continuity maps those integrations to business workflows.
8. Continuity establishes the current provider/version baseline.
9. Continuity begins continuous provider monitoring.
10. A provider publishes a change.
11. Continuity detects the provider update.
12. Continuity determines whether this project is affected.
13. Continuity identifies affected workflows, files, and functions.
14. Continuity rehearses the incompatibility where technically possible.
15. Continuity plans a migration.
16. Continuity modifies code inside an isolated workspace/branch.
17. Continuity runs validation.
18. On failure, Continuity diagnoses the failure.
19. Continuity repairs the migration.
20. Continuity reruns validation.
21. Continuity performs a security review.
22. Continuity detects sensitive permission/authentication changes.
23. Continuity requests human approval when required.
24. Continuity performs final validation.
25. Continuity generates a migration evidence report.
26. Continuity opens a GitHub pull request.
27. Developer reviews and merges using their normal workflow.
28. Continuity verifies the migration post-merge where an environment exists.
29. Continuity returns to continuous monitoring.

The developer's only mandatory actions are steps 1–4, step 23 when it triggers,
and step 27. Everything else is autonomous.

---

## 6. Main product loop

```
CONNECT → UNDERSTAND → MAP → MONITOR → DETECT → ANALYZE → ASSESS IMPACT
   → REHEARSE → MIGRATE → TEST → REPAIR → SECURITY REVIEW
   → HUMAN APPROVAL IF REQUIRED → PROVE → PULL REQUEST → VERIFY → MONITOR AGAIN
```

---

## 7. MVP scope

The MVP must prove the **entire** loop end to end. Breadth of provider support
is explicitly *not* the MVP goal; a working loop over a small number of
providers is worth more than shallow support for many.

In scope for MVP:

- GitHub-connected or local project analysis
- detection of external integrations
- structured integration mapping into the Integration Intelligence Graph
- provider adapter architecture with capability detection
- provider version / changelog / spec monitoring
- provider change normalization
- relevant-vs-irrelevant impact analysis
- affected file, function, and workflow detection
- migration planning
- code patch generation in an isolated workspace
- test execution with structured result parsing
- bounded autonomous repair loop
- security review
- deterministic human approval state
- GitHub branch + pull request delivery
- migration evidence report

Explicitly **not** MVP:

- Provider Lab, demo storefronts, or demo provider dashboards — these live
  outside this repository and are built separately
- support for every possible provider or language
- live production deployment verification
- organization-wide dashboards

Continuity must not claim language, framework, or provider support that does not
actually exist in the code.

---

## 8. Initial repository support

Continuity's own backend is Python-first, and repository analysis targets, in
order:

1. **Python** — full AST-based analysis (imports, call sites, decorators,
   route definitions, function boundaries).
2. **TypeScript / JavaScript** — import graph and call-site detection.

Analysis is adapter-based (`LanguageAnalyzer`) so additional languages are added
without touching the orchestration or graph layers.

---

## 9. Future functionality

Deliberately out of MVP, recorded so scope stays honest:

- additional programming languages and frameworks
- a larger provider adapter catalogue
- automatic SDK major-version upgrade reasoning
- live deployment verification (Release Guardian, Phase 9+)
- organization-wide integration health
- Slack / email notifications
- VS Code extension
- CI/CD integration
- cross-repository dependency mapping
- long-term organizational memory (AgentCore Memory)
- richer provider digital twins
- predictive deprecation risk scoring

---

## 10. Success criteria

Continuity is working when, without human prompting, it can:

1. Detect a published provider change from a real monitored source.
2. Correctly classify the majority of that provider's changes as irrelevant to
   a given repository, and correctly flag the relevant ones.
3. Trace each relevant change to specific files, functions, and business
   workflows, each with source evidence.
4. Produce a patch that makes previously-failing contract tests pass without
   regressing the rest of the suite.
5. Recover from at least one failed validation attempt through the repair loop.
6. Block, or escalate for approval, a migration that expands permissions.
7. Open a pull request whose description is backed entirely by recorded
   execution data.

Measurement detail lives in `02_ARCHITECTURE.md` §Evaluation.

---

## 11. Definition of done

Continuity is **not** complete because the frontend renders, agents return text,
provider cards appear, code is generated, or a build succeeds.

It is complete when this path genuinely executes:

```
Repository → Integration Mapping → Provider Monitoring → Provider Change
  → Impact Analysis → Rehearsal/Evidence → Migration → Validation
  → Repair if needed → Security Review → Human Approval if needed
  → Migration Proof → GitHub Pull Request
```

At minimum this path must function against controlled test fixtures before the
project may be called complete. Externally-built provider/demo systems will
later exercise this same architecture; Continuity must require no hardcoded
knowledge of them.

---

## 12. Hackathon positioning

- **Hackathon:** AWS Agents for Humans
- **Track:** Professional Agents

Continuity fits the track because it takes repetitive, judgment-heavy
professional work — integration maintenance — and performs it end to end.

**Mandatory technology:** Strands Agents SDK, used centrally rather than
cosmetically. Amazon Bedrock is the primary model provider. Amazon Bedrock
AgentCore is used where it delivers real production value (see
`02_ARCHITECTURE.md` §AgentCore).
