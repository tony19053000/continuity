"use client";

/**
 * One project, and everything Continuity knows about it.
 *
 * Tabbed rather than one long page: overview, integrations, changes, runs,
 * graph, and security are separate questions, and a reader arrives with one of
 * them in mind. The tab lives in the URL so a link to "this project's security
 * findings" is a link someone can send.
 */

import { useState } from "react";

import { Changes } from "./Changes";
import { GraphView } from "./GraphView";
import { Integrations } from "./Integrations";
import { ProjectOverview } from "./ProjectOverview";
import { Runs } from "./Runs";
import { SecurityPage } from "./SecurityPage";

const TABS = [
  "overview",
  "integrations",
  "changes",
  "runs",
  "graph",
  "security",
] as const;

type Tab = (typeof TABS)[number];

export function ProjectTabs({ projectId }: { projectId: string }) {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <div className="space-y-6">
      <nav
        aria-label="Project sections"
        className="flex flex-wrap gap-1 border-b border-neutral-200 dark:border-neutral-800"
      >
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            aria-current={tab === name ? "page" : undefined}
            onClick={() => setTab(name)}
            className={`rounded-t px-3 py-2 text-sm capitalize focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 ${
              tab === name
                ? "border-b-2 border-sky-600 font-medium text-sky-700 dark:text-sky-300"
                : "text-neutral-600 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
            }`}
          >
            {name}
          </button>
        ))}
      </nav>

      {tab === "overview" ? <ProjectOverview projectId={projectId} /> : null}
      {tab === "integrations" ? <Integrations projectId={projectId} /> : null}
      {tab === "changes" ? <Changes projectId={projectId} /> : null}
      {tab === "runs" ? <Runs projectId={projectId} /> : null}
      {tab === "graph" ? <GraphView projectId={projectId} /> : null}
      {tab === "security" ? <SecurityPage projectId={projectId} /> : null}
    </div>
  );
}
