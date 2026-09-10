import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  ApiUnreachableError,
  getCurrentUser,
  request,
} from "@/lib/api";

afterEach(() => {
  vi.restoreAllMocks();
});

const jsonResponse = (body: unknown, status = 200): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }) as Response;

describe("request", () => {
  it("sends credentials so the HttpOnly session cookie is included", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ ok: true }));

    await request("/health");

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/health"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("raises a typed ApiError carrying the backend error code", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ code: "not_found", message: "Missing." }, 404),
    );

    await expect(request("/projects/1")).rejects.toMatchObject({
      code: "not_found",
      status: 404,
    });
  });

  it("distinguishes an unreachable API from an API error", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new TypeError("Failed to fetch"),
    );

    await expect(request("/health")).rejects.toBeInstanceOf(
      ApiUnreachableError,
    );
  });

  it("handles a non-JSON error body without crashing", async () => {
    // A proxy or gateway answering instead of the API.
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => {
        throw new SyntaxError("Unexpected token <");
      },
    } as unknown as Response);

    await expect(request("/health")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("getCurrentUser", () => {
  it("returns null when nobody is signed in", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ code: "authentication_required", message: "no" }, 401),
    );

    await expect(getCurrentUser()).resolves.toBeNull();
  });

  it("propagates errors that are not a missing session", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ code: "internal_error", message: "boom" }, 500),
    );

    await expect(getCurrentUser()).rejects.toBeInstanceOf(ApiError);
  });

  it("reports github_connected separately from being signed in", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        id: "u1",
        email: "dev@example.com",
        display_name: null,
        avatar_url: null,
        github_connected: false,
      }),
    );

    const user = await getCurrentUser();

    expect(user).not.toBeNull();
    expect(user?.github_connected).toBe(false);
  });
});
