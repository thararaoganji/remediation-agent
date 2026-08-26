"""Per-file loop body (Section 5.1-5.3): pop a file, generate a fix,
apply/verify it, retry narrowly if needed. This is the most complex
module in the package by a wide margin -- the actual fix-generation and
recovery logic (diff attempt -> full-file retry -> narrow single-issue
retry, plus NO_SAFE_FIX detection at each stage) lives here as one
cohesive unit rather than being split further, since its methods are
tightly coupled steps of the same overall recovery flow."""

import difflib
import os
import re
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from .. import state_schema as sk
from ..adapters.base import get_adapter
from ..prompts import build_fix_prompt
from ..tools import deterministic_fixes, git_tools, patch_tools
from ._shared import _msg
from .checkpoint import CheckpointGate


def _hide_text(event: Event) -> Event:
    """Strips a text-bearing event's visible content while preserving its
    `actions` (notably `state_delta`, which is how output_key writes
    reach session.state — see LlmAgent.__maybe_save_output_to_state).
    ADK only applies an event's state_delta when that event reaches the
    top-level Runner via session_service.append_event(); a step that
    drops the event entirely (rather than yielding a version of it) also
    drops that write. Confirmed live: dropping fix_llm_agent's own
    response event this way left session.state[PROPOSED_DIFF] unset
    entirely (KeyError downstream) -- and _retry_full_file's identical,
    pre-existing pattern had the same bug the whole session, silently
    reading PROPOSED_DIFF's STALE value from the original failed diff
    attempt instead of the actual full-file regeneration, which explains
    the "corrupted diff-shaped full file" symptom diagnosed earlier as a
    model-quality issue -- it was actually just old data.
    Non-text events (function calls/responses) pass through unchanged;
    only cosmetic content is touched, `.model_copy` leaves `actions` (and
    everything else) as the same object."""
    if not (event.content and any(getattr(p, "text", None) for p in event.content.parts or [])):
        return event
    return event.model_copy(update={"content": None})


_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_DIFF_ARTIFACT_RE = re.compile(r"^diff --git |^@@ .*@@", re.MULTILINE)


def _looks_like_diff(text: str) -> bool:
    """Observed live: when a full-file retry is asked to regenerate the
    whole file (see _retry_full_file), the model can still degrade back
    into diff-shaped output partway through — the extracted 'full file'
    ends with a literal `diff --git a/...` / `@@ ... @@` block instead of
    real source, which then fails to compile with confusing javac errors
    ("class, interface, enum, or record expected") pointing at that
    embedded diff syntax. Catching this before writing to disk turns a
    wasted compile-check cycle into an immediate, clearly-explained
    decline."""
    return bool(_DIFF_ARTIFACT_RE.search(text))


def _llm_error_message(event: Event) -> str | None:
    """Event extends google-adk's LlmResponse, so error_code/error_message
    are set directly on it whenever the model's turn ends with a
    finish_reason other than STOP (RECITATION, SAFETY, ...) — ADK's own
    output_key mechanism (LlmAgent.__maybe_save_output_to_state) only
    writes state_delta[output_key] when event.content has actual text
    parts, which a blocked turn never has. Confirmed live: WebGoat -- a
    heavily-forked, extremely well-known OWASP training repo -- triggered
    Gemini's RECITATION filter on a fix attempt, PROPOSED_DIFF was never
    written for that turn, and the very next line that unconditionally
    read s[sk.PROPOSED_DIFF] KeyError'd (or, on a later file in the same
    run, silently reused a stale diff from an unrelated earlier file --
    since the key already existed by then, just with old data). Every
    site that reads PROPOSED_DIFF right after an LLM call must check this
    first."""
    if event.error_code is None:
        return None
    return f"{event.error_code}: {event.error_message or 'no further detail from the model API'}"


_NO_SAFE_FIX_RE = re.compile(r"^\s*NO_SAFE_FIX\s*:?\s*(.*)$", re.MULTILINE)


