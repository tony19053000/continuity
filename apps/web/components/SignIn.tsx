"use client";

import { useEffect, useState } from "react";

import { apiBaseUrl, getCurrentUser, type CurrentUser } from "@/lib/api";

type Load =
  | { kind: "loading" }
  | { kind: "anonymous" }
  | { kind: "signed-in"; user: CurrentUser }
  | { kind: "failed" };

/**
 * Sign-in.
 *
 * The scope statement below is not decoration. `04_FRONTEND_SPEC.md` §3.1
 * requires the UI to say plainly that authentication is not repository
 * authorization, because the single most damaging assumption a developer could
 * make here is that signing in has already given Continuity access to their
 * code. The backend enforces the separation; this states it.
 */
export function SignIn() {
  const [state, setState] = useState<Load>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;

    getCurrentUser()
      .then((user) => {
        if (cancelled) return;
        setState(user ? { kind: "signed-in", user } : { kind: "anonymous" });
      })
      .catch(() => {
        if (!cancelled) setState({ kind: "failed" });
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="mx-auto max-w-md space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Sign in to Continuity
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-neutral-600 dark:text-neutral-400">
          Continuity uses your Google account to identify you.
        </p>
      </div>

      <section
        aria-labelledby="scope-heading"
        className="rounded-lg border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900"
      >
        <h2
          id="scope-heading"
          className="text-sm font-semibold tracking-tight"
        >
          Signing in does not grant access to your code
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-neutral-600 dark:text-neutral-400">
          Repository access is a separate step. After signing in you choose
          which repositories to authorize by installing the Continuity GitHub
          App, and you can revoke that access at any time without affecting your
          account.
        </p>
      </section>

      {state.kind === "signed-in" ? (
        <p className="text-sm text-neutral-700 dark:text-neutral-300">
          Signed in as {state.user.email}.{" "}
          {state.user.github_connected
            ? "GitHub is connected."
            : "GitHub is not connected yet."}
        </p>
      ) : (
        <a
          href={`${apiBaseUrl()}/auth/login`}
          className="inline-flex w-full items-center justify-center rounded-md bg-neutral-900 px-4 py-2.5 text-sm font-medium text-white hover:bg-neutral-800 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-200"
          aria-disabled={state.kind === "loading"}
        >
          Continue with Google
        </a>
      )}

      {state.kind === "failed" && (
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          The API could not be reached, so your sign-in state is unknown. You
          can still try to continue.
        </p>
      )}
    </div>
  );
}
