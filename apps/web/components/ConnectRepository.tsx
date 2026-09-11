"use client";

/**
 * §3.2–3.4: connect a repository, import it, and watch it become monitored.
 *
 * The entry point the product did not have. Everything downstream — the graph,
 * the baseline, monitoring — depends on this happening, and until it existed a
 * project could only be created by writing to the database.
 *
 * Two honesty rules it carries:
 *
 * - The list comes from the GitHub App installation, not from anything the
 *   browser can name. Signing in with Google does not authorize a repository.
 * - A project without a local checkout is imported and says plainly that it
 *   will be monitored but not migrated, rather than letting the user find out
 *   when a pull request never arrives.
 */

import Link from "next/link";
import { useState } from "react";

import {
  type ProjectCreated,
  type ScanResult,
  importRepository,
  listAuthorizedRepositories,
  scanProject,
} from "../lib/api";
import { useApi } from "../lib/useApi";
import { Badge, Card, Empty, Failed, Loading } from "./ui/primitives";

export function ConnectRepository() {
  const repositories = useApi(listAuthorizedRepositories);

  const [selected, setSelected] = useState<string | null>(null);
  const [localPath, setLocalPath] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [imported, setImported] = useState<ProjectCreated | null>(null);
  const [scan, setScan] = useState<ScanResult | null>(null);
  const [failure, setFailure] = useState<unknown>(null);

  const run = async () => {
    if (!selected) return;
    setFailure(null);
    setBusy("Importing…");
    try {
      const project = await importRepository(selected, localPath.trim());
      setImported(project);
      // Scanning is the slow part. It is a separate call so a failure here is
      // distinguishable from a failure to import.
      setBusy("Scanning, extracting integrations, and building the graph…");
      setScan(await scanProject(project.project_id));
    } catch (cause) {
      setFailure(cause);
    } finally {
      setBusy(null);
    }
  };

  if (repositories.loading) return <Loading label="authorized repositories" />;
  if (repositories.error) return <Failed error={repositories.error} />;

  return (
    <div className="space-y-6">
      <Card title="Authorized repositories">
        <p className="mb-3 text-xs text-neutral-500 dark:text-neutral-400">
          These come from your Continuity GitHub App installation. Signing in
          with Google identifies you; it does not grant access to any
          repository.
        </p>

        {!repositories.data || repositories.data.length === 0 ? (
          <Empty>
            No repositories are authorized yet. Install the Continuity GitHub
            App on the repositories you want monitored.
          </Empty>
        ) : (
          <ul className="divide-y divide-neutral-200 dark:divide-neutral-800">
            {repositories.data.map((repository) => (
              <li
                key={repository.full_name}
                className="flex items-center justify-between gap-3 py-2 text-sm"
              >
                <label className="flex min-w-0 items-center gap-2">
                  <input
                    type="radio"
                    name="repository"
                    value={repository.full_name}
                    checked={selected === repository.full_name}
                    onChange={() => setSelected(repository.full_name)}
                  />
                  <span className="truncate">{repository.full_name}</span>
                </label>
                <span className="shrink-0 text-xs text-neutral-500 dark:text-neutral-400">
                  {repository.default_branch}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Local checkout (optional)">
        <p className="mb-2 text-xs text-neutral-500 dark:text-neutral-400">
          Continuity reads your repository through the GitHub App, which gives
          it the contents but not a working tree. A migration needs somewhere to
          run your tests — without a checkout this project is monitored and
          assessed, and no pull request is opened.
        </p>
        <input
          type="text"
          value={localPath}
          onChange={(event) => setLocalPath(event.target.value)}
          placeholder="/path/to/your/checkout"
          className="w-full rounded border border-neutral-300 bg-white px-3 py-2 font-mono text-sm dark:border-neutral-700 dark:bg-neutral-900"
        />
      </Card>

      {failure ? <Failed error={failure} /> : null}

      <button
        type="button"
        disabled={!selected || busy !== null}
        onClick={run}
        className="rounded bg-sky-700 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 hover:bg-sky-800"
      >
        {busy ?? "Import and scan"}
      </button>

      {busy ? (
        <p role="status" className="text-sm text-neutral-500 dark:text-neutral-400">
          {busy}
        </p>
      ) : null}

      {imported && !scan ? (
        <Card title="Imported">
          <p className="text-sm">{imported.note}</p>
        </Card>
      ) : null}

      {scan ? (
        <Card
          title="Scan complete"
          action={
            <Badge tone={scan.monitorable ? "success" : "attention"}>
              {scan.monitorable ? "Monitoring" : scan.state}
            </Badge>
          }
        >
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
            <Figure label="Files indexed" value={scan.files_indexed} />
            <Figure label="Graph nodes" value={scan.confirmed_nodes} />
            <Figure label="Workflows" value={scan.inferred_workflows} />
            <Figure label="Providers" value={scan.providers} />
          </dl>

          {scan.mapping_degraded ? (
            <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">
              Workflow inference was skipped: {scan.mapping_degraded}. The graph
              holds confirmed extraction only.
            </p>
          ) : null}

          {imported?.can_migrate === false ? (
            <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">
              No local checkout, so Continuity will detect and assess provider
              changes for this project but will not open pull requests.
            </p>
          ) : null}

          <Link
            href={`/projects/${scan.project_id}`}
            className="mt-4 inline-block rounded border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
          >
            Open project
          </Link>
        </Card>
      ) : null}
    </div>
  );
}

function Figure({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
        {label}
      </dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}
