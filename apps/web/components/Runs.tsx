"use client";

/**
 * §3.10 Agent Runs and §3.11 Migration report.
 *
 * The report shown here is the same one that becomes the pull request body,
 * built from the same rows — so what a reviewer reads on GitHub and what a
 * developer reads here cannot disagree.
 */

import { useState } from "react";

import { getReport, listRuns } from "../lib/api";
import { useApi, usePolling } from "../lib/useApi";
import {
  Badge,
  Card,
  Empty,
  Failed,
  Loading,
  When,
  humanise,
  toneForState,
} from "./ui/primitives";

export function Runs({ projectId }: { projectId: string }) {
  const { data, loading, error, refresh } = useApi(
    () => listRuns(projectId),
    [projectId],
  );
  const [openRun, setOpenRun] = useState<string | null>(null);
  usePolling(refresh);

  if (loading) return <Loading label="migration runs" />;
  if (error) return <Failed error={error} />;
  if (!data || data.length === 0) {
    return (
      <Card title="Migration runs">
        <Empty>
          No migrations yet. One opens when a provider change actually affects
          this project.
        </Empty>
      </Card>
    );
  }

  return (
    <Card title="Migration runs">
      <ul className="divide-y divide-neutral-200 dark:divide-neutral-800">
        {data.map((run) => (
          <li key={run.id} className="py-3 text-sm">
            <div className="flex items-center justify-between gap-3">
              <span className="min-w-0">
                <span className="block font-medium">
                  {run.provider_id} {run.from_version} → {run.to_version}
                </span>
                <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                  {run.attempts} attempt{run.attempts === 1 ? "" : "s"} ·{" "}
                  {run.findings} finding{run.findings === 1 ? "" : "s"} ·{" "}
                  <When iso={run.created_at} />
                </span>
              </span>
              <span className="flex shrink-0 items-center gap-2">
                {run.pull_request ? (
                  <Badge tone="success">PR #{run.pull_request}</Badge>
                ) : null}
                <Badge tone={toneForState(run.state)}>
                  {humanise(run.state)}
                </Badge>
                <button
                  type="button"
                  onClick={() =>
                    setOpenRun((current) => (current === run.id ? null : run.id))
                  }
                  className="rounded border border-neutral-300 px-2 py-0.5 text-xs hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
                >
                  {openRun === run.id ? "Hide report" : "Report"}
                </button>
              </span>
            </div>
            {openRun === run.id ? (
              <Report projectId={projectId} runId={run.id} />
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function Report({ projectId, runId }: { projectId: string; runId: string }) {
  const { data, loading, error } = useApi(
    () => getReport(projectId, runId),
    [projectId, runId],
  );

  if (loading) return <Loading label="report" />;
  if (error) return <Failed error={error} />;
  if (!data) return <Empty>No report for this run.</Empty>;

  return (
    <pre className="mt-3 max-h-96 overflow-auto rounded bg-neutral-100 p-3 text-xs whitespace-pre-wrap dark:bg-neutral-800">
      {data.markdown}
    </pre>
  );
}
