# 04 — Frontend Specification

**Status:** Phase 0 baseline. Living state.

**Implementation priority:** the frontend is built incrementally and is *not*
front-loaded. The first goal is `Continuity works`; the second is
`Continuity looks excellent`. Do not spend early effort recreating dashboard
animations while the integration-maintenance loop is incomplete. Phase 1 ships
only a shell; Phase 9 ships the finished product.

---

## 1. Design position

Continuity should feel like a premium modern developer platform. Reference
points: Vercel, modern cloud consoles, infrastructure monitoring products, and
high-quality developer tools.

**Do not pixel-copy Vercel.** Continuity has its own identity.

The visual language communicates: reliability, calm, trust, engineering rigour,
continuous monitoring, and clear system state. The product's emotional promise is
"something competent is watching this for you" — so the default state is quiet.
Colour is spent on state that actually needs attention, never on decoration.

Desktop is primary. Layouts remain usable down to tablet width; mobile is
read-only-comfortable, not a build target.

### State colour discipline

A fixed, small vocabulary, used consistently everywhere:

| State | Meaning |
| --- | --- |
| Neutral | Monitoring, healthy, nothing required |
| Info | Work in progress (scan, migration, validation running) |
| Attention | Relevant change detected; awaiting human input |
| Critical | Breaking + relevant, failed validation, high-risk security finding |
| Success | Verified with evidence |

**Success is earned, never assumed.** A green check requires a backing record.
There is no optimistic UI in this product.

### Confirmed vs. inferred

The backend distinguishes `confirmed` (deterministic) from `inferred` (model
proposed). The UI must too — inferred data carries a visible marker and its
evidence is one click away. Presenting inferred data as confirmed is a defect.

---

## 2. Navigation

**Global:** Projects · Activity · Settings

**Project:** Overview · Integrations · Changes · Agent Runs · Security ·
Pull Requests · Settings

---

## 3. Screens

### 3.1 Authentication

User signs in (Google). Sign-in grants access to Continuity only — it does not
grant repository access, and the UI says so plainly rather than implying it.

### 3.2 Connect GitHub

Explicit, separate step. The user installs/authorizes the Continuity GitHub App
and chooses which repositories it may access. The screen states which
permissions are requested and why, in the same terms as
`03_SECURITY_ACCESS.md` §3.

### 3.3 Project import

Lists only repositories the installation actually authorized.

```
Import Git Repository

Search...

commerce-api
booking-platform
analytics-service
internal-tools

[Import]
```

### 3.4 Initial scan

Real progress driven by activity events. Each line appears when its event
arrives — never on a timer, never pre-checked.

```
Analyzing commerce-api

✓ Repository structure
✓ Framework detection
✓ Dependency manifests
✓ External SDKs
✓ API clients
✓ Webhooks
✓ Integration mapping

Building Integration Intelligence Graph...
```

If a step fails, its row shows the failure and a next action. No step is marked
complete before its event is recorded.

### 3.5 Projects

Developer-grade grid/list with current state per project:

```
commerce-api        Healthy
booking-platform    Attention
analytics-service   Monitoring
internal-tools      Migration Running
```

### 3.6 Project Overview

```
Integration Health
91%

7 Providers
23 Integration Points
1 Active Change
0 Critical
```

(That figure is the worked example in `02_ARCHITECTURE.md` §18: 23 integration
points, 5 untested, one relevant non-breaking change, no pending approvals.
Examples in this document must remain producible by the documented formula.)

Also: monitored providers, active change events, pending approvals, recent
migrations, PR status, and live agent activity.

The health percentage links to its formula (`02_ARCHITECTURE.md` §18) and its
inputs. A score whose inputs are not yet recorded is not displayed at all —
showing a placeholder number would be a lie about system state.

### 3.7 Integrations

One card per provider, all fields backed by real records:

```
Payment Provider

Status:          Healthy
Used by:         Checkout, Subscriptions, Refunds
Files:           9
API version:     v1
Authentication:  OAuth
Last checked:    2 minutes ago
```

"Last checked" is a real monitoring timestamp. If monitoring has never run, it
says so.

### 3.8 Integration Intelligence Graph

