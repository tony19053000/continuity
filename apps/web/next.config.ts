import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next.js generates its own AGENTS.md and CLAUDE.md on dev start. This
  // repository already has a root CLAUDE.md that defines the engineering
  // process, and a second, auto-regenerated one inside apps/web would compete
  // with it and silently reappear after every `npm run dev`.
  agentRules: false,
};

export default nextConfig;