def _no_safe_fix_reason(text: str) -> str | None:
    """The fix prompt (rule 5) explicitly tells the model to respond with
    'NO_SAFE_FIX: <one-line reason>' instead of guessing when an issue
    can't be safely fixed with the context it has -- state_schema.py even
    has ISSUES_NO_SAFE_FIX reserved for tracking this. But nothing ever
    actually detected the marker: the refusal text fell through the
    normal diff/full-file path unrecognized. Confirmed live: a diff
    attempt correctly failed to apply (plain prose isn't a valid diff,
    so patch_tools.apply_diff safely rejected it), but the full-file
    retry's response -- ALSO just 'NO_SAFE_FIX: ...' prose, since the
    model was refusing the same issue for the same reason -- doesn't
    match _looks_like_diff() either, so it fell through as if it WERE
    the complete regenerated file and got written to disk verbatim,
    corrupting VisitController.java into invalid Java ('class, interface,
    enum, or record expected') and failing the build on every subsequent
    checkpoint. Returns the reason text if found, else None -- callers
    must check this before treating a response as diff or full-file
    content, not after."""
    m = _NO_SAFE_FIX_RE.search(text)
    if not m:
        return None
    return m.group(1).strip() or "no reason given"


def _extract_code_block(text: str) -> str:
    """fix_llm_agent's responses are consistently prose explanation followed
    by one fenced code block — every response observed live follows this
    shape, even when the prompt explicitly asks for raw output only. Used by
    ApplyAndVerifyStep's full-file retry to pull just the file content back
    out. Falls back to the raw text if no fence is found, in case the model
    does comply literally."""
    m = _CODE_FENCE_RE.search(text)
    return m.group(1) if m else text


_FIX_SUMMARY_DIFF_CHAR_LIMIT = 1500


def _build_fix_summary(file_path: str, issues: list[dict], before: str, after: str) -> str:
    """The one place a fix's actual content is shown in chat/web —
    "error" (the Sonar issue(s) that triggered this fix) plus "resolution"
    (a compact diff of what changed), replacing what used to be shown
    piecemeal: fix_llm_agent's own verbose prose+diff response, a second
    diff dump from a full-file retry, and no diff at all for deterministic
    fixes. Diffed against the TRUE pre-fix content (before any
    deterministic or LLM change), so a file that got both still shows one
    combined diff, not two. Truncated — a full class-level diff dumped
    into chat for every fix is the "displays too much" complaint this
    exists to fix."""
    lines = [f"- `{i['rule_key']}`: {i['message']}" for i in issues]
    diff_lines = list(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=file_path, tofile=file_path,
    ))
    diff_text = "".join(diff_lines) or "(no textual difference from the original)"
    if len(diff_text) > _FIX_SUMMARY_DIFF_CHAR_LIMIT:
        diff_text = diff_text[:_FIX_SUMMARY_DIFF_CHAR_LIMIT] + "\n… (truncated)"
    return f"Fixed `{file_path}`:\n" + "\n".join(lines) + f"\n```diff\n{diff_text}\n```"


def _java_fqcn(file_path: str) -> str:
    """Converts a Java source path (as reported by Sonar, relative to the
    repo root) to its fully-qualified class name — e.g.
    'src/test/java/portal/expenses/controller/AuthControllerTest.java' ->
    'portal.expenses.controller.AuthControllerTest'. Assumes the standard
    Maven/Gradle layout (a 'java/' segment marking the source root)."""
    parts = file_path.replace("\\", "/").split("/")
    if "java" in parts:
        parts = parts[parts.index("java") + 1:]
    joined = "/".join(parts)
    if joined.endswith(".java"):
        joined = joined[: -len(".java")]
    return joined.replace("/", ".")


