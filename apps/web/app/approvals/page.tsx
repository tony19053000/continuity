import { Approvals } from "../../components/Approvals";

export const metadata = { title: "Approvals — Continuity" };

export default function ApprovalsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold tracking-tight">Approvals</h1>
      <p className="text-sm text-neutral-600 dark:text-neutral-300">
        Continuity pauses here and waits. Nothing below has happened yet.
      </p>
      <Approvals />
    </div>
  );
}
