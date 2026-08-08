"""Checkpoint pipeline (Section 5.4): fires every CHECKPOINT_BATCH_SIZE
files (or when the queue empties) to run the full build/test suite and
re-scan Sonar, bisect-reverting anything that broke either."""

import datetime
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from .. import state_schema as sk
from ..adapters.base import get_adapter
from ..tools import git_tools, patch_tools, sonar_tools
from ._shared import _msg


class CheckpointGate(BaseAgent):
    """Fires the checkpoint SequentialAgent when the batch-size boundary is
    hit (Section 5.3's checkpoint_boundary_reached()). ADK's LoopAgent has
    no native 'every N iterations' primitive, so this conditional dispatch
    is implemented directly rather than forced into a workflow-agent shape."""
    name: str = "checkpoint_gate"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        if s[sk.FILES_SINCE_CHECKPOINT] >= s[sk.CHECKPOINT_BATCH_SIZE] or not s[sk.ORDERED_FILES_REMAINING]:
            async for event in checkpoint_pipeline.run_async(ctx):
                yield event
            s[sk.FILES_SINCE_CHECKPOINT] = 0
        else:
            yield Event(author=self.name, content=_msg(
                f"{s[sk.FILES_SINCE_CHECKPOINT]}/{s[sk.CHECKPOINT_BATCH_SIZE]} file(s) since last checkpoint."
            ))


class RunFullVerifyStep(BaseAgent):
    name: str = "run_full_verify_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        working_dir = s[sk.WORKING_DIR]
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        batch = s.get("temp:checkpoint_batch", [])
        result = adapter.verify_build(working_dir)
        reverted = []

        if not result.passed:
            # Captured before any reverts happen — this is the actual
            # compiler/test output that triggered the bisect below, and the
            # only evidence of WHY without re-deriving it after the fact
            # (as happened diagnosing a real run: the revert-list alone
            # doesn't say what broke, only what got blamed for it).
            original_error = result.errors
            # gradle -q (quiet console) never prints individual failing test
            # names/stack traces — only the summary count seen in
            # original_error — so this is the only way to name which
            # test(s) actually broke, from the JUnit XML report the Test
            # task still writes regardless of console verbosity.
            failing_tests = patch_tools.parse_junit_failures(working_dir)
            # bisect_within_checkpoint_files(): quick_compile_check() in
            # ApplyAndVerifyStep only checks the single file being patched
            # in isolation, so a full-project build regression here means
            # one of THIS checkpoint's commits broke something elsewhere
            # (e.g. a caller of a changed method signature). Not true
            # binary-search bisection — each candidate needs a full project
            # build and there's only one working tree to test against, so a
            # linear reverse-commit-order sweep (most recent first, most
            # likely culprit) is the practical tradeoff. Stops at the first
            # revert that restores a passing build.
            #
            # Deliberately NOT true binary search, even though that would
            # be fewer full builds in the common case: a real run
            # (spring-petclinic) needed ALL commits in a checkpoint batch
            # reverted before the build passed (an interconnected entity/
            # DTO conversion broke multiple files' tests simultaneously) --
            # binary search assumes a single culprit and can get that case
            # wrong (leaving a bad commit in place), which is worse than
            # being slow. This linear scan is slower but correct for any
            # number of simultaneous culprits.
            for entry in reversed(batch):
                git_tools.revert_commit_for_file(working_dir, entry["commit_sha"], entry["file"])
                reverted.append(entry)
                result = adapter.verify_build(working_dir)
                if result.passed:
                    break

            reverted_issue_keys = {k for entry in reverted for k in entry["issue_keys"]}
            revert_reason = "reverted: broke the full build at checkpoint"
            if failing_tests:
                revert_reason += f" (failing test(s): {'; '.join(failing_tests)})"
            for entry in reverted:
                if entry["file"] in s[sk.FILES_COMPLETED]:
                    s[sk.FILES_COMPLETED].remove(entry["file"])
                if entry["file"] not in s[sk.FILES_REVERTED_AT_CHECKPOINT]:
                    s[sk.FILES_REVERTED_AT_CHECKPOINT].append(entry["file"])
                s[sk.FILES_FLAGGED].append({
                    "file": entry["file"],
                    "reason": revert_reason,
                })
            s[sk.ISSUES_FIXED] = [k for k in s[sk.ISSUES_FIXED] if k not in reverted_issue_keys]

            if not result.passed:
                # Reverted every commit in this checkpoint's batch and the
                # build is still broken — the regression predates this run
                # (or lives outside these files entirely). Not something an
                # agent should silently paper over: stop and surface it.
                raise RuntimeError(
                    f"Full build still failing after reverting all {len(batch)} file(s) "
                    f"committed since the last checkpoint ({[e['file'] for e in batch]}). "
                    "Likely a pre-existing failure, not caused by this run's fixes — "
                    f"build errors: {result.errors}"
                )

        s["temp:checkpoint_batch"] = []
        if batch and not reverted:
            msg = f"Checkpoint: full build passed ({len(batch)} file(s) since last checkpoint)."
        elif reverted:
            s["temp:checkpoint_build_error"] = {"error": original_error, "failing_tests": failing_tests}
            test_detail = f"\nFailing test(s): {'; '.join(failing_tests)}" if failing_tests else ""
            msg = (
                f"Checkpoint: build failed — reverted `{'`, `'.join(e['file'] for e in reverted)}`, then passed."
                f"{test_detail}\n"
                f"Build error that triggered the revert:\n```\n{original_error[-1500:]}\n```"
            )
        else:
            msg = "Checkpoint: full build passed."
        yield Event(author=self.name, content=_msg(msg))


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
            # Set by RunFullVerifyStep earlier in this same checkpoint_pipeline
            # run when a full-build failure triggered a revert — carries the
            # actual compiler/test error into the final report instead of
            # just the list of files that got blamed for it.
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


checkpoint_pipeline = SequentialAgent(
    name="checkpoint_pipeline",
    sub_agents=[RunFullVerifyStep(), TriggerAndReconcileScanStep()],
)
