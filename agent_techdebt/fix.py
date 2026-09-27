"""Per-file loop body, Sonar-issue-specific half: pop a file, generate a
fix, apply/verify it, retry narrowly if needed. The tool-agnostic pieces
this leans on (the LLM-call gate, diff/NO_SAFE_FIX text helpers,
_java_fqcn, the loop-nesting escalate fix-up) live in
core.agents.fix_loop -- see that module's docstring. This is the most
complex module in the package by a wide margin -- the actual fix-generation
and recovery logic (diff attempt -> full-file retry -> narrow single-issue
retry, plus NO_SAFE_FIX detection at each stage) lives here as one cohesive
unit rather than being split further, since its methods are tightly
coupled steps of the same overall recovery flow."""

import difflib
import os
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from core import state_schema as sk
from core.adapters.base import get_adapter
from core.agents._shared import _msg
from core.agents.fix_loop import (
    FixLlmGateStep, _build_fix_llm_agent, _extract_code_block, _hide_text,
    _java_fqcn, _llm_error_message, _looks_like_diff, _no_safe_fix_reason,
)
from core.tools.patch_tools import apply_diff, parse_junit_failures
from core.tools import git_tools

from sonar.checkpoint import build_checkpoint_gate
from sonar.tools import deterministic_fixes, patch_tools
from .prompts import build_fix_prompt


_FIX_SUMMARY_DIFF_CHAR_LIMIT = 1500


def _build_fix_summary(file_path: str, issues: list[dict], before: str, after: str) -> str:
    """The one place a fix's actual content is shown in chat/web --
    "error" (the Sonar issue(s) that triggered this fix) plus "resolution"
    (a compact diff of what changed). Diffed against the TRUE pre-fix
    content (before any deterministic or LLM change), so a file that got
    both still shows one combined diff, not two. Truncated -- a full
    class-level diff dumped into chat for every fix is the "displays too
    much" complaint this exists to fix."""
    lines = [f"- `{i['rule_key']}`: {i['message']}" for i in issues]
    diff_lines = list(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=file_path, tofile=file_path,
    ))
    diff_text = "".join(diff_lines) or "(no textual difference from the original)"
    if len(diff_text) > _FIX_SUMMARY_DIFF_CHAR_LIMIT:
        diff_text = diff_text[:_FIX_SUMMARY_DIFF_CHAR_LIMIT] + "\n… (truncated)"
    return f"Fixed `{file_path}`:\n" + "\n".join(lines) + f"\n```diff\n{diff_text}\n```"


