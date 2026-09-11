"use client";

/**
 * §3.5 Projects — the grid of what Continuity is watching.
 *
 * Every value is read from `/projects`. There is no hardcoded provider, count,
 * or status anywhere in this file, which is the rule that makes the screen
 * trustworthy: if the backend has no data, the screen says so rather than
 * showing a plausible number.
 */

import Link from "next/link";

import { listProjects } from "../lib/api";
import { useApi } from "../lib/useApi";
import {
  Badge,
  Card,
  Empty,
  Failed,
  Loading,
  humanise,
  toneForState,
} from "./ui/primitives";

export function ProjectsList() {
  const { data, loading, error } = useApi(listProjects);

  if (loading) return <Loading label="projects" />;
  if (error) return <Failed error={error} />;
  if (!data || data.length === 0) {
    return (
      <Card title="Projects">
        <Empty>
          No projects yet. Connect a repository to start monitoring its
          integrations.
        </Empty>
      </Card>
    );
  }

  return (
    <Card title="Projects">
      <ul className="divide-y divide-neutral-200 dark:divide-neutral-800">
        {data.map((project) => (
          <li key={project.id} className="py-3">
            <Link
              href={`/projects/${project.id}`}
              className="flex items-center justify-between gap-4 rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500"
            >
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium">
                  {project.name}
                </span>
                <span className="block truncate text-xs text-neutral-500 dark:text-neutral-400">
                  {project.repository ?? "no repository linked"} ·{" "}
                  {project.providers} provider
                  {project.providers === 1 ? "" : "s"} ·{" "}
                  {project.integration_points} integration point
                  {project.integration_points === 1 ? "" : "s"}
                </span>
              </span>
              <span className="flex shrink-0 items-center gap-2">
                {project.pending_approvals > 0 ? (
                  <Badge tone="attention">
                    {project.pending_approvals} awaiting approval
                  </Badge>
                ) : null}
                {/* A score with no inputs is not rendered at all: a
                    placeholder number would be a lie about system state. */}
                {project.health.available ? (
                  <span
                    title={project.health.formula ?? undefined}
                    className="text-sm tabular-nums text-neutral-600 dark:text-neutral-300"
                  >
                    {project.health.score}%
                  </span>
                ) : null}
                <Badge tone={toneForState(project.state)}>
                  {humanise(project.state)}
                </Badge>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}
