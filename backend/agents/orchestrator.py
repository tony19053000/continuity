"""The Orchestrator agent.

It proposes; `RunCoordinator` disposes. This agent has no tools, cannot write an
approval, and cannot move a run — `transition()` is not exposed to it, and
`apply_proposal` validates every proposal against `ALLOWED_TRANSITIONS` before
acting.

That separation is why the Orchestrator existing as a model at all is safe: the
worst a confused or manipulated proposal can do is name an illegal state, which
escalates the run to a human instead of advancing it.
"""

from __future__ import annotations

from backend.agents.base import AgentContract, ContinuityAgent
from backend.agents.contracts import OrchestratorInput, OrchestratorOutput
from backend.models.enums import AgentRole


class OrchestratorAgent(ContinuityAgent[OrchestratorInput, OrchestratorOutput]):
    contract = AgentContract(
        role=AgentRole.ORCHESTRATOR,
        system_prompt="""You are the Orchestrator of Continuity, an autonomous
integration reliability platform. You coordinate a migration run.

You propose the next workflow state given the current state and the evidence
gathered so far. You do not perform the transition — deterministic application
code validates your proposal against the allowed transition table and applies it
only if it is legal. A proposal naming an unreachable state stops the run for
human review, so guessing costs the team time rather than gaining you progress.

Never claim work happened that the evidence does not show. If the evidence is
insufficient to move forward, say so in blocking_reason instead of proposing a
state.

You have no tools and no authority. You cannot approve anything, and you cannot
skip validation, security review, or human approval.""",
        allowed_tools=frozenset(),
        input_model=OrchestratorInput,
        output_model=OrchestratorOutput,
        max_attempts=2,
    )

    def build_prompt(self, task: OrchestratorInput) -> str:
        evidence = "\n".join(f"- {item}" for item in task.available_evidence) or "- none"
        return (
            f"Migration run: {task.migration_run_id}\n"
            f"Current state: {task.current_state}\n\n"
            f"Evidence so far:\n{evidence}\n\n"
            "Propose the next state, or explain what is blocking progress."
        )
