import { ProjectsList } from "../../components/ProjectsList";

export const metadata = { title: "Projects — Continuity" };

export default function ProjectsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold tracking-tight">Projects</h1>
      <ProjectsList />
    </div>
  );
}
