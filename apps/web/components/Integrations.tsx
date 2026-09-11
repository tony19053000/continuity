"use client";

/** §3.7 Integrations — what this project depends on, and when each was checked. */

import { listIntegrations } from "../lib/api";
import { useApi } from "../lib/useApi";
import {
  Badge,
  Card,
  ConfidenceMark,
  Empty,
  Failed,
  Loading,
  When,
} from "./ui/primitives";

export function Integrations({ projectId }: { projectId: string }) {
  const { data, loading, error } = useApi(
    () => listIntegrations(projectId),
    [projectId],
  );

  if (loading) return <Loading label="integrations" />;
  if (error) return <Failed error={error} />;
  if (!data || data.length === 0) {
    return (
      <Card title="Integrations">
        <Empty>
          No providers detected. Scan the repository to find its integrations.
        </Empty>
      </Card>
    );
  }

  return (
    <Card title="Integrations">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
          <tr>
            <th scope="col" className="pb-2 font-medium">Provider</th>
            <th scope="col" className="pb-2 font-medium">Version</th>
            <th scope="col" className="pb-2 font-medium">Auth</th>
            <th scope="col" className="pb-2 text-right font-medium">Points</th>
            <th scope="col" className="pb-2 font-medium">Last checked</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-200 dark:divide-neutral-800">
          {data.map((integration) => (
            <tr key={integration.provider_id}>
              <td className="py-2">
                {integration.display_name}
                <ConfidenceMark confidence={integration.confidence} />
                {integration.sdk_package ? (
                  <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                    {integration.sdk_package}
                  </span>
                ) : null}
              </td>
              {/* An em dash, not a guess: the scan did not determine this. */}
              <td className="py-2 tabular-nums">
                {integration.detected_api_version ?? "—"}
              </td>
              <td className="py-2">{integration.auth_mechanism ?? "—"}</td>
              <td className="py-2 text-right tabular-nums">
                {integration.integration_points}
              </td>
              <td className="py-2 text-xs">
                {integration.last_check_error ? (
                  <Badge tone="critical" title={integration.last_check_error}>
                    check failed
                  </Badge>
                ) : (
                  <When iso={integration.last_checked_at} />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
