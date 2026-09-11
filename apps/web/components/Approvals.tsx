"use client";

/**
 * §3.11 Approval — the human-control surface.
 *
 * Two properties the spec asks for, and both are load-bearing:
 *
 * - **The buttons write backend state.** Nothing here is optimistic. The card
 *   re-reads after the decision, so what is shown is what the database says,
 *   not what the click intended.
 * - **The run stays paused until it changes.** This screen does not advance
 *   anything; it records a decision, and the pipeline notices on its next pass.
 */

import { useState } from "react";

import { decideApproval, listApprovals } from "../lib/api";
import { useApi, usePolling } from "../lib/useApi";
import {
  Badge,
  Card,
  Empty,
  Failed,
  Loading,
  When,
  humanise,
  toneForSeverity,
} from "./ui/primitives";

export function Approvals() {
  const { data, loading, error, refresh } = useApi(listApprovals);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  usePolling(refresh, 10000);

  const decide = async (id: string, decision: "approve" | "reject") => {
    setBusy(id);
    setFailure(null);
    try {
      await decideApproval(id, decision);
      // Re-read rather than assume. A decision that failed server-side must
      // not leave the card looking decided.
      refresh();
    } catch (cause) {
      setFailure(cause);
    } finally {
      setBusy(null);
    }
  };

  if (loading) return <Loading label="approvals" />;
  if (error) return <Failed error={error} />;

  return (
    <Card title="Awaiting your decision">
      {failure ? <Failed error={failure} /> : null}
      {!data || data.length === 0 ? (
        <Empty>Nothing needs your approval right now.</Empty>
      ) : (
        <ul className="divide-y divide-neutral-200 dark:divide-neutral-800">
          {data.map((approval) => (
            <li key={approval.id} className="py-4 text-sm">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="font-medium">{humanise(approval.trigger)}</p>
                  <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">
                    Requested <When iso={null} />
                    {approval.agent_recommendation
                      ? ` · agent recommended ${approval.agent_recommendation}`
                      : null}
                  </p>
                  <pre className="mt-2 overflow-auto rounded bg-neutral-100 p-2 text-xs dark:bg-neutral-800">
                    {JSON.stringify(approval.requested_action, null, 2)}
                  </pre>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-2">
                  <Badge tone={toneForSeverity(approval.risk)}>
                    {approval.risk} risk
                  </Badge>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      disabled={busy === approval.id}
                      onClick={() => decide(approval.id, "approve")}
                      className="rounded bg-emerald-600 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 hover:bg-emerald-700"
                    >
                      {busy === approval.id ? "Saving…" : "Approve"}
                    </button>
                    <button
                      type="button"
                      disabled={busy === approval.id}
                      onClick={() => decide(approval.id, "reject")}
                      className="rounded border border-neutral-300 px-3 py-1 text-xs font-medium disabled:opacity-50 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
                    >
                      Reject
                    </button>
                  </div>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
