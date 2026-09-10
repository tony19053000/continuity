import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SystemStatus } from "@/components/SystemStatus";
import * as api from "@/lib/api";

afterEach(() => {
  vi.restoreAllMocks();
});

const health = (overrides: Partial<api.Health> = {}): api.Health => ({
  version: "0.1.0",
  status: "degraded",
  components: {
    database: "ok",
    bedrock: "not_configured",
    github_app: "not_configured",
    job_queue: "not_configured",
  },
  ...overrides,
});

describe("SystemStatus", () => {
  it("renders the state the backend reports", async () => {
    vi.spyOn(api, "getHealth").mockResolvedValue(health());

    render(<SystemStatus />);

    await waitFor(() => {
      expect(screen.getByText("Connected")).toBeInTheDocument();
    });
    expect(screen.getByText("0.1.0")).toBeInTheDocument();
  });

  it("shows unconfigured integrations as 'Not configured', never as healthy", async () => {
    // The project's honesty rule: an integration Continuity has not been told
    // how to reach must never render as a success state.
    vi.spyOn(api, "getHealth").mockResolvedValue(health());

    render(<SystemStatus />);

    await waitFor(() => {
      // Bedrock, GitHub App, and the job queue — none of which exist yet.
      expect(screen.getAllByText("Not configured")).toHaveLength(3);
    });
    expect(screen.queryByText("Configured")).not.toBeInTheDocument();
  });

  it("never renders the job queue as Ready while no runner exists", async () => {
    // Regression guard: this was hardcoded to "ok", putting a green check in
    // the UI backed by nothing.
    vi.spyOn(api, "getHealth").mockResolvedValue(health());

    render(<SystemStatus />);

    await waitFor(() => {
      expect(screen.getByText("Job queue")).toBeInTheDocument();
    });
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
  });

  it("does not claim health while still loading", () => {
    vi.spyOn(api, "getHealth").mockReturnValue(new Promise(() => {}));

    render(<SystemStatus />);

    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
  });

  it("gives an actionable message when the API is unreachable", async () => {
    vi.spyOn(api, "getHealth").mockRejectedValue(
      new api.ApiUnreachableError(new Error("connection refused")),
    );

    render(<SystemStatus />);

    await waitFor(() => {
      expect(screen.getByText(/not reachable/i)).toBeInTheDocument();
    });
    // An error must say what to do next, not merely that something failed.
    expect(screen.getByText(/uvicorn/)).toBeInTheDocument();
  });

  it("surfaces a backend error rather than rendering a blank panel", async () => {
    vi.spyOn(api, "getHealth").mockRejectedValue(
      new api.ApiError(500, {
        code: "internal_error",
        message: "An internal error occurred.",
      }),
    );

    render(<SystemStatus />);

    await waitFor(() => {
      expect(
        screen.getByText("An internal error occurred."),
      ).toBeInTheDocument();
    });
  });
});
