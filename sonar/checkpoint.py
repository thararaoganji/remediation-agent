"""Sonar-specific half of checkpointing: re-scan the project on Sonar and
reconcile any newly-introduced findings against this checkpoint's own
commit batch. Composed with core.agents.checkpoint.RunFullVerifyStep (the
build-verify+bisect half) into one pipeline, shared by all three Sonar
agents (autofix, coverage, duplicate) via build_checkpoint_gate()."""

import datetime
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from core import state_schema as sk
from core.adapters.base import get_adapter
from core.agents._shared import _msg
from core.agents.checkpoint import CheckpointGate, RunFullVerifyStep  # noqa: F401 -- re-exported
from core.tools import git_tools

from .tools import sonar_tools


class TriggerAndReconcileScanStep(BaseAgent):
    name: str = "trigger_and_reconcile_scan_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        working_dir = s[sk.WORKING_DIR]
        checkpoint_start = datetime.datetime.utcnow().isoformat()
        task_id = sonar_tools.trigger_sonar_analysis(
            working_dir, s[sk.SONAR_PROJECT_KEY], ce_edition=s.get("ce_edition", True),
            language=s[sk.LANGUAGE], sonar_base_url=s["sonar_base_url"], sonar_token=s["sonar_token"],
        )
        sonar_tools.poll_ce_task_status(s["sonar_base_url"], s["sonar_token"], task_id, timeout_s=600)
        new_issues = sonar_tools.get_issues_created_after(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], checkpoint_start, s["sonar_token"], s[sk.BRANCH_NAME]
        )

        reverted_files = []
        if new_issues:
            # Same bisect-revert reasoning as RunFullVerifyStep: only this
            # checkpoint's own batch of commits are candidates to blame.
            # New issues attributable to a batch file get that file
            # reverted; anything else gets flagged without touching code,
            # since there's no commit here that's safe to undo for it.
            batch = s.get("temp:checkpoint_batch", [])
            batch_by_file = {entry["file"]: entry for entry in batch}
            implicated = {i["component_path"] for i in new_issues if i["component_path"] in batch_by_file}

            for file in implicated:
                entry = batch_by_file[file]
                git_tools.revert_commit_for_file(working_dir, entry["commit_sha"], entry["file"])
                reverted_files.append(entry["file"])
                if entry["file"] in s[sk.FILES_COMPLETED]:
                    s[sk.FILES_COMPLETED].remove(entry["file"])
                if entry["file"] not in s[sk.FILES_REVERTED_AT_CHECKPOINT]:
                    s[sk.FILES_REVERTED_AT_CHECKPOINT].append(entry["file"])
                s[sk.ISSUES_FIXED] = [k for k in s[sk.ISSUES_FIXED] if k not in entry["issue_keys"]]
                new_count = sum(1 for i in new_issues if i["component_path"] == file)
                s[sk.FILES_FLAGGED].append({
                    "file": entry["file"],
                    "reason": f"reverted: introduced {new_count} new Sonar issue(s) found by this checkpoint's re-scan",
                })

            for i in new_issues:
                if i["component_path"] not in batch_by_file:
                    s[sk.FILES_FLAGGED].append({
                        "file": i["component_path"],
                        "reason": f"new Sonar issue after checkpoint, not attributable to this batch: "
                                  f"{i.get('rule_key')} — {i.get('message', '')}",
                    })

            if reverted_files:
                adapter = get_adapter(s[sk.LANGUAGE], working_dir)
                result = adapter.verify_build(working_dir)
                if not result.passed:
                    raise RuntimeError(
                        f"Build still failing after reverting {reverted_files} in response to new "
                        f"Sonar issues — errors: {result.errors}"
                    )

        s[sk.CHECKPOINTS].append({
            "timestamp": checkpoint_start,
            "new_issues_found": len(new_issues),
            "reverted_files": reverted_files,
            # Set by RunFullVerifyStep earlier in this same checkpoint
            # pipeline run when a full-build failure triggered a revert --
            # carries the actual compiler/test error into the final report
            # instead of just the list of files that got blamed for it.
            "build_error": s.pop("temp:checkpoint_build_error", None),
        })
        git_tools.commit_checkpoint_marker(working_dir)
        if not new_issues:
            msg = "Re-scanned Sonar — no new issues introduced."
        elif reverted_files:
            msg = f"Re-scan found new issues — reverted `{'`, `'.join(reverted_files)}`, then re-verified clean."
        else:
            msg = f"Re-scan found {len(new_issues)} new issue(s) not attributable to this batch — flagged for review."
        yield Event(author=self.name, content=_msg(msg))


def _build_checkpoint_pipeline() -> SequentialAgent:
    """Factory, not a module-level singleton -- checkpoint_pipeline is
    embedded (via CheckpointGate's `pipeline` field) once per Sonar agent
    (autofix/coverage/duplicate) and, within autofix specifically, once
    per per-file-loop instance (main pass + maintainability expansion
    pass) -- same reasoning as core.agents.fix_loop._build_fix_llm_agent's
    docstring: each embedding gets its own instance rather than sharing
    one across independent loop instances."""
    return SequentialAgent(
        name="sonar_checkpoint_pipeline",
        sub_agents=[RunFullVerifyStep(), TriggerAndReconcileScanStep()],
    )


def build_checkpoint_gate() -> CheckpointGate:
    return CheckpointGate(pipeline=_build_checkpoint_pipeline())
