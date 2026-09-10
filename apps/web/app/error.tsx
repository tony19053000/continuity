"use client";

import { useEffect } from "react";

/**
 * Route-level error boundary.
 *
 * Shows what happened and what to do next — never a bare stack trace and never
 * an indefinite spinner (`04_FRONTEND_SPEC.md` §4). The digest is included
 * because it is the handle for finding the matching server log entry.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-6 dark:border-red-900 dark:bg-red-950/40">
      <h2 className="text-sm font-semibold text-red-900 dark:text-red-200">
        Something went wrong
      </h2>
      <p className="mt-2 text-sm text-red-800 dark:text-red-300">
        {error.message || "An unexpected error occurred."}
      </p>
      {error.digest && (
        <p className="mt-2 text-xs text-red-700 dark:text-red-400">
          Reference: {error.digest}
        </p>
      )}
      <button
        type="button"
        onClick={reset}
        className="mt-4 rounded-md border border-red-300 px-3 py-1.5 text-sm font-medium text-red-900 hover:bg-red-100 dark:border-red-800 dark:text-red-200 dark:hover:bg-red-900/40"
      >
        Try again
      </button>
    </div>
  );
}