Visualizes `Provider → source module → business workflow → dependent
tests/services`, reading the persisted graph.

When a provider change is active, affected nodes highlight so blast radius is
immediately legible. This is not a decorative force-directed cloud — the layout
is hierarchical and readable, node labels are always visible, and clicking any
node opens its evidence (file, line span, source excerpt).

Large graphs collapse by provider rather than becoming a hairball.

### 3.9 Changes

```
Provider API v2

Severity:            HIGH
Detected:            4 min ago
Changes:             12
Breaking:            3
Relevant:            2
Affected workflows:  Checkout, Subscription Renewal
```

The relevant/irrelevant split is the product's core insight and should be
prominent — the value is as much in the 10 changes correctly ignored as in the
2 flagged. Each change expands to show its type, old/new contract, and source
evidence link.

### 3.10 Agent Runs

Live pipeline state:

```
Change Scout        COMPLETE
Impact Analyst      COMPLETE
Migration Engineer  WORKING
Validator           WAITING
Security Reviewer   WAITING
```

Clicking an agent shows: task, tools used, evidence, status, output summary,
duration, and errors.

Activity vocabulary shown to users: Monitoring provider · Reading changelog ·
Comparing OpenAPI · Mapping repository usage · Identifying affected workflows ·
Rehearsing new contract · Preparing migration · Running tests · Diagnosing
failure · Repairing · Running security review · Waiting for approval ·
Preparing pull request.

**Raw model chain-of-thought is never displayed.** Concise task and activity
summaries plus evidence only.

The repair loop is worth showing explicitly — attempt 1 failing, the diagnosis,
and attempt 2 passing is the clearest demonstration that the system is doing
real work.

### 3.11 Approval

```
ACTION REQUIRED

Permission expansion detected

Current:    customers.read
Requested:  customers.write
Risk:       HIGH

Continuity recommendation:
Find alternative approach.

[Approve]  [Use safer alternative]  [Reject]
```

Every approval card shows what triggered it, the exact delta, the risk
classification, and the agent's recommendation. The action buttons write
deterministic backend approval state; the run stays paused until that state
changes. Frontend display alone never counts as approval.

### 3.12 Security

Every row reads live backend state (`03_SECURITY_ACCESS.md` §11):

```
Repository Access          Scoped
Secret Filtering           Active
Default Branch Writes      Blocked
Prompt Injection Defense   Active
Secure Execution           Development Isolation
TEE Attestation            Not Configured
```

Rows that are not configured say `Not Configured` in a neutral tone — not a
warning, and never a fake green check. `Development Isolation` is displayed
distinctly from hardware-backed confidential execution.

### 3.13 Pull Requests

Migration, provider, source → target version, files changed, test results,
security state, approval state, and GitHub PR link/status. Each row expands to
the full migration evidence report.

### 3.14 Ask Continuity

Secondary. Chat answers questions grounded in stored records:

- "Why is checkout affected?"
- "Why did this migration require three files?"
- "Can you find a migration without increasing the OAuth scope?"
- "Explain this security finding."

The chat respects the same backend policy and approval system and cannot bypass
workflow state. It is an explanation surface over recorded evidence, not a
second control plane.

---

## 4. Loading and error states

Polished, specific states are required for: repository import, initial scan, no
integrations found, provider unavailable, model unavailable, GitHub
unavailable, analysis failed, migration failed, test failed, approval required,
PR creation failed, and AWS service unavailable.

Rules:

- No indefinite blank spinners. Long operations show which step is running and
  what evidence has arrived so far.
- Every error states what happened, what it affects, and the next action.
- Empty states explain what will populate them and when.
- "No integrations found" is a legitimate, well-designed result — not an error.
- A blocked external service (no AWS credentials, GitHub App not installed)
  shows the exact blocker, matching `STATUS.md`, rather than a generic failure.

---

## 5. Quality bar for Phase 9

Responsive, accessible (keyboard navigable, sufficient contrast, semantic
landmarks, respects reduced-motion), polished loading states, meaningful empty
and error states, and real data throughout.

Forbidden at completion: fake green checks, hardcoded agent results, placeholder
critical flows, and any displayed number not traceable to a stored record.
