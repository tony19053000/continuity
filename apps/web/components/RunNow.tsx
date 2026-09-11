"use client";

/**
 * Run one pass now, and say plainly what happened.
 *
 * The scheduler sweeps on an interval — an hour by default — which is right for
 * running unattended and useless for watching the product work. This is the
 * button that makes the loop observable by hand.
 *
 * Deliberately plain. It exists so the loop can be *verified*, not to be the
 * product's surface: every figure is read straight from the response, nothing
 * is styled to look more successful than it was, and the limitations the
 * backend reports are shown above the result rather than below it. A pass that
 * checked no providers because none are configured must not read as a pass that
 * found nothing.
 */

import { useState } from "react";

import { runProjectNow, type RunNowResult } from "../lib/api";
import { Card, Failed } from "./ui/primitives";

export function RunNow({ projectId }: { projectId: string }) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<RunNowResult | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(await runProjectNow(projectId));
    } catch (caught) {
      setError(caught);
    } finally {
      setRunning(false);
    }
  }

  return (
    <Card title="Run a pass now">
      <p className="text-sm text-neutral-600 dark:text-neutral-300">
        Checks this project&apos;s providers, decides whether anything affects
        it, and — if it does — migrates, validates, reviews, and opens a pull
        request. The same call the scheduler makes.
      </p>

      <button
        type="button"
        onClick={run}
        disabled={running}
        className="mt-3 rounded border border-neutral-300 px-3 py-1.5 text-sm font-medium disabled:opacity-50 dark:border-neutral-700"
      >
        {running ? "Running…" : "Run now"}
      </button>

      {running ? (
        <p className="mt-3 text-sm text-neutral-500">
          This runs the real pipeline, including the project&apos;s own test
          suite. It can take a while.
        </p>
      ) : null}

      {error ? <div className="mt-3"><Failed error={error} /></div> : null}

      {result ? (
        <div className="mt-4 space-y-4 text-sm">
          {result.limitations.length > 0 ? (
            <div className="rounded border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950">
              <p className="font-medium">
                This deployment could not do everything:
              </p>
              <ul className="mt-1 list-disc pl-5">
                {result.limitations.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}

          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-4">
            <Figure
              label="Providers monitored"
              value={result.providers_monitored}
              of={result.providers_seen}
            />
            <Figure label="Changes recorded" value={result.changes_recorded} />
            <Figure label="Affect this project" value={result.relevant} />
            <Figure label="Pull requests" value={result.pull_requests.length} />
          </dl>

          {result.unmonitored.length > 0 ? (
            <div>
              <p className="font-medium">Not monitored</p>
              <ul className="mt-1 list-disc pl-5 text-neutral-600 dark:text-neutral-300">
                {result.unmonitored.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}

          <div>
            <p className="font-medium">What happened</p>
            <ol className="mt-1 space-y-1">
              {result.stages.map((stage, index) => (
                <li key={`${stage.name}-${index}`} className="flex gap-2">
                  <span className="shrink-0 font-mono text-xs text-neutral-500">
                    {stage.name}
                  </span>
                  <span className="text-neutral-700 dark:text-neutral-300">
                    {stage.detail}
                  </span>
                </li>
              ))}
            </ol>
          </div>

          {result.pull_requests.length > 0 ? (
            <p>
              Opened pull request
              {result.pull_requests.length > 1 ? "s" : ""}{" "}
              {result.pull_requests.map((number) => `#${number}`).join(", ")}.
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function Figure({
  label,
  value,
  of,
}: {
  label: string;
  value: number;
  /** The larger set this is a part of, when the difference matters. */
  of?: number;
}) {
  return (
    <div>
      <dt className="text-xs text-neutral-500 dark:text-neutral-400">{label}</dt>
      <dd className="text-lg font-medium tabular-nums">
        {value}
        {of !== undefined && of !== value ? (
          <span className="text-sm text-neutral-500"> of {of}</span>
        ) : null}
      </dd>
    </div>
  );
}
