import { ConnectRepository } from "../../components/ConnectRepository";

export const metadata = { title: "Connect a repository — Continuity" };

export default function ConnectPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold tracking-tight">
        Connect a repository
      </h1>
      <p className="text-sm text-neutral-600 dark:text-neutral-300">
        Continuity will index it, map its third-party integrations, record a
        baseline, and start watching those providers for changes.
      </p>
      <ConnectRepository />
    </div>
  );
}
