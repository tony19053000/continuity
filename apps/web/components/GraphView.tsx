"use client";

/**
 * §3.8 Integration Intelligence Graph.
 *
 * Rendered as grouped lists rather than a force-directed drawing. The graph's
 * value is knowing *what a change reaches*, and a readable inventory answers
 * that better than a hairball — while keeping the confirmed/inferred marker
 * visible on every node, which a canvas drawing would lose.
 */

import { getGraph } from "../lib/api";
import { useApi } from "../lib/useApi";
import {
  Card,
  ConfidenceMark,
  Empty,
  Failed,
  Loading,
  humanise,
} from "./ui/primitives";

export function GraphView({ projectId }: { projectId: string }) {
  const { data, loading, error } = useApi(
    () => getGraph(projectId),
    [projectId],
  );

  if (loading) return <Loading label="graph" />;
  if (error) return <Failed error={error} />;
  if (!data || data.version === null) {
    return (
      <Card title="Integration Intelligence Graph">
        <Empty>
          No graph yet. It is built when the repository is scanned.
        </Empty>
      </Card>
    );
  }

  const byKind = new Map<string, typeof data.nodes>();
  for (const node of data.nodes) {
    byKind.set(node.kind, [...(byKind.get(node.kind) ?? []), node]);
  }

  return (
    <Card title={`Integration Intelligence Graph — version ${data.version}`}>
      <p className="mb-4 text-xs text-neutral-500 dark:text-neutral-400">
        {data.nodes.length} nodes, {data.edges.length} edges.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        {[...byKind.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([kind, nodes]) => (
            <div key={kind}>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
                {humanise(kind)} ({nodes.length})
              </h3>
              <ul className="mt-1 space-y-0.5">
                {nodes.map((node) => (
                  <li key={node.id} className="truncate text-xs">
                    {node.label}
                    <ConfidenceMark confidence={node.confidence} />
                  </li>
                ))}
              </ul>
            </div>
          ))}
      </div>
    </Card>
  );
}
