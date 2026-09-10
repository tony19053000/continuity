import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SignIn } from "@/components/SignIn";
import * as api from "@/lib/api";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SignIn", () => {
  it("states that signing in does not grant repository access", async () => {
    // 04_FRONTEND_SPEC.md §3.1. The backend enforces this separation; the UI
    // must say it, because the damaging assumption is that sign-in already
    // handed Continuity the user's code.
    vi.spyOn(api, "getCurrentUser").mockResolvedValue(null);

    render(<SignIn />);

    expect(
      screen.getByText(/does not grant access to your code/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/separate step/i)).toBeInTheDocument();
  });

  it("offers Google sign-in when nobody is signed in", async () => {
    vi.spyOn(api, "getCurrentUser").mockResolvedValue(null);

    render(<SignIn />);

    await waitFor(() => {
      expect(
        screen.getByRole("link", { name: /continue with google/i }),
      ).toBeInTheDocument();
    });
  });

  it("reports GitHub as not connected for a freshly signed-in user", async () => {
    vi.spyOn(api, "getCurrentUser").mockResolvedValue({
      id: "u1",
      email: "dev@example.com",
      display_name: null,
      avatar_url: null,
      github_connected: false,
    });

    render(<SignIn />);

    await waitFor(() => {
      expect(screen.getByText(/GitHub is not connected yet/i)).toBeInTheDocument();
    });
  });

  it("says the state is unknown rather than guessing when the API fails", async () => {
    vi.spyOn(api, "getCurrentUser").mockRejectedValue(
      new api.ApiUnreachableError(new Error("down")),
    );

    render(<SignIn />);

    await waitFor(() => {
      expect(screen.getByText(/sign-in state is unknown/i)).toBeInTheDocument();
    });
  });
});
