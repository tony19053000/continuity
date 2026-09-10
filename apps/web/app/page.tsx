import { SystemStatus } from "@/components/SystemStatus";

export default function Home() {
  return (
    <div className="space-y-10">
      <section>
        <h1 className="text-2xl font-semibold tracking-tight">Continuity</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-600 dark:text-neutral-400">
          Continuity monitors the third-party APIs your application depends on,
          determines whether a provider change actually affects your code,
          prepares and validates a migration, and delivers a reviewable pull
          request.
        </p>
      </section>

      <SystemStatus />

      <section className="rounded-lg border border-dashed border-neutral-300 p-6 dark:border-neutral-700">
        <h2 className="text-sm font-semibold tracking-tight">
          What is not here yet
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-neutral-600 dark:text-neutral-400">
          This is the Phase 1 shell. Repository import, the Integration
          Intelligence Graph, agent runs, and pull request delivery arrive in
          later phases. Nothing on this page is simulated — every value shown
          comes from the running backend.
        </p>
        <p className="mt-4 text-sm">
          <a
            href="/signin"
            className="font-medium underline underline-offset-4 hover:no-underline"
          >
            Sign in
          </a>
          <span className="text-neutral-600 dark:text-neutral-400">
            {" "}
            — available now, and separate from granting repository access.
          </span>
        </p>
      </section>
    </div>
  );
}
