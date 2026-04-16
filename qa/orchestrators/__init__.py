# qa/orchestrators/ — Python-driven orchestration for multi-step flows
#
# Each orchestrator describes ONE interaction pattern at page scale.
# Python drives the sequence; the LLM is invoked only for narrow perception
# or classification sub-tasks where its judgment actually helps.
#
# vs the single-LLM-agent approach:
#   - Context stays small (each sub-task gets a fresh prompt + snapshot)
#   - Failure is per-section, not all-or-nothing
#   - KB is checkpointed after each section — nothing is lost on timeout

from qa.orchestrators.base import Orchestrator, RunContext

__all__ = ["Orchestrator", "RunContext"]
