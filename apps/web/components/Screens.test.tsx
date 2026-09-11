/**
 * Component tests for the screens `04_FRONTEND_SPEC.md` §3 defines.
 *
 * What these check is not that React renders — it is the rules the spec puts on
 * what may be displayed:
 *
 * - a health score whose inputs are absent is not displayed at all;
 * - inferred data carries a visible marker;
 * - a provider never checked says so, rather than showing a plausible time;
 * - a change that reached no code says so, which is the product's most frequent
 *   correct answer;
 * - both security verdicts are shown, and a disagreement is marked;
 * - approval buttons write backend state and re-read rather than assuming.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Approvals } from "@/components/Approvals";
import { Changes } from "@/components/Changes";
import { ConnectRepository } from "@/components/ConnectRepository";
import { GraphView } from "@/components/GraphView";
import { Integrations } from "@/components/Integrations";
import { ProjectOverview } from "@/components/ProjectOverview";
import { ProjectsList } from "@/components/ProjectsList";
import { Runs } from "@/components/Runs";
import { SecurityPage } from "@/components/SecurityPage";
import * as api from "@/lib/api";

afterEach(() => {
  vi.restoreAllMocks();
});

const PROJECT_ID = "11111111-1111-1111-1111-111111111111";

const project = (
  overrides: Partial<api.ProjectSummary> = {},
): api.ProjectSummary => ({
  id: PROJECT_ID,
  name: "commerce-api",
  state: "monitoring_active",
  repository: "acme/commerce-api",
  providers: 2,
  integration_points: 23,
  open_changes: 1,
  pending_approvals: 0,
  health: {
    available: true,
    score: 91,
    formula: "health = 100 - 15*(breaking) - ... - coverage_penalty",
    inputs: { integration_points: 23, uncovered_integration_points: 5 },
    unavailable_reason: null,
  },
  ...overrides,
});

describe("ProjectsList", () => {
  it("renders only what the API returned", async () => {
    vi.spyOn(api, "listProjects").mockResolvedValue([project()]);

    render(<ProjectsList />);

    expect(await screen.findByText("commerce-api")).toBeInTheDocument();
    expect(screen.getByText("91%")).toBeInTheDocument();
    expect(screen.getByText("Monitoring Active")).toBeInTheDocument();
  });

  it("says so when there are no projects", async () => {
    vi.spyOn(api, "listProjects").mockResolvedValue([]);

    render(<ProjectsList />);

    expect(await screen.findByText(/No projects yet/)).toBeInTheDocument();
  });

  it("distinguishes an unreachable API from an API error", async () => {
    vi.spyOn(api, "listProjects").mockRejectedValue(
      new api.ApiUnreachableError(new Error("ECONNREFUSED")),
    );

    render(<ProjectsList />);

    expect(
      await screen.findByText(/API is unreachable/),
    ).toBeInTheDocument();
  });
});

describe("ProjectOverview", () => {
  it("shows the health score with the formula that produced it", async () => {
    vi.spyOn(api, "getProject").mockResolvedValue(project());
    vi.spyOn(api, "listActivity").mockResolvedValue([]);

    render(<ProjectOverview projectId={PROJECT_ID} />);

    expect(await screen.findByText("91%")).toBeInTheDocument();
    expect(screen.getByText(/How this is calculated/)).toBeInTheDocument();
    expect(screen.getByText(/coverage_penalty/)).toBeInTheDocument();
  });

  it("shows no score at all when its inputs are absent", async () => {
    // §3.6: a placeholder number would be a lie about system state.
    vi.spyOn(api, "getProject").mockResolvedValue(
      project({
        health: {
          available: false,
          score: null,
          formula: null,
          inputs: {},
          unavailable_reason: "this project has not been scanned",
        },
      }),
    );
    vi.spyOn(api, "listActivity").mockResolvedValue([]);

    render(<ProjectOverview projectId={PROJECT_ID} />);

    expect(await screen.findByText(/No score yet/)).toBeInTheDocument();
    expect(screen.getByText(/has not been scanned/)).toBeInTheDocument();
    // No score, and no formula either — there is nothing to publish the
    // arithmetic for. Scoped to the health card, because "At a glance" legitimately
    // shows zeroes for counts that really are zero.
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.queryByText(/How this is calculated/)).not.toBeInTheDocument();
  });
});

describe("Integrations", () => {
  it("says a provider was never checked rather than inventing a time", async () => {
    vi.spyOn(api, "listIntegrations").mockResolvedValue([
      {
        provider_id: "acmepay",
        display_name: "AcmePay",
        sdk_package: null,
        detected_api_version: null,
        auth_mechanism: null,
        integration_points: 3,
        confidence: "confirmed",
        last_checked_at: null,
        last_check_error: null,
      },
    ]);

    render(<Integrations projectId={PROJECT_ID} />);

    expect(await screen.findByText("AcmePay")).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument();
    // An em dash, not a guessed version.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("marks inferred data visibly", async () => {
    vi.spyOn(api, "listIntegrations").mockResolvedValue([
      {
        provider_id: "acmepay",
        display_name: "AcmePay",
        sdk_package: "acmepay",
        detected_api_version: "v1",
        auth_mechanism: "oauth2",
        integration_points: 3,
        confidence: "inferred",
        last_checked_at: "2026-09-11T10:00:00Z",
        last_check_error: null,
      },
    ]);

    render(<Integrations projectId={PROJECT_ID} />);

    expect(await screen.findByText("inferred")).toBeInTheDocument();
  });
});

describe("Changes", () => {
  it("says when a change reached no code", async () => {
    // The most frequent correct answer this product gives.
    vi.spyOn(api, "listChanges").mockResolvedValue([
      {
        id: "c1",
        provider_id: "acmepay",
        old_version: "v1",
        new_version: "v2",
        change_type: "endpoint_added",
        resource: "GET /v2/disputes",
        breaking: false,
        security_relevant: false,
        detected_at: "2026-09-11T10:00:00Z",
        migration_run_id: null,
      },
    ]);

    render(<Changes projectId={PROJECT_ID} />);

    expect(await screen.findByText("no code affected")).toBeInTheDocument();
    expect(screen.getByText("additive")).toBeInTheDocument();
  });

  it("marks a breaking change as breaking", async () => {
    vi.spyOn(api, "listChanges").mockResolvedValue([
      {
        id: "c2",
        provider_id: "acmepay",
        old_version: "v1",
        new_version: "v2",
        change_type: "request_field_required",
        resource: "POST /v1/charges request.currency",
        breaking: true,
        security_relevant: true,
        detected_at: "2026-09-11T10:00:00Z",
        migration_run_id: "r1",
      },
    ]);

    render(<Changes projectId={PROJECT_ID} />);

    expect(await screen.findByText("breaking")).toBeInTheDocument();
    expect(screen.getByText("security")).toBeInTheDocument();
    expect(screen.getByText("migration opened")).toBeInTheDocument();
  });
});

describe("Runs", () => {
  it("shows attempts, findings, and the pull request from the API", async () => {
    vi.spyOn(api, "listRuns").mockResolvedValue([
      {
        id: "r1",
        provider_id: "acmepay",
        from_version: "v1",
        to_version: "v2",
        state: "merge_waiting",
        target_branch: "continuity/migrate-acmepay-v2",
        attempts: 2,
        findings: 1,
        pull_request: 7,
        created_at: "2026-09-11T10:00:00Z",
      },
    ]);

    render(<Runs projectId={PROJECT_ID} />);

    expect(await screen.findByText("PR #7")).toBeInTheDocument();
    expect(screen.getByText(/2 attempts/)).toBeInTheDocument();
    expect(screen.getByText(/1 finding\b/)).toBeInTheDocument();
  });
});

describe("SecurityPage", () => {
  it("reports attestation as not configured", async () => {
    // 03_SECURITY_ACCESS.md §7: never claim an attested state without one.
    vi.spyOn(api, "listFindings").mockResolvedValue([]);

    render(<SecurityPage projectId={PROJECT_ID} />);

    expect(await screen.findByText("Not Configured")).toBeInTheDocument();
    expect(
      screen.getByText(/process isolation, not sandboxing/),
    ).toBeInTheDocument();
  });

  it("shows both verdicts and marks a disagreement", async () => {
    vi.spyOn(api, "listFindings").mockResolvedValue([
      {
        id: "f1",
        migration_run_id: "r1",
        category: "oauth_scope_change",
        severity: "high",
        summary: "The patch requests customers.write.",
        recommendation: "allow",
        policy_decision: "ask",
        disagreed: true,
      },
    ]);

    render(<SecurityPage projectId={PROJECT_ID} />);

    expect(await screen.findByText("disagreement")).toBeInTheDocument();
    expect(screen.getByText(/Reviewer advised/)).toBeInTheDocument();
  });
});

describe("GraphView", () => {
  it("marks inferred nodes and says when there is no graph", async () => {
    vi.spyOn(api, "getGraph").mockResolvedValue({
      version: null,
      nodes: [],
      edges: [],
    });

    const { unmount } = render(<GraphView projectId={PROJECT_ID} />);
    expect(await screen.findByText(/No graph yet/)).toBeInTheDocument();
    unmount();

    vi.spyOn(api, "getGraph").mockResolvedValue({
      version: 1,
      nodes: [
        { id: "n1", kind: "workflow", key: "Checkout", label: "Checkout", confidence: "inferred" },
        { id: "n2", kind: "file", key: "app/x.py", label: "app/x.py", confidence: "confirmed" },
      ],
      edges: [],
    });

    render(<GraphView projectId={PROJECT_ID} />);

    expect(await screen.findByText("Checkout")).toBeInTheDocument();
    expect(screen.getAllByText("inferred")).toHaveLength(1);
  });
});

describe("Approvals", () => {
  const approval = (): api.ApprovalView => ({
    id: "a1",
    project_id: PROJECT_ID,
    migration_run_id: "r1",
    trigger: "install_dependency",
    risk: "medium",
    status: "pending",
    requested_action: { package: "left-pad" },
    agent_recommendation: "ask",
    actor_user_id: null,
    resolved_at: null,
  });

  it("writes the decision to the backend and re-reads", async () => {
    // The spec's rule: the buttons write backend state, and nothing is
    // optimistic — a decision that failed server-side must not look decided.
    const list = vi.spyOn(api, "listApprovals").mockResolvedValue([approval()]);
    const decide = vi
      .spyOn(api, "decideApproval")
      .mockResolvedValue({ ...approval(), status: "approved" });

    render(<Approvals />);
    await screen.findByText("Install Dependency");

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(decide).toHaveBeenCalledWith("a1", "approve");
    });
    // Re-read rather than assumed.
    await waitFor(() => expect(list.mock.calls.length).toBeGreaterThan(1));
  });

  it("surfaces a refused decision instead of showing it as done", async () => {
    vi.spyOn(api, "listApprovals").mockResolvedValue([approval()]);
    vi.spyOn(api, "decideApproval").mockRejectedValue(
      new api.ApiError(409, {
        code: "approval_already_resolved",
        message: "This approval has already been decided.",
      }),
    );

    render(<Approvals />);
    await screen.findByText("Install Dependency");

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));

    expect(
      await screen.findByText(/already been decided/),
    ).toBeInTheDocument();
  });

  it("says so when nothing needs a decision", async () => {
    vi.spyOn(api, "listApprovals").mockResolvedValue([]);

    render(<Approvals />);

    expect(
      await screen.findByText(/Nothing needs your approval/),
    ).toBeInTheDocument();
  });
});

describe("ConnectRepository", () => {
  const repository = (): api.AuthorizedRepository => ({
    full_name: "acme/commerce-api",
    default_branch: "main",
    installation_id: 4900912,
  });

  it("lists only what the GitHub App authorizes", async () => {
    vi.spyOn(api, "listAuthorizedRepositories").mockResolvedValue([repository()]);

    render(<ConnectRepository />);

    expect(await screen.findByText("acme/commerce-api")).toBeInTheDocument();
    expect(
      screen.getByText(/does not grant access to any repository/),
    ).toBeInTheDocument();
  });

  it("says so when nothing is authorized", async () => {
    vi.spyOn(api, "listAuthorizedRepositories").mockResolvedValue([]);

    render(<ConnectRepository />);

    expect(
      await screen.findByText(/No repositories are authorized yet/),
    ).toBeInTheDocument();
  });

  it("imports, scans, and reports what the scan actually found", async () => {
    vi.spyOn(api, "listAuthorizedRepositories").mockResolvedValue([repository()]);
    const imported = vi.spyOn(api, "importRepository").mockResolvedValue({
      project_id: "p1",
      name: "commerce-api",
      repository: "acme/commerce-api",
      can_migrate: true,
      note: "Scan this project to map its integrations and start monitoring.",
      state: "project_created",
    });
    const scanned = vi.spyOn(api, "scanProject").mockResolvedValue({
      project_id: "p1",
      state: "monitoring_active",
      monitorable: true,
      files_indexed: 12,
      graph_version: 1,
      confirmed_nodes: 9,
      inferred_workflows: 2,
      providers: 1,
      mapping_degraded: null,
    });

    render(<ConnectRepository />);
    await userEvent.click(await screen.findByRole("radio"));
    await userEvent.click(screen.getByRole("button", { name: "Import and scan" }));

    await waitFor(() => expect(scanned).toHaveBeenCalledWith("p1"));
    expect(imported).toHaveBeenCalledWith("acme/commerce-api", "");
    expect(await screen.findByText("Scan complete")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("Monitoring")).toBeInTheDocument();
  });

  it("warns when a project cannot be migrated", async () => {
    // Honest rather than convenient: without a checkout there is nowhere to run
    // the tests a migration has to pass, so no pull request will ever arrive.
    vi.spyOn(api, "listAuthorizedRepositories").mockResolvedValue([repository()]);
    vi.spyOn(api, "importRepository").mockResolvedValue({
      project_id: "p1",
      name: "commerce-api",
      repository: "acme/commerce-api",
      can_migrate: false,
      note: "Migrations need a local checkout.",
      state: "project_created",
    });
    vi.spyOn(api, "scanProject").mockResolvedValue({
      project_id: "p1",
      state: "monitoring_active",
      monitorable: true,
      files_indexed: 12,
      graph_version: 1,
      confirmed_nodes: 9,
      inferred_workflows: 0,
      providers: 1,
      mapping_degraded: "no model provider is configured",
    });

    render(<ConnectRepository />);
    await userEvent.click(await screen.findByRole("radio"));
    await userEvent.click(screen.getByRole("button", { name: "Import and scan" }));

    expect(
      await screen.findByText(/will not open pull requests/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Workflow inference was skipped/),
    ).toBeInTheDocument();
  });

  it("surfaces an import failure instead of proceeding to scan", async () => {
    vi.spyOn(api, "listAuthorizedRepositories").mockResolvedValue([repository()]);
    vi.spyOn(api, "importRepository").mockRejectedValue(
      new api.ApiError(404, {
        code: "not_found",
        message: "acme/commerce-api is not authorized for any of your installations.",
      }),
    );
    const scanned = vi.spyOn(api, "scanProject");

    render(<ConnectRepository />);
    await userEvent.click(await screen.findByRole("radio"));
    await userEvent.click(screen.getByRole("button", { name: "Import and scan" }));

    expect(await screen.findByText(/is not authorized/)).toBeInTheDocument();
    expect(scanned).not.toHaveBeenCalled();
  });
});
