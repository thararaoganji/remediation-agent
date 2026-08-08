"""
ADK wiring for the sonar auto-fix workflow.

Design rule carried over from the review: LlmAgent is used ONLY for
Section 6 fix generation. Every orchestration decision — branch setup,
prioritization, cluster classification, checkpoint gating, loop exit — is a
custom BaseAgent making a deterministic decision from session.state and
tool results. This keeps Principle #2 (orchestration never varies with
language or issue count) actually true at the framework level, not just on
paper: an LlmAgent could always decide to improvise, a BaseAgent can't.

This package builds `pipeline_agent`, the deterministic Sonar remediation
graph. It is not the package's `root_agent` — `intake.py` wraps it behind a
conversational front door that gathers the repo location first; see
`sonar_autofix_agent/__init__.py`.

Composition (top to bottom), and which submodule each piece lives in:

  pipeline_agent (SequentialAgent)                              [__init__.py]
  ├── setup_step           (BaseAgent)  -- Phase I               [setup.py]
  ├── outer_loop           (LoopAgent, max_iterations = MAX_OUTER_ITERATIONS)
  │     │                                                        [outer_loop.py]
  │     ├── fetch_prioritize_step (BaseAgent) -- Phase II, called once + each outer iter
  │     ├── per_file_loop_step    (BaseAgent) -- wraps per_file_loop, strips its escalate
  │     │     └── per_file_loop           (LoopAgent, iterations = queue length)
  │     │                                                        [fix.py]
  │     │           ├── file_fixer_step   (BaseAgent)   -- 5.1/5.2 prep + deterministic pre-pass
  │     │           ├── fix_llm_gate_step (BaseAgent)   -- calls fix_llm_agent unless every issue
  │     │           │     └── fix_llm_agent (LlmAgent)  -- was resolved deterministically already
  │     │           ├── apply_and_verify_step (BaseAgent) -- apply diff, compile check, verify,
  │     │           │                                        narrow retry on a verified miss
  │     │           └── checkpoint_gate   (BaseAgent)   -- fires checkpoint_pipeline conditionally
  │     │                 └── checkpoint_pipeline (SequentialAgent) -- Section 5.4
  │     │                                                        [checkpoint.py]
  │     └── outer_exit_check (BaseAgent) -- escalate=True when queue empty or max hit
  ├── maintainability_expansion_loop (LoopAgent) -- post-main-pass debt-ratio top-up
  │                                                                [maintainability.py]
  ├── push_step             (BaseAgent)  -- Phase IV
  └── report_step           (BaseAgent)  -- Section 9, always runs (SequentialAgent tail)
                                                                   [report.py]

Split from one 1395-line agents.py into this package along these existing
phase boundaries (already marked by section-header comments in the
original file) once it grew large enough that navigating it got genuinely
harder — every name below is re-exported here so external imports
(`from sonar_autofix_agent.agents import X`) don't need to know which
submodule X actually lives in.
"""

from google.adk.agents import SequentialAgent

from ..tools import deterministic_fixes, git_tools, patch_tools, sonar_tools  # noqa: F401 -- re-exported
from .checkpoint import (  # noqa: F401
    CheckpointGate, RunFullVerifyStep, TriggerAndReconcileScanStep, checkpoint_pipeline,
)
from .fix import (  # noqa: F401
    ApplyAndVerifyStep, FileFixerStep, FixLlmGateStep, PerFileLoopStep,
    _build_fix_llm_agent, _build_fix_summary, _build_per_file_loop, _extract_code_block,
    _hide_text, _java_fqcn, _looks_like_diff, _no_safe_fix_reason, _strip_escalate,
)
from .maintainability import MaintainabilityDebtCheckStep, _scanned_branch, maintainability_expansion_loop
from .outer_loop import FetchPrioritizeStep, OuterExitCheck, outer_loop
from .report import PushStep, ReportStep, _format_duration, _format_summary
from .setup import SetupStep

pipeline_agent = SequentialAgent(
    name="sonar_autofix_pipeline",
    sub_agents=[SetupStep(), outer_loop, maintainability_expansion_loop, PushStep(), ReportStep()],
)

__all__ = [
    "pipeline_agent",
    "SetupStep",
    "FetchPrioritizeStep", "OuterExitCheck", "outer_loop",
    "FileFixerStep", "FixLlmGateStep", "ApplyAndVerifyStep", "PerFileLoopStep",
    "CheckpointGate", "RunFullVerifyStep", "TriggerAndReconcileScanStep", "checkpoint_pipeline",
    "MaintainabilityDebtCheckStep", "maintainability_expansion_loop",
    "PushStep", "ReportStep",
    "get_adapter",
]

# get_adapter is re-exported at the package root too (agents.get_adapter),
# even though no code in this package calls it that way -- fix.py/setup.py/
# checkpoint.py each import it directly from adapters.base for their own
# use, since a monkeypatch on agents.get_adapter would NOT affect their
# calls (each holds its own separate name binding to the same function
# object; patching one doesn't touch the others). This one's just for
# convenience/introspection at the package level.
from ..adapters.base import get_adapter  # noqa: E402, F401