class FileFixerStep(BaseAgent):
    """Pops the next file, classifies clusters (5.2/6.1 resolution: colliding
    excluded here, independent+nested flattened for a single prompt), and
    writes the prompt into temp: state for fix_llm_agent to consume."""
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
        # component-path convention), regardless of host OS — split and
        # rejoin with os.path.join rather than raw string interpolation so
        # the actual filesystem call always uses the native separator.
        file_abs_path = os.path.join(s[sk.WORKING_DIR], *group["file"].split("/"))
        with open(file_abs_path, encoding="utf-8") as f:
            original_content = f.read()

        # Deterministic pre-pass: a handful of rules (see
        # tools/deterministic_fixes.py) have exactly one unambiguous
        # correct fix — .collect(toList()) -> .toList(), deleting
        # commented-out code, etc. Applying those with plain text surgery
        # before the LLM ever sees the file cuts cost/latency for that
        # slice and removes any chance of the LLM touching something else
        # in the same pass. Issues a fixer declines (wrong shape) fall
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
        # LLM-bound) — ApplyAndVerifyStep uses it for ISSUES_FIXED
        # tracking, the commit's issue_keys, and (usefully) re-verifies
        # the deterministic fixes too via the same before/after count
        # check it already runs for LLM fixes.
        s[sk.CURRENT_FILE_GROUP] = {"file": group["file"], "issues": batch_issues}
        # The TRUE pre-patch text, not the post-mechanical-fix content —
        # verify_issue_patterns_resolved()'s before/after comparison and
        # _retry_full_file()'s diff both need the real starting point.
        s[sk.CURRENT_FILE_CONTENT] = original_content

        if not remaining_issues:
            # Every issue in this file was resolved by the deterministic
            # pre-pass — nothing left for fix_llm_agent to do. The file on
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


# Caps Gemini's thinking effort for fix_llm_agent. IMPORTANT: this model
# (gemini-3.5-flash, a Gemini 3.5+ model) deprecates the older numeric
# thinking_budget field in favor of this categorical thinking_level --
# per google.genai.types' own ReinforcementTuningThinkingLevel docstring,
# "Starting from Gemini 3.5 models, the old thinking_budget will no
# longer be supported ... use the thinking_level parameter instead."
# Confirmed live: setting thinking_budget=4096 on this model did NOT
# error, but also plainly wasn't enforced -- several single-file fixes
# still logged over 5000-6000 thinking tokens despite the cap. LOW (not
# MINIMAL) is the default: these are narrow, single-file, already-scoped
# fixes, not open-ended reasoning, but some (self-invocation setter
# wiring, multi-issue batches) do need genuine reasoning to get right.
_FIX_LLM_THINKING_LEVEL = types.ThinkingLevel(os.environ.get("FIX_LLM_THINKING_LEVEL", "LOW").upper())


def _build_fix_llm_agent() -> LlmAgent:
    """The only LLM call in the entire graph. A factory, not a module-level
    singleton, because per_file_loop (which embeds this) is instantiated
    twice (outer_loop and maintainability_expansion_loop) — ADK agents are
    single-parent nodes, so each embedding needs its own instance."""
    return LlmAgent(
        name="fix_llm_agent",
        model="gemini-3.7-flash",
        instruction="{temp:fix_prompt}",  # ADK injects state directly into instruction
        output_key=sk.PROPOSED_DIFF,
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=_FIX_LLM_THINKING_LEVEL),
        ),
    )


class FixLlmGateStep(BaseAgent):
    """Wraps fix_llm_agent so a file whose issues were fully resolved by
    FileFixerStep's deterministic pre-pass skips the LLM call entirely,
    instead of asking the model to regenerate a diff for zero remaining
    issues. Manually invokes the wrapped LlmAgent's .run_async(ctx) — the
    same free-standing-agent invocation pattern CheckpointGate already
    uses for checkpoint_pipeline — so its output_key write and
    usage_metadata plumbing behave exactly as when it was a bare
    per_file_loop sub_agent."""
    name: str = "fix_llm_gate_step"
    llm_agent: LlmAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        if s.get("temp:skip_llm_fix"):
            return
        # Hide the model's own prose+diff text — it's verbose (explanation
        # of the fix plus the full unified diff) and gets replaced by
        # ApplyAndVerifyStep's own concise "issue + compact diff" summary
        # once the fix is actually confirmed to work. Still yields the
        # event (via _hide_text, content stripped but actions/state_delta
        # intact) rather than dropping it outright — dropping it would
        # also drop the output_key write that lands PROPOSED_DIFF in
        # session.state; see _hide_text's docstring.
        llm_call_error = None
        try:
            async for event in self.llm_agent.run_async(ctx):
                llm_call_error = _llm_error_message(event) or llm_call_error
                yield _hide_text(event)
        except Exception as e:
            # A transient failure below ADK's own response handling (a
            # dropped connection, a timeout after tenacity's retries are
            # exhausted, ...) raises out of the async generator instead of
            # coming back as an error-bearing event -- confirmed live: a
            # WebGoat run died on ClientOSError: Connection reset by peer,
            # an uncaught exception that unwound all the way out of the
            # whole pipeline and killed a run that had already fixed 78
            # files, none of which got the chance to reach PushStep/
            # ReportStep despite being safely committed. Treated the same
            # way as a RECITATION block: this file's fix attempt failed,
            # not the whole run.
            llm_call_error = f"{type(e).__name__}: {e}"
        # Consumed by ApplyAndVerifyStep before it ever reads PROPOSED_DIFF
        # -- see _llm_error_message's docstring for why that read can't be
        # trusted when this is set.
        s["temp:llm_call_error"] = llm_call_error


