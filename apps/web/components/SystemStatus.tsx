"use client";

import { useEffect, useState } from "react";

import {
  ApiUnreachableError,
  getHealth,
  type ConfiguredState,
  type Health,
  type OkState,
  type QueueState,
} from "@/lib/api";

type Load =
  | { kind: "loading" }
  | { kind: "loaded"; health: Health }
  | { kind: "unreachable" }
  | { kind: "failed"; message: string };

/**
 * Live backend status.
 *
 * This component is the frontend's first commitment to the project's honesty
 * rule: it renders exactly what the backend reports. An unconfigured
 * integration reads "Not configured" — never a green check
 * (`04_FRONTEND_SPEC.md` §1), and never an optimistic guess while loading.
 */
export function SystemStatus() {
  const [state, setState] = useState<Load>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;

    getHealth()
      .then((health) => {
        if (!cancelled) setState({ kind: "loaded", health });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState(
          error instanceof ApiUnreachableError
            ? { kind: "unreachable" }
            : {
                kind: "failed",
                message:
                  error instanceof Error ? error.message : "Unknown error.",
              },
        );
      });

    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") {
    return (
      <Panel title="Backend status">
        <p className="text-sm text-neutral-500">Checking…</p>
      </Panel>
    );
  }

  if (state.kind === "unreachable") {
    return (
      <Panel title="Backend status">
        <p className="text-sm text-neutral-700 dark:text-neutral-300">
          The API is not reachable.
        </p>
        <p className="mt-2 text-xs text-neutral-500">
          Start it with{" "}
          <code className="rounded bg-neutral-200 px-1 py-0.5 dark:bg-neutral-800">
            uvicorn backend.api.app:create_app --factory --reload
          </code>
          , then reload this page.
        </p>
      </Panel>
    );
  }

  if (state.kind === "failed") {
    return (
      <Panel title="Backend status">
        <p className="text-sm text-neutral-700 dark:text-neutral-300">
          {state.message}
        </p>
      </Panel>
    );
  }

  const { health } = state;

  return (
    <Panel title="Backend status">
      <dl className="grid gap-x-8 gap-y-3 sm:grid-cols-2">
        <Row label="Version" value={health.version} tone="neutral" />
        <Row
          label="Overall"
          value={health.status}
          tone={
            health.status === "ok"
              ? "good"
              : health.status === "degraded"
                ? "attention"
                : "bad"
          }
        />
        <Row
          label="Database"
          value={health.components.database === "ok" ? "Connected" : "Error"}
          tone={okTone(health.components.database)}
        />
        <Row
          label="Job queue"
          value={queueLabel(health.components.job_queue)}
          tone={queueTone(health.components.job_queue)}
        />
        <Row
          label="Amazon Bedrock"
          value={configuredLabel(health.components.bedrock)}
          tone={configuredTone(health.components.bedrock)}
        />
        <Row
          label="GitHub App"
          value={configuredLabel(health.components.github_app)}
          tone={configuredTone(health.components.github_app)}
        />
      </dl>
    </Panel>
  );
}

const okTone = (state: OkState) => (state === "ok" ? "good" : "bad");

const configuredLabel = (state: ConfiguredState) =>
  state === "configured" ? "Configured" : "Not configured";

const queueLabel = (state: QueueState) =>
  state === "ok" ? "Ready" : state === "error" ? "Error" : "Not configured";

/**
 * A queue that has not been built yet is neutral, not green and not a failure.
 * Rendering "Ready" for a component that does not exist would be a fake green
 * check on a page that tells the user nothing here is simulated.
 */
const queueTone = (state: QueueState): Tone =>
  state === "ok" ? "good" : state === "error" ? "bad" : "neutral";

/**
 * An unconfigured integration is neutral, not a warning. Nothing is broken —
 * Continuity simply has not been told how to reach it yet.
 */
const configuredTone = (state: ConfiguredState) =>
  state === "configured" ? "good" : "neutral";

type Tone = "good" | "attention" | "bad" | "neutral";

const TONE_CLASS: Record<Tone, string> = {
  good: "text-emerald-700 dark:text-emerald-400",
  attention: "text-amber-700 dark:text-amber-400",
  bad: "text-red-700 dark:text-red-400",
  neutral: "text-neutral-600 dark:text-neutral-400",
};

function Row({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: Tone;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-neutral-100 pb-2 dark:border-neutral-900">
      <dt className="text-sm text-neutral-500">{label}</dt>
      <dd className={`text-sm font-medium ${TONE_CLASS[tone]}`}>{value}</dd>
    </div>
  );
}

function Panel({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-6 dark:border-neutral-800 dark:bg-neutral-900">
      <h2 className="mb-4 text-sm font-semibold tracking-tight">{title}</h2>
      {children}
    </section>
  );
}
