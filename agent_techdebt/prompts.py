"""
Section 6: shared skeleton + per-language addenda.

build_fix_prompt() is called by a tool (tools/patch_tools.py), not injected
via ADK's {key} instruction templating, because the issue list needs
pre-formatting (bottom-to-top ordering, cluster filtering per the 5.2/6.1
resolution) that's easier to do in Python than in a template string.
"""

SHARED_SKELETON = """\
You are fixing static analysis findings from SonarQube in an existing production codebase.

FILE: {file_path}
LANGUAGE: {language}

FULL FILE CONTENT:
{file_content}

ISSUES TO FIX IN THIS FILE (already ordered bottom-to-top; fix all of them
in this single pass, in the order given):
{issue_block}

REQUIREMENTS:
1. Preserve existing behavior exactly unless the issue description explicitly
   requires a behavior change (e.g. a real bug fix for a RELIABILITY issue).
2. Fix ONLY the listed issues. Do not refactor, rename, reformat, or "improve"
   any code outside the flagged ranges, even if you notice other problems.
3. Do not introduce new SonarQube findings: no new unused variables/imports,
   no new magic numbers, no reduced test coverage, no new complexity.
4. If two issues in this file overlap or are nested, resolve the outer/structural
   one first and re-derive the inner one against the resulting code, IN THIS
   SAME RESPONSE. You have the full file text, not stale line offsets — do not
   assume you need a second turn to see the effect of the outer fix.
5. If a listed issue cannot be safely fixed without more context than provided
   (e.g. requires understanding a caller's contract elsewhere in the codebase),
   output NO_SAFE_FIX for that issue with a one-line reason, and do not guess.
6. Output a unified diff (or the adapter's expected patch format) — not the
   full rewritten file — so each change is independently reviewable.

OUTPUT FORMAT:
{output_format}
"""

# The actual Java/Spring guidance text now lives on the adapters
# themselves (core/adapters/base.py's _JAVA_FIX_PROMPT_ADDENDUM, returned by
# JavaMavenAdapter/JavaGradleAdapter.get_fix_prompt_addendum()) rather than
# here -- this module's build_fix_prompt() takes language_addendum as a
# plain parameter precisely so it never needs to know which language it's
# building for. Re-exported under its old name for backward compatibility
# (tests/test_prompts.py still imports it from here).
from core.adapters.base import _JAVA_FIX_PROMPT_ADDENDUM as JAVA_SPRING_ADDENDUM  # noqa: F401


def build_issue_block(issues: list[dict]) -> str:
    lines = []
    for i in issues:
        lines.append(
            f"  - Rule: {i['rule_key']} — {i['rule_name']}\n"
            f"    Category: {i['category']}\n"
            f"    Severity: {i['severity']}\n"
            f"    Lines: {i['start_line']}-{i['end_line']}\n"
            f"    Message: {i['message']}\n"
            f"    Rule description: {i['rule_description']}"
        )
    return "\n".join(lines)


def build_fix_prompt(
    file_path: str,
    language: str,
    file_content: str,
    issues_bottom_to_top: list[dict],
    language_addendum: str,
    output_format: str = "unified diff",
) -> str:
    """issues_bottom_to_top must already have colliding-cluster issues
    removed by the caller (see tools/patch_tools.classify_and_prepare_batch) —
    this function does no cluster logic itself."""
    prompt = SHARED_SKELETON.format(
        file_path=file_path,
        language=language,
        file_content=file_content,
        issue_block=build_issue_block(issues_bottom_to_top),
        output_format=output_format,
    )
    return prompt + "\n" + language_addendum
