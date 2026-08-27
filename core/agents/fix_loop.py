"""Tool-agnostic per-file loop machinery: the LLM-call gate, the loop-nesting
escalate fix-up, and the small text-processing helpers every fix-generation
step needs (diff extraction, NO_SAFE_FIX detection, LLM-error detection).

None of this knows or cares whether the "issue" being fixed is a Sonar rule
violation, an uncovered line, a duplicated block, or (eventually) a Veracode/
Black Duck finding — that's exactly why it lives here rather than in a
per-tool package. The Sonar-issue-shaped pieces (FileFixerStep,
ApplyAndVerifyStep, the per-issue verification) stay in each Sonar agent
that needs them."""

import os
import re
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from .. import state_schema as sk

_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_DIFF_ARTIFACT_RE = re.compile(r"^diff --git |^@@ .*@@", re.MULTILINE)


def _looks_like_diff(text: str) -> bool:
    """Observed live: when a full-file retry is asked to regenerate the
    whole file, the model can still degrade back into diff-shaped output
    partway through — the extracted 'full file' ends with a literal
    `diff --git a/...` / `@@ ... @@` block instead of real source, which
    then fails to compile with confusing javac errors ("class, interface,
    enum, or record expected") pointing at that embedded diff syntax.
    Catching this before writing to disk turns a wasted compile-check
    cycle into an immediate, clearly-explained decline."""
    return bool(_DIFF_ARTIFACT_RE.search(text))


def _llm_error_message(event: Event) -> str | None:
    """Event extends google-adk's LlmResponse, so error_code/error_message
    are set directly on it whenever the model's turn ends with a
    finish_reason other than STOP (RECITATION, SAFETY, ...) — ADK's own
    output_key mechanism (LlmAgent.__maybe_save_output_to_state) only
    writes state_delta[output_key] when event.content has actual text
    parts, which a blocked turn never has. Every site that reads
    PROPOSED_DIFF right after an LLM call must check this first."""
    if event.error_code is None:
        return None
    return f"{event.error_code}: {event.error_message or 'no further detail from the model API'}"


_NO_SAFE_FIX_RE = re.compile(r"^\s*NO_SAFE_FIX\s*:?\s*(.*)$", re.MULTILINE)


def _no_safe_fix_reason(text: str) -> str | None:
    """Every fix prompt in every agent tells the model to respond with
    'NO_SAFE_FIX: <one-line reason>' instead of guessing when a fix can't
    be made safely with the context it has. Returns the reason text if
    found, else None — callers must check this before treating a response
    as diff or full-file content, not after (plain refusal prose isn't
    valid diff syntax, so a naive apply_diff call would reject it safely,
    but only after a confusing "failed to apply" message)."""
    m = _NO_SAFE_FIX_RE.search(text)
    if not m:
        return None
    return m.group(1).strip() or "no reason given"


def _extract_code_block(text: str) -> str:
    """fix_llm_agent's responses are consistently prose explanation
    followed by one fenced code block — every response observed live
    follows this shape, even when the prompt explicitly asks for raw
    output only. Falls back to the raw text if no fence is found, in case
    the model does comply literally."""
    m = _CODE_FENCE_RE.search(text)
    return m.group(1) if m else text


def _java_fqcn(file_path: str) -> str:
    """Converts a Java source path (relative to the repo root) to its
    fully-qualified class name — e.g.
    'src/test/java/portal/expenses/controller/AuthControllerTest.java' ->
    'portal.expenses.controller.AuthControllerTest'. Assumes the standard
    Maven/Gradle layout (a 'java/' segment marking the source root).
    Works for both src/main/java and src/test/java, since it only anchors
    on the literal 'java' folder name."""
    parts = file_path.replace("\\", "/").split("/")
    if "java" in parts:
        parts = parts[parts.index("java") + 1:]
    joined = "/".join(parts)
    if joined.endswith(".java"):
        joined = joined[: -len(".java")]
    return joined.replace("/", ".")


def _hide_text(event: Event) -> Event:
    """Strips a text-bearing event's visible content while preserving its
    `actions` (notably `state_delta`, which is how output_key writes reach
    session.state — see LlmAgent.__maybe_save_output_to_state). ADK only
    applies an event's state_delta when that event reaches the top-level
    Runner via session_service.append_event(); a step that drops the event
    entirely (rather than yielding a version of it) also drops that write.
    Non-text events (function calls/responses) pass through unchanged;
    only cosmetic content is touched, `.model_copy` leaves `actions` (and
    everything else) as the same object."""
    if not (event.content and any(getattr(p, "text", None) for p in event.content.parts or [])):
        return event
    return event.model_copy(update={"content": None})


