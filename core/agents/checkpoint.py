"""Tool-agnostic half of checkpointing: run the full build/test suite every
CHECKPOINT_BATCH_SIZE files (or when the queue empties), bisect-reverting
anything that broke the build.

The other half -- re-scanning the finding source itself for newly-introduced
findings -- is inherently tool-specific (what "re-scan" even means differs
completely between Sonar, Veracode, and Black Duck), so it isn't here.
CheckpointGate takes whatever pipeline to actually fire as a field instead
of hardcoding one, the same "pass the sub-component as a typed field"
pattern core.agents.fix_loop.PerFileLoopStep (loop=...) and
FixLlmGateStep (llm_agent=...) already use -- a tool package composes
RunFullVerifyStep with its own re-scan step into a SequentialAgent and
passes that in as `pipeline`."""

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from .. import state_schema as sk
from ..adapters.base import get_adapter
from ..tools import git_tools, patch_tools
from ._shared import _msg


class CheckpointGate(BaseAgent):
    """Fires `pipeline` when the batch-size boundary is hit. ADK's
    LoopAgent has no native 'every N iterations' primitive, so this
    conditional dispatch is implemented directly rather than forced into a
    workflow-agent shape."""
    name: str = "checkpoint_gate"
    pipeline: BaseAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        if s[sk.FILES_SINCE_CHECKPOINT] >= s[sk.CHECKPOINT_BATCH_SIZE] or not s[sk.ORDERED_FILES_REMAINING]:
            async for event in self.pipeline.run_async(ctx):
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
        restored = []

        if not result.passed:
            # Captured before any reverts happen -- this is the actual
            # compiler/test output that triggered the bisect below, and the
            # only evidence of WHY without re-deriving it after the fact.
            original_error = result.errors
            # gradle -q (quiet console) never prints individual failing test
            # names/stack traces -- only the summary count seen in
            # original_error -- so this is the only way to name which
            # test(s) actually broke, from the JUnit XML report the Test
            # task still writes regardless of console verbosity.
            failing_tests = patch_tools.parse_junit_failures(working_dir)
            # Not true binary-search bisection -- each candidate needs a
            # full project build and there's only one working tree to test
            # against, so a linear reverse-commit-order sweep (most recent
            # first, most likely culprit) is the practical tradeoff. Stops
            # at the first revert that restores a passing build.
            #
            # Deliberately NOT true binary search, even though that would
            # be fewer full builds in the common case: a real run needed
            # ALL commits in a checkpoint batch reverted before the build
            # passed (an interconnected change broke multiple files'
            # tests simultaneously) -- binary search assumes a single
            # culprit and can get that case wrong (leaving a bad commit in
            # place), which is worse than being slow. This linear scan is
            # slower but correct for any number of simultaneous culprits.
            for entry in reversed(batch):
                git_tools.revert_commit_for_file(working_dir, entry["commit_sha"], entry["file"])
                reverted.append(entry)
                result = adapter.verify_build(working_dir)
                if result.passed:
                    break

            # Re-apply-and-verify: the sweep above stops at the FIRST
            # revert that restores a passing build, which means every file
            # reverted before reaching the true culprit is collateral
            # damage, not evidence of guilt -- the culprit just happened to
            # be older in the batch than they are. Tested one at a time
            # against the fully-reverted baseline (not cumulatively) so
            # this also does the right thing when MULTIPLE files are
            # simultaneously guilty -- each guilty file still reproduces
            # the failure on its own regardless of the others' state, so
            # each independently fails this check and correctly stays
            # reverted.
            if reverted:
                for entry in list(reverted):
                    git_tools.restore_file_from_commit(working_dir, entry["commit_sha"], entry["file"])
                    retest = adapter.verify_build(working_dir)
                    if retest.passed:
                        reverted.remove(entry)
                        restored.append(entry)
                    else:
                        git_tools.revert_file(working_dir, entry["file"])

                if restored:
                    # Each restored file only proved innocent in isolation
                    # (one re-applied at a time, all others still
                    # reverted) -- one more full build with all of them
                    # restored together rules out an interaction bug
                    # between two "individually innocent" files before
                    # anything gets committed.
                    final_check = adapter.verify_build(working_dir)
                    if final_check.passed:
                        for entry in restored:
                            git_tools.commit(
                                working_dir,
                                f"fix: sonar issues in {entry['file']} "
                                "(restored -- unrelated to checkpoint failure)",
                            )
                        result = final_check
                    else:
                        # Rare: safe fallback to the already-known-good
                        # all-reverted state from before this pass -- none
                        # of them actually stuck, so they're not "restored"
                        # for reporting purposes either.
                        for entry in restored:
                            git_tools.revert_file(working_dir, entry["file"])
                            reverted.append(entry)
                        restored = []
                        result = adapter.verify_build(working_dir)
                else:
                    result = adapter.verify_build(working_dir)

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
                # build is still broken -- the regression predates this run
                # (or lives outside these files entirely). Not something an
                # agent should silently paper over: stop and surface it.
                raise RuntimeError(
                    f"Full build still failing after reverting all {len(batch)} file(s) "
                    f"committed since the last checkpoint ({[e['file'] for e in batch]}). "
                    "Likely a pre-existing failure, not caused by this run's fixes -- "
                    f"build errors: {result.errors}"
                )

        s["temp:checkpoint_batch"] = []
        restored_note = (
            f" `{'`, `'.join(e['file'] for e in restored)}` reproduced no failure alone "
            "and were restored." if restored else ""
        )
        if batch and not reverted and not restored:
            msg = f"Checkpoint: full build passed ({len(batch)} file(s) since last checkpoint)."
        elif reverted or restored:
            s["temp:checkpoint_build_error"] = {"error": original_error, "failing_tests": failing_tests}
            test_detail = f"\nFailing test(s): {'; '.join(failing_tests)}" if failing_tests else ""
            revert_note = (
                f"reverted `{'`, `'.join(e['file'] for e in reverted)}`, then passed."
                if reverted else "reverting each file since the last checkpoint in turn found a passing build."
            )
            msg = (
                f"Checkpoint: build failed -- {revert_note}{restored_note}"
                f"{test_detail}\n"
                f"Build error that triggered the revert:\n```\n{original_error[-1500:]}\n```"
            )
        else:
            msg = "Checkpoint: full build passed."
        yield Event(author=self.name, content=_msg(msg))
