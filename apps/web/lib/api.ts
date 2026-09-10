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