# Caps Gemini's thinking effort for fix_llm_agent. IMPORTANT: this model
# (gemini-3.5-flash, a Gemini 3.5+ model) deprecates the older numeric
# thinking_budget field in favor of this categorical thinking_level --
# per google.genai.types' own ReinforcementTuningThinkingLevel docstring,
# "Starting from Gemini 3.5 models, the old thinking_budget will no
# longer be supported ... use the thinking_level parameter instead."
# LOW (not MINIMAL) is the default: these are narrow, already-scoped
# fixes, not open-ended reasoning, but some do need genuine reasoning to
# get right.
_FIX_LLM_THINKING_LEVEL = types.ThinkingLevel(os.environ.get("FIX_LLM_THINKING_LEVEL", "LOW").upper())


def _build_fix_llm_agent() -> LlmAgent:
    """The only LLM call in the entire graph, for every agent that embeds
    this per-file loop. A factory, not a module-level singleton, because
    a per-file loop built from this can be instantiated more than once
    (e.g. a main pass and a later expansion pass) — ADK agents are
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
    """Wraps an LlmAgent so a file whose issues were fully resolved by a
    deterministic pre-pass (if the embedding agent has one) skips the LLM
    call entirely, instead of asking the model to regenerate a diff for
    zero remaining issues. Manually invokes the wrapped LlmAgent's
    .run_async(ctx) — the same free-standing-agent invocation pattern used
    elsewhere for a nested one-off agent call — so its output_key write and
    usage_metadata plumbing behave exactly as when it was a bare loop
    sub_agent."""
    name: str = "fix_llm_gate_step"
    llm_agent: LlmAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        if s.get("temp:skip_llm_fix"):
            return
        # Hide the model's own prose+diff text — it's verbose and gets
        # replaced by the caller's own concise summary once the fix is
        # actually confirmed to work. Still yields the event (via
        # _hide_text, content stripped but actions/state_delta intact)
        # rather than dropping it outright — dropping it would also drop
        # the output_key write that lands PROPOSED_DIFF in session.state.
        llm_call_error = None
        try:
            async for event in self.llm_agent.run_async(ctx):
                llm_call_error = _llm_error_message(event) or llm_call_error
                yield _hide_text(event)
        except Exception as e:
            # A transient failure below ADK's own response handling (a
            # dropped connection, a timeout after tenacity's retries are
            # exhausted, ...) raises out of the async generator instead of
            # coming back as an error-bearing event. Treated the same way
            # as a RECITATION block: this file's fix attempt failed, not
            # the whole run.
            llm_call_error = f"{type(e).__name__}: {e}"
        # Consumed by the caller before it ever reads PROPOSED_DIFF -- see
        # _llm_error_message's docstring for why that read can't be
        # trusted when this is set.
        s["temp:llm_call_error"] = llm_call_error


def _strip_escalate(event: Event) -> Event:
    """ADK's LoopAgent re-yields every sub-agent event upward completely
    unmodified, and checks event.actions.escalate at EVERY nesting level
    it passes through. A per-file loop's own "queue empty" escalate=True
    signal, meant only to stop ITS OWN iteration, would therefore also
    terminate whatever LoopAgent embeds it (an outer fetch/re-fetch loop)
    the moment it bubbles through. Stripping the flag here, once, at the
    per-file-loop boundary, means only the inner loop sees it — the
    enclosing loop's own exit check makes its own independent decision
    instead of being silently pre-empted."""
    if not event.actions.escalate:
        return event
    return event.model_copy(update={"actions": event.actions.model_copy(update={"escalate": False})})


class PerFileLoopStep(BaseAgent):
    """Wraps a per-file LoopAgent instance so its own internal LoopAgent
    exit signal doesn't also terminate whatever LoopAgent embeds it -- see
    _strip_escalate. Manually invokes .run_async(ctx), the same free-
    standing-agent pattern used elsewhere for a child that needs its own
    event post-processing."""
    name: str = "per_file_loop_step"
    loop: LoopAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        async for event in self.loop.run_async(ctx):
            yield _strip_escalate(event)
