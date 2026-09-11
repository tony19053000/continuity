import { ProjectTabs } from "../../../components/ProjectTabs";

export const metadata = { title: "Project — Continuity" };

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ProjectTabs projectId={id} />;
}