class ApplyAndVerifyStep(BaseAgent):
    """Section 5.3 body: apply diff, quick compile check, then the
    verification (not regeneration) step from the 5.2/6.1 resolution."""
    name: str = "apply_and_verify_step"

    async def _retry_full_file(
        self, ctx: InvocationContext, group: dict, working_dir: str, reason: str,
    ) -> AsyncGenerator[Event, None]:
        """Fallback for when the diff-based fix fails — either git apply
        rejects it outright, or it applies but the result doesn't compile.
        Both were observed live to share the same root cause: fix_llm_agent
        miscounting unified-diff hunk headers on larger, multi-hunk edits.
        A wrong header either makes git apply reject the whole patch, or —
        worse — 'succeed' with some hunks silently mis-merged (e.g. a field
        rename applied but not every call site updated), which then fails
        at compile time instead of at apply time. Asking for the WHOLE file
        instead of a diff sidesteps hunk arithmetic entirely.

        Sets state["temp:full_file_retry_ok"] rather than returning a value
        (async generators can't `return` one) — the caller checks it once
        this generator is fully drained. Uses a fresh, unregistered
        LlmAgent (see _build_fix_llm_agent()'s docstring on why factories
        instead of singletons sidestep ADK's single-parent constraint) so
        this can run from inside another step with no sub_agents wiring,
        the same pattern IntakeStep/CheckpointGate already use for
        one-off/nested agent invocations."""
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
        # Hide the model's own text — that's the entire regenerated file,
        # and showing it verbatim dumps the whole class into the visible
        # chat/web log on every retry (confirmed live: a several-hundred-
        # line file rendered in full in adk web). Still yields the event
        # via _hide_text (content stripped, actions/state_delta intact),
        # not dropped outright — dropping it drops the output_key write
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
        # No diff shown here — ApplyAndVerifyStep's own summary (issue
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
        main fix -- this was a documented TODO hook ("wire a second,
        narrower fix_llm_agent invocation here if you hit this in
        practice") that sat unimplemented until it blocked real progress
        twice in one session: ExpenseService.java's S112 fix touched an
        unrelated occurrence and left the actually-flagged line alone,
        then PetClinicRuntimeHints.java's S125 fix edited the wrong one
        of two adjacent trailing-comment lines. Both cases are exactly
        what a focused second prompt is suited for -- naming precisely
        which issue(s) weren't resolved and where is a meaningfully
        different, easier task than the original "fix N issues in this
        file" batch that produced the mistake.

        Applied against the file's CURRENT on-disk content (already
        reflects whatever DID succeed from the main attempt) as the
        retry's starting point, not the original pre-fix content --
        re-doing already-correct changes isn't the goal. Only reverts
        back to that pre-retry content if the retry's compile check
        fails; a retry that compiles but only resolves SOME of the
        unresolved issues still keeps that partial progress rather than
        discarding it, since it's strictly more correct than not
        retrying at all and doesn't risk anything the compile check
        wouldn't already catch.

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

        applied = patch_tools.apply_diff(raw, working_dir, group["file"])
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

        # Check for a NO_SAFE_FIX refusal before ever touching apply_diff —
        # plain refusal prose isn't valid diff syntax, so apply_diff would
        # reject it safely, but only after a confusing "failed to apply"
        # message and a wasted full-file retry call that (observed live)
        # just refuses again for the same reason. Catching it here skips
        # straight to an honest, immediate "declined" outcome instead.
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
        # to disk — there's no diff to apply, so treat this file as
        # already "applied" and fall through to the same compile-check/
        # verify path every other fix goes through.
        applied = True if s.get("temp:skip_llm_fix") else patch_tools.apply_diff(
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
            # apply — same root cause as the apply-fail branch above (a
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
        # very first time — quick_compile_check above only proved it
        # compiles, not that it passes. Verify it here, in isolation, on
        # just this file, rather than letting a broken re-enabled test ride
        # into a shared checkpoint batch where the full build's failure
        # would drag every other file in that batch into a collateral
        # bisect-revert (observed live: a bad re-enabled test took 6
        # unrelated, individually-correct fixes down with it).
        if any(i["rule_key"] == "java:S2187" for i in group["issues"]):
            test_result = adapter.run_specific_tests(working_dir, [_java_fqcn(group["file"])])
            if not test_result.passed:
                git_tools.revert_file(working_dir, group["file"])
                failing = patch_tools.parse_junit_failures(working_dir)
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

        # This attempt is about to succeed — clear any stale flag left by an
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
            # None means this was a no-op — the regenerated fix was already
            # byte-identical to what's on disk (e.g. a redundant re-fix of
            # an already-correct file). Nothing new exists for a later
            # checkpoint to revert in that case, so it's deliberately left
            # out of this checkpoint's revertible batch — including it
            # would point RunFullVerifyStep's revert at the wrong commit
            # (whatever unrelated commit HEAD already was), not at a real
            # fix for this file.
            #
            # Tracked per-checkpoint (not just FILES_COMPLETED) so
            # RunFullVerifyStep knows exactly which commits are candidates
            # to revert if the full build fails — cleared after each
            # checkpoint fires, pass or fail.
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
    """Factory, not a module-level singleton — see _build_fix_llm_agent()."""
    return LoopAgent(
        name="per_file_loop",
        sub_agents=[
            FileFixerStep(),
            FixLlmGateStep(llm_agent=_build_fix_llm_agent()),
            ApplyAndVerifyStep(),
            CheckpointGate(),
        ],
        max_iterations=1000,  # real exit is FileFixerStep's escalate=True on empty queue
    )


def _strip_escalate(event: Event) -> Event:
    """ADK's LoopAgent re-yields every sub-agent event upward completely
    unmodified, and checks event.actions.escalate at EVERY nesting level
    it passes through. FileFixerStep's escalate=True (its own queue-empty
    signal, meant only to stop per_file_loop's iteration) therefore also
    terminates whatever LoopAgent per_file_loop is embedded in (outer_loop,
    maintainability_expansion_loop) the moment it bubbles through —
    confirmed live: outer_loop had never run a second iteration in this
    project's history, always exiting after exactly one per_file_loop
    pass regardless of whether files ended up flagged and outer_loop's
    own re-fetch-and-retry (Section 5.5, MAX_OUTER_ITERATIONS) was meant
    to give them another attempt; OuterExitCheck itself never ran (its
    OUTER_ITERATION counter stayed 0 in the final report). Stripping the
    flag here, once, at the per_file_loop boundary, means only
    per_file_loop's own LoopAgent sees it — the enclosing loop's own exit
    check (OuterExitCheck / MaintainabilityDebtCheckStep) makes its own
    independent decision instead of being silently pre-empted."""
    if not event.actions.escalate:
        return event
    return event.model_copy(update={"actions": event.actions.model_copy(update={"escalate": False})})


class PerFileLoopStep(BaseAgent):
    """Wraps a per_file_loop instance so its own internal LoopAgent exit
    signal doesn't also terminate whatever LoopAgent embeds it — see
    _strip_escalate. Manually invokes .run_async(ctx), the same
    free-standing-agent pattern used elsewhere (CheckpointGate,
    IntakeStep) for a child that needs its own event post-processing."""
    name: str = "per_file_loop_step"
    loop: LoopAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        async for event in self.loop.run_async(ctx):
            yield _strip_escalate(event)
