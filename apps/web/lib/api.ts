/**
 * Typed client for the Continuity API.
 *
 * Only `NEXT_PUBLIC_*` variables are readable here — anything else would be
 * inlined into the browser bundle at build time, which is exactly how secrets
 * leak into a frontend (`03_SECURITY_ACCESS.md` §2).
 *
 * Every call goes through `request()`, so error handling, credentials, and the
 * typed-error contract are defined once rather than at each call site.
 */

const DEFAULT_BASE_URL = "http://localhost:8000";

export const apiBaseUrl = (): string =>
  process.env.NEXT_PUBLIC_API_BASE_URL || DEFAULT_BASE_URL;

/** A typed error from the backend (`backend/shared/errors.py`). */
export interface ApiErrorBody {
  code: string;
  message: string;
  detail?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly detail?: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.detail = body.detail;
  }
}

/** The backend is unreachable — distinct from the backend returning an error. */
export class ApiUnreachableError extends Error {
  constructor(cause: unknown) {
    super("The Continuity API could not be reached.");
    this.name = "ApiUnreachableError";
    this.cause = cause;
  }
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      // Sessions are HttpOnly cookies; they must be sent with every request.
      credentials: "include",
      headers: { Accept: "application/json", ...init.headers },
    });
  } catch (cause) {
    throw new ApiUnreachableError(cause);
  }

  if (!response.ok) {
    // A non-JSON error body means something other than the API answered — a
    // proxy or a gateway. Surface it as a typed error rather than crashing on
    // a parse failure.
    const body = (await response.json().catch(() => ({
      code: "unexpected_response",
      message: `The API returned ${response.status}.`,
    }))) as ApiErrorBody;
    throw new ApiError(response.status, body);
  }

  return (await response.json()) as T;
}

// --- Health -------------------------------------------------------------

export type OkState = "ok" | "error";
export type ConfiguredState = "configured" | "not_configured";
/** Three-valued: no job runner exists yet, and "ok" would be a fake green. */
export type QueueState = "ok" | "error" | "not_configured";

export interface HealthComponents {
  database: OkState;
  gemini: ConfiguredState;
  github_app: ConfiguredState;
  job_queue: QueueState;
}

export interface Health {
  version: string;
  status: "ok" | "degraded" | "error";
  components: HealthComponents;
}

export const getHealth = (): Promise<Health> => request<Health>("/health");

// --- Auth ---------------------------------------------------------------

export interface CurrentUser {
  id: string;
  email: string;
  display_name: string | null;
  avatar_url: string | null;
  /** Signing in does NOT grant repository access; this is the separate state. */
  github_connected: boolean;
}

/** The signed-in user, or `null` when nobody is signed in. */
export async function getCurrentUser(): Promise<CurrentUser | null> {
  try {
    return await request<CurrentUser>("/auth/me");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return null;
    }
    throw error;
  }
}

// --- Projects -----------------------------------------------------------

/**
 * Every type below mirrors a backend response exactly. None of them has a
 * default or a fallback: `04_FRONTEND_SPEC.md` forbids a component containing a
 * hardcoded provider, change, workflow, test count, or status, and the way to
 * guarantee that is to make absence representable — `null` here means the
 * backend has no record, and the UI must say so rather than fill it in.
 */

export type RunState = string;
export type Confidence = "confirmed" | "inferred";

/** A score, or an explicit statement that there is not one yet. */
export interface IntegrationHealth {
  available: boolean;
  score: number | null;
  /** Published beside the score, so the arithmetic is checkable. */
  formula: string | null;
  inputs: Record<string, number>;
  unavailable_reason: string | null;
}

export interface ProjectSummary {
  id: string;
  name: string;
  state: RunState;
  repository: string | null;
  providers: number;
  integration_points: number;
  open_changes: number;
  pending_approvals: number;
  health: IntegrationHealth;
}

export interface IntegrationView {
  provider_id: string;
  display_name: string;
  sdk_package: string | null;
  detected_api_version: string | null;
  auth_mechanism: string | null;
  integration_points: number;
  confidence: Confidence;
  /** Null means never checked — not "checked and fine". */
  last_checked_at: string | null;
  last_check_error: string | null;
}

export interface ChangeView {
  id: string;
  provider_id: string;
  old_version: string;
  new_version: string;
  change_type: string;
  resource: string;
  breaking: boolean;
  security_relevant: boolean;
  detected_at: string;
  migration_run_id: string | null;
}

