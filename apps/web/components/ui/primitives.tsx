/**
 * The vocabulary every screen is built from.
 *
 * Two rules from `04_FRONTEND_SPEC.md` are enforced here rather than left to
 * each screen to remember:
 *
 * - **State colour discipline.** Five states, one mapping, defined once. A
 *   screen that invents its own colour for "attention" would make the palette
 *   meaningless.
 * - **Confirmed vs inferred.** Inferred data carries a visible marker. It is a
 *   component rather than a convention so a screen cannot render inferred data
 *   without one.
 */

import type { ReactNode } from "react";

export type ToneName =
  | "neutral"
  | "info"
  | "attention"
  | "critical"
  | "success";

/** `04_FRONTEND_SPEC.md` §2: what each state means. */
export const TONE_MEANING: Record<ToneName, string> = {
  neutral: "Monitoring, healthy, nothing required",
  info: "Work in progress",
  attention: "Awaiting human input",
  critical: "Breaking, failed, or high-risk",
  success: "Verified with evidence",
};

const TONE_CLASS: Record<ToneName, string> = {
  neutral:
    "bg-neutral-100 text-neutral-700 ring-neutral-300 dark:bg-neutral-800 dark:text-neutral-300 dark:ring-neutral-700",
  info: "bg-sky-50 text-sky-800 ring-sky-300 dark:bg-sky-950 dark:text-sky-200 dark:ring-sky-800",
  attention:
    "bg-amber-50 text-amber-900 ring-amber-300 dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-800",
  critical:
    "bg-rose-50 text-rose-900 ring-rose-300 dark:bg-rose-950 dark:text-rose-200 dark:ring-rose-800",
  success:
    "bg-emerald-50 text-emerald-900 ring-emerald-300 dark:bg-emerald-950 dark:text-emerald-200 dark:ring-emerald-800",
};

/**
 * Run and project states, mapped to a tone.
 *
 * Derived from the state name rather than listed one by one: the backend has
 * 45 states and a hand-written map would go stale silently the first time one
 * was added.
 */
export function toneForState(state: string): ToneName {
  if (/(failed|denied|rejected)/.test(state)) return "critical";
  if (/(verified|passed|confirmed|merged)/.test(state)) return "success";
  if (/(human_review|approval_pending|change_detected|relevant)/.test(state)) {
    return "attention";
  }
  if (/(running|pending|creating|waiting)/.test(state)) return "info";
  return "neutral";
}

export function toneForSeverity(severity: string): ToneName {
  if (severity === "critical" || severity === "high") return "critical";
  if (severity === "medium") return "attention";
  if (severity === "low") return "info";
  return "neutral";
}

/** A state, in words a person reads rather than an enum value. */
export function humanise(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function Badge({
  tone = "neutral",
  children,
  title,
}: {
  tone?: ToneName;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title ?? TONE_MEANING[tone]}
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

/**
 * The confirmed/inferred marker.
 *
 * Inferred data is visibly marked wherever it appears. Confirmed data renders
 * nothing — the absence of a marker is the signal, and marking both would make
 * neither stand out.
 */
export function ConfidenceMark({ confidence }: { confidence: string }) {
  if (confidence !== "inferred") return null;
  return (
    <span
      title="Inferred by a model from deterministic evidence. Not a confirmed fact."
      className="ml-1.5 rounded border border-dashed border-amber-400 px-1 text-[10px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-300"
    >
      inferred
    </span>
  );
}

export function Card({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

/**
 * What a screen shows when there is nothing to show.
 *
 * A distinct state from loading and from error: "nothing has happened yet" is
 * information, and rendering an empty table instead leaves a reader wondering
 * whether it is broken.
 */
export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="py-6 text-center text-sm text-neutral-500 dark:text-neutral-400">
      {children}
    </p>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p
      role="status"
      className="py-6 text-center text-sm text-neutral-500 dark:text-neutral-400"
    >
      Loading {label}…
    </p>
  );
}

/**
 * An error a person can act on.
 *
 * Distinguishes "the API said no" from "the API could not be reached", because
 * the second is usually the backend not running and the first usually is not.
 */
export function Failed({ error }: { error: unknown }) {
  const unreachable =
    error instanceof Error && error.name === "ApiUnreachableError";
  return (
    <div
      role="alert"
      className="rounded-md border border-rose-300 bg-rose-50 p-4 text-sm text-rose-900 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200"
    >
      <p className="font-medium">
        {unreachable ? "The Continuity API is unreachable." : "Something went wrong."}
      </p>
      <p className="mt-1 text-xs">
        {unreachable
          ? "Start the backend, or check NEXT_PUBLIC_API_BASE_URL."
          : error instanceof Error
            ? error.message
            : "An unexpected error occurred."}
      </p>
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
        {label}
      </dt>
      <dd className="mt-1 text-2xl font-semibold tabular-nums">{value}</dd>
      {hint ? (
        <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/** A timestamp a person can read, or an explicit "never". */
export function When({ iso }: { iso: string | null }) {
  if (!iso) {
    return (
      <span className="text-neutral-500 dark:text-neutral-400">never</span>
    );
  }
  return <time dateTime={iso}>{new Date(iso).toLocaleString()}</time>;
}