class FileFixerStep(BaseAgent):
    """Pops the next file, classifies clusters (colliding excluded here,
    independent+nested flattened for a single prompt), and writes the
    prompt into temp: state for fix_llm_agent to consume."""
    name: str = "file_fixer_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        queue = s[sk.ORDERED_FILES_REMAINING]
        if not queue:
            s[sk.FILE_LOOP_DONE] = True
            yield Event(
                author=self.name,
                content=_msg("No files left in the queue."),
                actions=EventActions(escalate=True),
            )
            return

        group = queue[0]  # keep at index 0 until fully handled; pop on success
        cluster_result = patch_tools.classify_and_prepare_batch(group["issues"])
        for issue in cluster_result.colliding_flagged:
            s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": "colliding textRange"})

        batch_issues = patch_tools.issues_for_prompt(cluster_result)
        adapter = get_adapter(s[sk.LANGUAGE], s[sk.WORKING_DIR])
        # group["file"] is always forward-slash-separated (Sonar's own
        # component-path convention), regardless of host OS -- split and
        # rejoin with os.path.join rather than raw string interpolation so
        # the actual filesystem call always uses the native separator.
        file_abs_path = os.path.join(s[sk.WORKING_DIR], *group["file"].split("/"))
        with open(file_abs_path, encoding="utf-8") as f:
            original_content = f.read()

        # Deterministic pre-pass: a handful of rules have exactly one
        # unambiguous correct fix -- .collect(toList()) -> .toList(),
        # deleting commented-out code, etc. Applying those with plain text
        # surgery before the LLM ever sees the file cuts cost/latency for
        # that slice and removes any chance of the LLM touching something
        # else in the same pass. Issues a fixer declines (wrong shape) fall
        # straight through to remaining_issues unchanged.
        file_content, mechanical_fixed, remaining_issues = deterministic_fixes.apply_deterministic_fixes(
            original_content, batch_issues,
        )
        if mechanical_fixed:
            with open(file_abs_path, "w", encoding="utf-8") as f:
                f.write(file_content)
            rule_list = ", ".join(sorted({i["rule_key"] for i in mechanical_fixed}))
            yield Event(author=self.name, content=_msg(
                f"Deterministically fixed {len(mechanical_fixed)} issue(s) in `{group['file']}` "
                f"({rule_list}) — no LLM call needed for these."
            ))

        # CURRENT_FILE_GROUP["issues"] stays the FULL batch (mechanical +
        # LLM-bound) -- ApplyAndVerifyStep uses it for ISSUES_FIXED
        # tracking, the commit's issue_keys, and (usefully) re-verifies
        # the deterministic fixes too via the same before/after count
        # check it already runs for LLM fixes.
        s[sk.CURRENT_FILE_GROUP] = {"file": group["file"], "issues": batch_issues}
        # The TRUE pre-patch text, not the post-mechanical-fix content --
        # verify_issue_patterns_resolved()'s before/after comparison and
        # _retry_full_file()'s diff both need the real starting point.
        s[sk.CURRENT_FILE_CONTENT] = original_content

        if not remaining_issues:
            # Every issue in this file was resolved by the deterministic
            # pre-pass -- nothing left for fix_llm_agent to do. The file on
            # disk already IS the fix; ApplyAndVerifyStep's apply_diff
            # call is skipped for this file (see temp:skip_llm_fix) so it
            # runs its normal compile-check/verify path against what's
            # already there instead.
            s["temp:skip_llm_fix"] = True
            s[sk.PROPOSED_DIFF] = ""
            yield Event(author=self.name, content=_msg(
                f"All issue(s) in `{group['file']}` resolved deterministically."
            ))
            return

        s["temp:skip_llm_fix"] = False
        s["temp:fix_prompt"] = build_fix_prompt(
            file_path=group["file"],
            language=s[sk.LANGUAGE],
            file_content=file_content,
            issues_bottom_to_top=remaining_issues,
            language_addendum=adapter.get_fix_prompt_addendum(),
        )
        yield Event(author=self.name, content=_msg(
            f"Fixing `{group['file']}` ({len(remaining_issues)} issue(s)"
            f"{f', {len(mechanical_fixed)} more fixed deterministically' if mechanical_fixed else ''})."
        ))


