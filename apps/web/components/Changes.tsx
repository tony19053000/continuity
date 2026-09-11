"use client";

/** §3.9 Changes — what each provider shipped, and whether it reached this code. */

import Link from "next/link";

import { listChanges } from "../lib/api";
import { useApi, usePolling } from "../lib/useApi";
import {
  Badge,
  Card,
  Empty,
  Failed,
  Loading,
  When,
  humanise,
} from "./ui/primitives";

export function Changes({ projectId }: { projectId: string }) {
  const { data, loading, error, refresh } = useApi(
    () => listChanges(projectId),
    [projectId],
  );
  usePolling(refresh, 10000);

  if (loading) return <Loading label="changes" />;
  if (error) return <Failed error={error} />;
  if (!data || data.length === 0) {
    return (
      <Card title="Provider changes">
        <Empty>
          No provider changes detected yet. Continuity is watching.
        </Empty>
      </Card>
    );
  }

  return (
    <Card title="Provider changes">
      <ul className="divide-y divide-neutral-200 dark:divide-neutral-800">
        {data.map((change) => (
          <li key={change.id} className="flex items-start gap-3 py-3 text-sm">
            <span className="flex shrink-0 gap-1.5">
              {change.breaking ? (
                <Badge tone="critical">breaking</Badge>
              ) : (
                <Badge tone="neutral">additive</Badge>
              )}
              {change.security_relevant ? (
                <Badge tone="attention">security</Badge>
              ) : null}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block font-medium">
                {humanise(change.change_type)}
              </span>
              <span className="block truncate font-mono text-xs text-neutral-600 dark:text-neutral-300">
                {change.resource}
              </span>
              <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                {change.provider_id} {change.old_version} → {change.new_version}
                {" · "}
                <When iso={change.detected_at} />
              </span>
            </span>
            {/* A change with no run reached no code. Saying so is the
                product's most frequent correct answer. */}
            <span className="shrink-0 text-xs">
              {change.migration_run_id ? (
                <Link
                  href={`/projects/${projectId}?tab=runs`}
                  className="text-sky-700 hover:underline dark:text-sky-300"
                >
                  migration opened
                </Link>
              ) : (
                <span className="text-neutral-500 dark:text-neutral-400">
                  no code affected
                </span>
              )}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
