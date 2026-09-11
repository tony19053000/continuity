"use client";

/**
 * §3.6 Project Overview — the numbers, and where each one comes from.
 *
 * The Integration Health score is shown with its formula attached, because a
 * number nobody can check is not evidence. When its inputs are absent the score
 * is not rendered at all — the spec is explicit that a placeholder would be a
 * lie about system state.
 */

import { getProject, listActivity } from "../lib/api";
import { useApi, usePolling } from "../lib/useApi";
import {
  Badge,
  Card,
  Empty,
  Failed,
  Loading,
  Stat,
  When,
  humanise,
  toneForState,
} from "./ui/primitives";

export function ProjectOverview({ projectId }: { projectId: string }) {
  const project = useApi(() => getProject(projectId), [projectId]);
  const activity = useApi(() => listActivity(projectId), [projectId]);

  // A pipeline pass takes minutes. Without this the page looks hung while the
  // system is working.
  usePolling(() => {
    project.refresh();
    activity.refresh();
  });

  if (project.loading) return <Loading label="project" />;
  if (project.error) return <Failed error={project.error} />;
  if (!project.data) return <Empty>That project could not be found.</Empty>;

  const { health } = project.data;

  return (
    <div className="space-y-6">
      <Card
        title="Integration health"
        action={
          <Badge tone={toneForState(project.data.state)}>
            {humanise(project.data.state)}
          </Badge>
        }
      >
        {health.available ? (
          <>
            <p className="text-4xl font-semibold tabular-nums">
              {health.score}%
            </p>
            <details className="mt-3">
              <summary className="cursor-pointer text-xs text-neutral-500 hover:underline dark:text-neutral-400">
                How this is calculated
              </summary>
              <p className="mt-2 font-mono text-xs text-neutral-600 dark:text-neutral-300">
                {health.formula}
              </p>
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                {Object.entries(health.inputs).map(([name, value]) => (
                  <div key={name} className="contents">
                    <dt className="text-neutral-500 dark:text-neutral-400">
                      {humanise(name)}
                    </dt>
                    <dd className="tabular-nums">{value}</dd>
                  </div>
                ))}
              </dl>
            </details>
          </>
        ) : (
          <Empty>
            No score yet — {health.unavailable_reason ?? "its inputs are not recorded."}
          </Empty>
        )}
      </Card>

      <Card title="At a glance">
        <dl className="grid grid-cols-2 gap-6 sm:grid-cols-4">
          <Stat label="Providers" value={project.data.providers} />
          <Stat
            label="Integration points"
            value={project.data.integration_points}
          />
          <Stat label="Open changes" value={project.data.open_changes} />
          <Stat
            label="Pending approvals"
            value={project.data.pending_approvals}
          />
        </dl>
      </Card>

      <Card title="Activity">
        {activity.loading ? (
          <Loading label="activity" />
        ) : activity.error ? (
          <Failed error={activity.error} />
        ) : !activity.data || activity.data.length === 0 ? (
          <Empty>Nothing has happened on this project yet.</Empty>
        ) : (
          <ol className="space-y-3">
            {activity.data.map((event) => (
              <li key={event.id} className="flex gap-3 text-sm">
                <span className="w-40 shrink-0 text-xs text-neutral-500 dark:text-neutral-400">
                  <When iso={event.occurred_at} />
                </span>
                <span className="min-w-0">
                  <span className="font-medium">{humanise(event.kind)}</span>{" "}
                  <span className="text-neutral-500 dark:text-neutral-400">
                    · {event.actor}
                  </span>
                  <span className="block text-neutral-600 dark:text-neutral-300">
                    {event.summary}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}