class ApplyAndVerifyStep(BaseAgent):
    """Apply diff, quick compile check, then the verification (not
    regeneration) step."""
    name: str = "apply_and_verify_step"

    async def _retry_full_file(
        self, ctx: InvocationContext, group: dict, working_dir: str, reason: str,
    ) -> AsyncGenerator[Event, None]:
        """Fallback for when the diff-based fix fails -- either git apply
        rejects it outright, or it applies but the result doesn't compile.
        Both were observed live to share the same root cause: fix_llm_agent
        miscounting unified-diff hunk headers on larger, multi-hunk edits.
        Asking for the WHOLE file instead of a diff sidesteps hunk
        arithmetic entirely.

        Sets state["temp:full_file_retry_ok"] rather than returning a value
        (async generators can't `return` one) -- the caller checks it once
        this generator is fully drained. Uses a fresh, unregistered
        LlmAgent so this can run from inside another step with no
        sub_agents wiring."""
        s = ctx.session.state
        s["temp:full_file_retry_ok"] = False
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        s["temp:fix_prompt"] = build_fix_prompt(
            file_path=group["file"],
            language=s[sk.LANGUAGE],
            file_content=s[sk.CURRENT_FILE_CONTENT],
            issues_bottom_to_top=group["issues"],
            language_addendum=adapter.get_fix_prompt_addendum(),
            output_format=(
                f"The previous diff-based attempt failed because {reason}. "
                "This time, output the COMPLETE corrected file — every line "
                "from start to end, with the fixes applied — not a diff. "
                "Wrap it in a single fenced code block and nothing else: no "
                "explanation, no per-issue breakdown, just the fenced block "
                "containing the full file."
            ),
        )
        retry_agent = _build_fix_llm_agent()
        # Hide the model's own text -- that's the entire regenerated file,
        # and showing it verbatim dumps the whole class into the visible
        # chat/web log on every retry. Still yields the event via
        # _hide_text (content stripped, actions/state_delta intact), not
        # dropped outright -- dropping it drops the output_key write
        # PROPOSED_DIFF depends on below, same as FixLlmGateStep.
        llm_call_error = None
        try:
            async for event in retry_agent.run_async(ctx):
                llm_call_error = _llm_error_message(event) or llm_call_error
                yield _hide_text(event)
        except Exception as e:
            # See FixLlmGateStep's identical guard for why this is caught
            # here rather than left to crash the whole run.
            llm_call_error = f"{type(e).__name__}: {e}"

        if llm_call_error is not None:
            # Reuses temp:no_safe_fix_reason -- the caller (ApplyAndVerifyStep,
            # in the "diff failed to apply" branch) already prefers it over
            # its own generic fallback reason when flagging the file.
            s["temp:no_safe_fix_reason"] = f"model call failed: {llm_call_error}"
            yield Event(author=self.name, content=_msg(
                f"Full-file retry for `{group['file']}` failed: the model call failed ({llm_call_error})."
            ))
            return

        raw = s.get(sk.PROPOSED_DIFF, "")
        no_safe_fix_reason = _no_safe_fix_reason(raw)
        if no_safe_fix_reason is not None:
            s[sk.ISSUES_NO_SAFE_FIX].extend(i["issue_key"] for i in group["issues"])
            s["temp:no_safe_fix_reason"] = no_safe_fix_reason
            yield Event(author=self.name, content=_msg(
                f"Full-file retry for `{group['file']}` declined: {no_safe_fix_reason}"
            ))
            return
        content = _extract_code_block(raw).strip()
        if not content:
            return
        if _looks_like_diff(content):
            yield Event(author=self.name, content=_msg(
                f"Full-file retry for `{group['file']}` still returned diff-shaped output "
                "instead of a complete file — declining rather than writing it, flagged for manual review."
            ))
            return
        with open(os.path.join(working_dir, group["file"]), "w", encoding="utf-8") as f:
            f.write(content)
        s["temp:full_file_retry_ok"] = True
        # No diff shown here -- ApplyAndVerifyStep's own summary (issue
        # list + compact diff) covers this once the fix is confirmed to
        # actually compile/verify, instead of showing it twice.
        yield Event(author=self.name, content=_msg(
            f"Full-file fix for `{group['file']}` generated — verifying."
        ))

    async def _retry_unresolved_issues(
        self, ctx: InvocationContext, group: dict, working_dir: str, unresolved_keys: list[str],
    ) -> AsyncGenerator[Event, None]:
        """One narrow follow-up call, scoped to just the issue(s)
        verify_issue_patterns_resolved found still unresolved after the
        main fix -- naming precisely which issue(s) weren't resolved and
        where is a meaningfully different, easier task than the original
        "fix N issues in this file" batch that produced the mistake.

        Applied against the file's CURRENT on-disk content (already
        reflects whatever DID succeed from the main attempt) as the
        retry's starting point, not the original pre-fix content --
        re-doing already-correct changes isn't the goal. Only reverts
        back to that pre-retry content if the retry's compile check
        fails; a retry that compiles but only resolves SOME of the
        unresolved issues still keeps that partial progress rather than
        discarding it.

        Sets state["temp:retry_unresolved_ok"] to {issue_key: resolved}
        for unresolved_keys (async generators can't `return` a value) --
        the caller checks it once this generator is fully drained."""
        s = ctx.session.state
        with open(os.path.join(working_dir, group["file"]), encoding="utf-8") as f:
            pre_retry_content = f.read()
        s["temp:retry_unresolved_ok"] = {k: False for k in unresolved_keys}

        retry_issues = [i for i in group["issues"] if i["issue_key"] in unresolved_keys]
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        s["temp:fix_prompt"] = build_fix_prompt(
            file_path=group["file"],
            language=s[sk.LANGUAGE],
            file_content=pre_retry_content,
            issues_bottom_to_top=retry_issues,
            language_addendum=adapter.get_fix_prompt_addendum(),
            output_format=(
                "A PREVIOUS attempt at this file already ran and did NOT actually "
                "resolve the issue(s) below — each one's flagged code is still present, "
                "completely unchanged, at the exact line(s) given. Look carefully at "
                "those specific line numbers before editing anything; do not assume "
                "the previous attempt's guess at the right location was correct. This "
                "is a focused retry for ONLY these issue(s), against the file's CURRENT "
                "content (which already reflects any other, already-successful fixes). "
                "Output a unified diff, same as before."
            ),
        )
        retry_agent = _build_fix_llm_agent()
        llm_call_error = None
        try:
            async for event in retry_agent.run_async(ctx):
                llm_call_error = _llm_error_message(event) or llm_call_error
                yield _hide_text(event)
        except Exception as e:
            # See FixLlmGateStep's identical guard for why this is caught
            # here rather than left to crash the whole run.
            llm_call_error = f"{type(e).__name__}: {e}"

        if llm_call_error is not None:
            # Reuses temp:no_safe_fix_reason -- the caller already prefers it
            # over its own generic "unresolved after patch" fallback reason.
            s["temp:no_safe_fix_reason"] = f"model call failed: {llm_call_error}"
            yield Event(author=self.name, content=_msg(
                f"Narrow retry for `{group['file']}` failed: the model call failed ({llm_call_error})."
            ))
            return

        raw = s.get(sk.PROPOSED_DIFF, "")
        no_safe_fix_reason = _no_safe_fix_reason(raw)
        if no_safe_fix_reason is not None:
            s["temp:no_safe_fix_reason"] = no_safe_fix_reason
            yield Event(author=self.name, content=_msg(
                f"Narrow retry for `{group['file']}` declined: {no_safe_fix_reason}"
            ))
            return

        applied = apply_diff(raw, working_dir, group["file"])
        if not applied:
            content = _extract_code_block(raw).strip()
            no_safe_fix_reason = _no_safe_fix_reason(content)
            if no_safe_fix_reason is not None:
                s["temp:no_safe_fix_reason"] = no_safe_fix_reason
                yield Event(author=self.name, content=_msg(
                    f"Narrow retry for `{group['file']}` declined: {no_safe_fix_reason}"
                ))
                return
            if not content or _looks_like_diff(content):
                yield Event(author=self.name, content=_msg(
                    f"Narrow retry for `{group['file']}` failed to apply — still flagged for manual review."
                ))
                return
            with open(os.path.join(working_dir, group["file"]), "w", encoding="utf-8") as f:
                f.write(content)

        result = adapter.quick_compile_check(working_dir, scope=group["file"])
        if not result.passed:
            with open(os.path.join(working_dir, group["file"]), "w", encoding="utf-8") as f:
                f.write(pre_retry_content)
            yield Event(author=self.name, content=_msg(
                f"Narrow retry for `{group['file']}` failed to compile — reverted, still flagged for manual review."
            ))
            return

        s["temp:retry_unresolved_ok"] = patch_tools.verify_issue_patterns_resolved(
            group["file"], retry_issues, working_dir, original_content=pre_retry_content,
        )
        resolved_count = sum(1 for ok in s["temp:retry_unresolved_ok"].values() if ok)
        yield Event(author=self.name, content=_msg(
            f"Narrow retry for `{group['file']}` resolved {resolved_count}/{len(unresolved_keys)} "
            "previously-unresolved issue(s)."
        ))

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        group = s[sk.CURRENT_FILE_GROUP]
        working_dir = s[sk.WORKING_DIR]
        adapter = get_adapter(s[sk.LANGUAGE], s[sk.WORKING_DIR])

        # Checked before anything else even looks at PROPOSED_DIFF -- a
        # blocked model turn (RECITATION, SAFETY, ...) never writes it, so
        # reading it here would either KeyError (first file of the run) or
        # silently reuse a stale diff from a completely unrelated earlier
        # file (any later file) -- see _llm_error_message's docstring.
        llm_call_error = s.pop("temp:llm_call_error", None)
        if llm_call_error is not None:
            s[sk.FILES_FLAGGED].append({
                "file": group["file"],
                "reason": f"model call failed ({llm_call_error}) — no fix was generated",
            })
            s[sk.ORDERED_FILES_REMAINING].pop(0)
            yield Event(author=self.name, content=_msg(
                f"Fix for `{group['file']}` skipped: the model call failed ({llm_call_error}) — "
                "flagged for manual review."
            ))
            return

        # Check for a NO_SAFE_FIX refusal before ever touching apply_diff --
        # plain refusal prose isn't valid diff syntax, so apply_diff would
        # reject it safely, but only after a confusing "failed to apply"
        # message and a wasted full-file retry call that just refuses again
        # for the same reason. Catching it here skips straight to an
        # honest, immediate "declined" outcome instead.
        if not s.get("temp:skip_llm_fix"):
            no_safe_fix_reason = _no_safe_fix_reason(s[sk.PROPOSED_DIFF])
            if no_safe_fix_reason is not None:
                s[sk.ISSUES_NO_SAFE_FIX].extend(i["issue_key"] for i in group["issues"])
                s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": no_safe_fix_reason})
                s[sk.ORDERED_FILES_REMAINING].pop(0)
                yield Event(author=self.name, content=_msg(
                    f"Fix for `{group['file']}` declined: {no_safe_fix_reason} — flagged for manual review."
                ))
                return

        # A deterministic-only fix (FileFixerStep found no remaining
        # issues for the LLM) already wrote the patched content straight
        # to disk -- there's no diff to apply, so treat this file as
        # already "applied" and fall through to the same compile-check/
        # verify path every other fix goes through.
        applied = True if s.get("temp:skip_llm_fix") else apply_diff(
            s[sk.PROPOSED_DIFF], working_dir, group["file"],
        )
        retried = False
        if not applied:
            yield Event(author=self.name, content=_msg(
                f"Diff for `{group['file']}` failed to apply — retrying with a full-file fix."
            ))
            async for event in self._retry_full_file(ctx, group, working_dir, "the diff failed to apply"):
                yield event
            retried = True
            applied = s.get("temp:full_file_retry_ok", False)
            if not applied:
                s[sk.FILES_FLAGGED].append({
                    "file": group["file"],
                    "reason": s.pop("temp:no_safe_fix_reason", None)
                    or "diff failed to apply (full-file retry also failed)",
                })
                s[sk.ORDERED_FILES_REMAINING].pop(0)
                yield Event(author=self.name, content=_msg(
                    f"Could not apply the fix to `{group['file']}` even after a full-file retry — "
                    "flagged for manual review."
                ))
                return

        result = adapter.quick_compile_check(working_dir, scope=group["file"])
        if not result.passed and not retried:
            # First failure for this file, and it came from a diff that DID
            # apply -- same root cause as the apply-fail branch above (a
            # miscounted hunk can merge into subtly wrong code that still
            # "applies" cleanly), so the same fallback applies here too.
            # Only one retry per file either way (the `retried` guard).
            git_tools.revert_file(working_dir, group["file"])
            yield Event(author=self.name, content=_msg(
                f"Fix for `{group['file']}` applied but failed to compile — retrying with a full-file fix."
            ))
            async for event in self._retry_full_file(ctx, group, working_dir, "the applied fix failed to compile"):
                yield event
            retried = True
            if s.get("temp:full_file_retry_ok", False):
                result = adapter.quick_compile_check(working_dir, scope=group["file"])

        if not result.passed:
            git_tools.revert_file(working_dir, group["file"])
            no_safe_fix_reason = s.pop("temp:no_safe_fix_reason", None)
            s[sk.FILES_FLAGGED].append({
                "file": group["file"], "reason": no_safe_fix_reason or result.errors,
            })
            s[sk.ORDERED_FILES_REMAINING].pop(0)
            if no_safe_fix_reason is not None:
                yield Event(author=self.name, content=_msg(
                    f"Fix for `{group['file']}` declined: {no_safe_fix_reason} — flagged for manual review."
                ))
                return
            yield Event(author=self.name, content=_msg(
                f"Fix for `{group['file']}`{' still' if retried else ''} failed to compile — "
                "reverted, flagged for manual review."
            ))
            return

        # Re-enabling an S2187-flagged test means it's about to run for the
        # very first time -- quick_compile_check above only proved it
        # compiles, not that it passes. Verify it here, in isolation, on
        # just this file, rather than letting a broken re-enabled test ride
        # into a shared checkpoint batch where the full build's failure
        # would drag every other file in that batch into a collateral
        # bisect-revert.
        if any(i["rule_key"] == "java:S2187" for i in group["issues"]):
            test_result = adapter.run_specific_tests(working_dir, [_java_fqcn(group["file"])])
            if not test_result.passed:
                git_tools.revert_file(working_dir, group["file"])
                failing = parse_junit_failures(working_dir)
                detail = f" (failing test(s): {'; '.join(failing)})" if failing else ""
                s[sk.FILES_FLAGGED].append({
                    "file": group["file"],
                    "reason": f"re-enabled test still fails{detail}",
                })
                s[sk.ORDERED_FILES_REMAINING].pop(0)
                yield Event(author=self.name, content=_msg(
                    f"Fix for `{group['file']}` compiled, but the re-enabled test still fails{detail} — "
                    "reverted, flagged for manual review."
                ))
                return

        # This attempt is about to succeed -- clear any stale flag left by an
        # earlier outer_loop iteration's failed attempt on this same file
        # (e.g. a checkpoint revert), so the final report doesn't keep
        # showing it as needing manual review once it's actually fixed.
        s[sk.FILES_FLAGGED] = [f for f in s[sk.FILES_FLAGGED] if f["file"] != group["file"]]

        verification = patch_tools.verify_issue_patterns_resolved(
            group["file"], group["issues"], working_dir,
            original_content=s[sk.CURRENT_FILE_CONTENT],
        )
        unresolved = [k for k, ok in verification.items() if not ok]
        if unresolved:
            async for event in self._retry_unresolved_issues(ctx, group, working_dir, unresolved):
                yield event
            retry_result = s.get("temp:retry_unresolved_ok", {})
            unresolved = [k for k in unresolved if not retry_result.get(k, False)]
            no_safe_fix_reason = s.pop("temp:no_safe_fix_reason", None)
            if unresolved:
                s[sk.FILES_FLAGGED].append({
                    "file": group["file"],
                    "reason": no_safe_fix_reason or f"unresolved after patch: {unresolved}",
                })

        commit_sha = git_tools.commit(working_dir, f"fix: sonar issues in {group['file']}")
        s[sk.FILES_COMPLETED].append(group["file"])
        s[sk.ISSUES_FIXED].extend([i["issue_key"] for i in group["issues"]])
        if commit_sha is not None:
            # None means this was a no-op -- the regenerated fix was already
            # byte-identical to what's on disk. Nothing new exists for a
            # later checkpoint to revert in that case, so it's deliberately
            # left out of this checkpoint's revertible batch.
            s.setdefault("temp:checkpoint_batch", []).append({
                "file": group["file"],
                "commit_sha": commit_sha,
                "issue_keys": [i["issue_key"] for i in group["issues"]],
            })
        s[sk.ORDERED_FILES_REMAINING].pop(0)
        s[sk.FILES_SINCE_CHECKPOINT] += 1
        note = f" ({len(unresolved)} issue(s) still unresolved, also flagged)" if unresolved else ""
        with open(os.path.join(working_dir, group["file"]), encoding="utf-8") as f:
            after_content = f.read()
        summary = _build_fix_summary(group["file"], group["issues"], s[sk.CURRENT_FILE_CONTENT], after_content)
        yield Event(author=self.name, content=_msg(f"{summary}{note}"))


def _build_per_file_loop() -> LoopAgent:
    """Factory, not a module-level singleton -- see
    core.agents.fix_loop._build_fix_llm_agent's docstring."""
    return LoopAgent(
        name="per_file_loop",
        sub_agents=[
            FileFixerStep(),
            FixLlmGateStep(llm_agent=_build_fix_llm_agent()),
            ApplyAndVerifyStep(),
            build_checkpoint_gate(),
        ],
        max_iterations=1000,  # real exit is FileFixerStep's escalate=True on empty queue
    )