export interface RunView {
  id: string;
  provider_id: string;
  from_version: string;
  to_version: string;
  state: RunState;
  target_branch: string | null;
  attempts: number;
  findings: number;
  pull_request: number | null;
  created_at: string;
}

export interface ActivityView {
  id: string;
  kind: string;
  actor: string;
  summary: string;
  occurred_at: string;
  migration_run_id: string | null;
}

export interface GraphNode {
  id: string;
  kind: string;
  key: string;
  label: string;
  confidence: Confidence;
}

export interface GraphEdge {
  id: string;
  kind: string;
  source: string;
  target: string;
  confidence: Confidence;
}

export interface GraphView {
  version: number | null;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface FindingView {
  id: string;
  migration_run_id: string;
  category: string;
  severity: string;
  summary: string;
  /** What the Security Reviewer advised. */
  recommendation: string;
  /** What the policy engine ruled. This one is binding. */
  policy_decision: string;
  disagreed: boolean;
}

export interface EvidenceReport {
  report: Record<string, unknown>;
  markdown: string;
}

export const listProjects = (): Promise<ProjectSummary[]> =>
  request<ProjectSummary[]>("/projects");

export const getProject = (id: string): Promise<ProjectSummary> =>
  request<ProjectSummary>(`/projects/${id}`);

export const listIntegrations = (id: string): Promise<IntegrationView[]> =>
  request<IntegrationView[]>(`/projects/${id}/integrations`);

export const listChanges = (id: string): Promise<ChangeView[]> =>
  request<ChangeView[]>(`/projects/${id}/changes`);

export const listRuns = (id: string): Promise<RunView[]> =>
  request<RunView[]>(`/projects/${id}/runs`);

export const listActivity = (id: string): Promise<ActivityView[]> =>
  request<ActivityView[]>(`/projects/${id}/activity`);

export const getGraph = (id: string): Promise<GraphView> =>
  request<GraphView>(`/projects/${id}/graph`);

export const listFindings = (id: string): Promise<FindingView[]> =>
  request<FindingView[]>(`/projects/${id}/findings`);

export const getReport = (
  projectId: string,
  runId: string,
): Promise<EvidenceReport> =>
  request<EvidenceReport>(`/projects/${projectId}/runs/${runId}/report`);

// --- Approvals ----------------------------------------------------------

export interface ApprovalView {
  id: string;
  project_id: string;
  migration_run_id: string | null;
  trigger: string;
  risk: string;
  status: "pending" | "approved" | "rejected";
  requested_action: Record<string, unknown>;
  agent_recommendation: string | null;
  actor_user_id: string | null;
  resolved_at: string | null;
}

export const listApprovals = (): Promise<ApprovalView[]> =>
  request<ApprovalView[]>("/approvals");

/**
 * Approve or reject. The body carries no approver: the decision is attributed
 * to the authenticated session, and the backend refuses a body that tries to
 * name one.
 */
export const decideApproval = (
  id: string,
  decision: "approve" | "reject",
): Promise<ApprovalView> =>
  request<ApprovalView>(`/approvals/${id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });

// --- Onboarding ---------------------------------------------------------

export interface AuthorizedRepository {
  full_name: string;
  default_branch: string;
  installation_id: number;
}

export interface ProjectCreated {
  project_id: string;
  name: string;
  repository: string;
  /**
   * Whether migrations are possible. Continuity reads a repository through the
   * GitHub App, which supplies contents but not a working tree — so without a
   * local checkout it monitors and assesses but never opens a pull request.
   * The UI says so rather than letting a user discover it later.
   */
  can_migrate: boolean;
  note: string;
  state: RunState;
}

export interface ScanResult {
  project_id: string;
  state: RunState;
  monitorable: boolean;
  files_indexed: number;
  graph_version: number | null;
  confirmed_nodes: number;
  inferred_workflows: number;
  providers: number;
  /** Set when workflow inference was skipped; the graph is confirmed-only. */
  mapping_degraded: string | null;
}

export const listAuthorizedRepositories = (): Promise<AuthorizedRepository[]> =>
  request<AuthorizedRepository[]>("/repositories");

export const importRepository = (
  fullName: string,
  localPath?: string,
): Promise<ProjectCreated> =>
  request<ProjectCreated>("/repositories/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      full_name: fullName,
      local_path: localPath || null,
    }),
  });

export const scanProject = (projectId: string): Promise<ScanResult> =>
  request<ScanResult>(`/projects/${projectId}/scan`, { method: "POST" });
