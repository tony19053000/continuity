"use client";

/**
 * §3.12 Security — findings, and what the system can honestly claim.
 *
 * Two things this page must never do, both from `03_SECURITY_ACCESS.md`:
 *
 * - Show an agent's recommendation as though it were the decision. Both
 *   verdicts are displayed, and a disagreement is marked rather than smoothed
 *   over.
 * - Claim a security posture the backend has not demonstrated. The execution
 *   row reports the active provider's real behaviour, and attestation reports
 *   "Not Configured" because no attestation exists — §7's honesty rule.
 */

import { listFindings } from "../lib/api";
import { useApi } from "../lib/useApi";
import {
  Badge,
  Card,
  Empty,
  Failed,
  Loading,
  humanise,
  toneForSeverity,
} from "./ui/primitives";

export function SecurityPage({ projectId }: { projectId: string }) {
  const { data, loading, error } = useApi(
    () => listFindings(projectId),
    [projectId],
  );

  return (
    <div className="space-y-6">
      <Card title="Security posture">
        <dl className="space-y-2 text-sm">
          <Row
            label="Secret filtering"
            value="Active — before context construction, before storage, before display"
          />
          <Row
            label="Policy enforcement"
            value="Deterministic — backend/security/policy.py decides; agents recommend"
          />
          <Row
            label="Secure execution"
            value="Development Isolation — process isolation, not sandboxing"
          />
          {/* Not a placeholder. No attestation exists, and claiming one would
              violate 03_SECURITY_ACCESS.md §7. */}
          <Row label="TEE attestation" value="Not Configured" tone="neutral" />
        </dl>
      </Card>

      <Card title="Findings">
        {loading ? (
          <Loading label="findings" />
        ) : error ? (
          <Failed error={error} />
        ) : !data || data.length === 0 ? (
          <Empty>No security findings on this project.</Empty>
        ) : (
          <ul className="divide-y divide-neutral-200 dark:divide-neutral-800">
            {data.map((finding) => (
              <li
                key={finding.id}
                className={
                  finding.superseded ? "py-3 text-sm opacity-60" : "py-3 text-sm"
                }
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-medium">{humanise(finding.category)}</p>
                    <p className="text-neutral-600 dark:text-neutral-300">
                      {finding.summary}
                    </p>
                    <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">
                      Reviewer advised{" "}
                      <strong>{finding.recommendation}</strong> · policy ruled{" "}
                      <strong>{finding.policy_decision}</strong>
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <Badge tone={toneForSeverity(finding.severity)}>
                      {finding.severity}
                    </Badge>
                    {finding.superseded ? (
                      <Badge
                        tone="neutral"
                        title="This finding is about a patch a later attempt replaced. It is kept as a record of why that attempt was rejected, and does not describe the code being delivered."
                      >
                        superseded
                      </Badge>
                    ) : null}
                    {finding.disagreed ? (
                      <Badge
                        tone="attention"
                        title="The Security Reviewer and the policy engine reached different answers. Policy is binding; the disagreement is recorded."
                      >
                        disagreement
                      </Badge>
                    ) : null}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function Row({
  label,
  value,
  tone = "success",
}: {
  label: string;
  value: string;
  tone?: "success" | "neutral";
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-neutral-100 pb-2 last:border-0 dark:border-neutral-800">
      <dt className="text-neutral-500 dark:text-neutral-400">{label}</dt>
      <dd className="text-right">
        <Badge tone={tone}>{value}</Badge>
      </dd>
    </div>
  );
}
