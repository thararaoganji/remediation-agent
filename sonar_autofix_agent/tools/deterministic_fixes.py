"""
Deterministic (non-LLM) fixers for the handful of Sonar Java rules whose
correct fix is a single, unambiguous textual transform. Each fixer only
applies when the flagged line range matches its exact expected violation
shape; on anything less certain it returns None so the caller falls back
to the normal LLM path for that issue, unchanged. Correctness (never
producing a subtly wrong file) is prioritized over coverage — these are
meant to shrink the LLM's workload for the easy, mechanical slice of
issues, not to replace it.
"""

import re


def _replace_in_range(source: str, start_line: int, end_line: int, pattern: re.Pattern, repl) -> str | None:
    """Applies `pattern`'s substitution only within lines
    [start_line, end_line] (1-indexed, inclusive — Sonar's own textRange
    convention) of `source`, and only if the pattern matches at least
    once in that window — so a fixer never touches an unrelated
    occurrence elsewhere in the file. Returns None if the window doesn't
    contain the expected pattern at all, which the caller treats as "this
    fixer declined."""
    lines = source.splitlines(keepends=True)
    lo, hi = max(0, start_line - 1), min(len(lines), end_line)
    window = "".join(lines[lo:hi])
    new_window, count = pattern.subn(repl, window)
    if count == 0:
        return None
    lines[lo:hi] = [new_window]
    return "".join(lines)


def _fix_s6204(source: str, issue: dict) -> str | None:
    """.collect(Collectors.toList()) -> .toList() (Java 16+ Stream API).
    Same regex patch_tools._VIOLATION_COUNT uses for java:S6204, so a
    successful fix here is guaranteed to also satisfy that later
    verification pass."""
    pattern = re.compile(r"\.collect\(\s*Collectors\.toList\(\)\s*\)")
    return _replace_in_range(source, issue["start_line"], issue["end_line"], pattern, ".toList()")


def _fix_s125(source: str, issue: dict) -> str | None:
    """Commented-out code: delete the exact flagged line range outright.
    S125's own textRange already brackets precisely the commented block
    Sonar flagged — nothing else in the file is touched."""
    lines = source.splitlines(keepends=True)
    lo, hi = max(0, issue["start_line"] - 1), min(len(lines), issue["end_line"])
    if lo >= hi:
        return None
    del lines[lo:hi]
    return "".join(lines)


_S3_BUILDER_RE = re.compile(r"(S3Client\.builder\(\))(.*?)(\.build\(\))", re.DOTALL)


def _fix_s6242(source: str, issue: dict) -> str | None:
    """S6242: S3Client.builder()...build() with no explicit
    .credentialsProvider(...) — inserts one using
    DefaultCredentialsProvider.builder().build(), matching both the
    STRICT JAVA REMEDIATION RULES guidance in prompts.py (builder().build(),
    never .create()) and patch_tools._s3_credentials_provider_missing_count,
    the same check verification uses afterward."""
    lines = source.splitlines(keepends=True)
    lo, hi = max(0, issue["start_line"] - 1), min(len(lines), issue["end_line"])
    window = "".join(lines[lo:hi])
    m = _S3_BUILDER_RE.search(window)
    if not m or ".credentialsProvider(" in m.group(2):
        return None
    insertion = ".credentialsProvider(DefaultCredentialsProvider.builder().build())"
    new_window = window[: m.start(3)] + insertion + window[m.start(3):]
    lines[lo:hi] = [new_window]
    return "".join(lines)


_LOG_CONCAT_RE = re.compile(r'(\blog(?:ger)?\.\w+\()\s*"([^"]*)"\s*\+\s*([^,;)+"]+?)\s*\)')


def _fix_s2629(source: str, issue: dict) -> str | None:
    """S2629: narrow case only — a logger call whose message is exactly
    one string literal concatenated with exactly one other expression via
    `+` (e.g. log.debug("error: " + err.getMessage());). Converts to a
    `{}` placeholder. Anything with more than one `+`, nested
    concatenation, or no string literal at all is left for the LLM —
    parsing arbitrary Java expression trees reliably needs a real parser,
    not a regex, so this only ever touches the one unambiguous shape."""
    def repl(m: re.Match) -> str:
        call, literal, expr = m.group(1), m.group(2), m.group(3).strip()
        return f'{call}"{literal}{{}}", {expr})'
    return _replace_in_range(source, issue["start_line"], issue["end_line"], _LOG_CONCAT_RE, repl)


_ASSERT_THROWS_RE = re.compile(
    r"(?P<prefix>[ \t]*.*?assertThrows\([^,]+,\s*\(\)\s*->\s*\{)\s*"
    r"(?P<setup>[^\n;]+;)\s*"
    r"(?P<call>[^\n;]+;)\s*"
    r"\}\s*\);(?P<trailing>.*)",
    re.DOTALL,
)
_LEADING_WS_RE = re.compile(r"[ \t]*")


def _fix_s5778(source: str, issue: dict) -> str | None:
    """S5778: narrow case only — an assertThrows lambda with exactly two
    statements, `() -> { <setup>; <call>; }`, where the first is a `new`
    construction/local-var assignment and the second is the sole method
    call being asserted. Hoists the first statement above the
    assertThrows call and shrinks the lambda to just the second, keeping
    the assertThrows/closing-brace lines at the block's original
    indentation rather than jamming everything onto one line. Any other
    statement count or shape is left for the LLM."""
    lines = source.splitlines(keepends=True)
    lo, hi = max(0, issue["start_line"] - 1), min(len(lines), issue["end_line"])
    window = "".join(lines[lo:hi])
    m = _ASSERT_THROWS_RE.match(window)
    if not m:
        return None
    indent = _LEADING_WS_RE.match(m.group("prefix")).group()
    new_window = (
        f"{indent}{m.group('setup')}\n"
        f"{m.group('prefix')}\n"
        f"{indent}    {m.group('call')}\n"
        f"{indent}}});{m.group('trailing')}"
    )
    lines[lo:hi] = [new_window]
    return "".join(lines)


_IMPORT_LINE_RE = re.compile(r"^[ \t]*import\s+[\w.]+(?:\.\*)?\s*;\s*\n?$")


def _fix_s1128(source: str, issue: dict) -> str | None:
    """S1128 (unused import): Sonar already determined the flagged import
    is unused — that's what triggered the finding — so this just deletes
    the exact line it flagged. Guarded by a sanity check that the line
    actually looks like an import statement (in case of a stale offset)
    and that exactly one line was flagged, so a multi-line issue range
    (shouldn't normally happen for this rule) falls through to the LLM
    instead of deleting the wrong thing."""
    lines = source.splitlines(keepends=True)
    lo, hi = max(0, issue["start_line"] - 1), min(len(lines), issue["end_line"])
    if hi - lo != 1 or not _IMPORT_LINE_RE.match(lines[lo]):
        return None
    del lines[lo:hi]
    return "".join(lines)


_TYPE_DECL_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?:public|private|protected|static|final|abstract)\s+)*"
    r"(?:class|interface)\s+\w+"
)


def _fix_s2094(source: str, issue: dict) -> str | None:
    """S2094 (empty marker interface): per the STRICT JAVA REMEDIATION
    RULES in prompts.py, this is one of only three rules where
    @SuppressWarnings is the sanctioned fix rather than a workaround — a
    marker interface is deliberately empty by design. Inserts the
    suppression directly above the flagged class/interface declaration,
    scanning back through any existing stacked annotations first (same
    approach patch_tools._has_class_level_suppression uses) so it's never
    duplicated if one is already present some other way."""
    lines = source.splitlines(keepends=True)
    start = issue["start_line"] - 1
    if not (0 <= start < len(lines)):
        return None
    m = _TYPE_DECL_RE.match(lines[start])
    if not m:
        return None
    j = start - 1
    while j >= 0 and (lines[j].strip().startswith("@") or not lines[j].strip()):
        if "SuppressWarnings" in lines[j] or "NOSONAR" in lines[j]:
            return None  # already suppressed — nothing to do
        j -= 1
    lines.insert(start, f'{m.group("indent")}@SuppressWarnings("java:S2094")\n')
    return "".join(lines)


_LITERAL = r'(?:"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)\'|-?\d+(?:\.\d+)?[lLfFdD]?|true|false|null)'
_UNUSED_DECL_RE = re.compile(
    r"^[ \t]*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:private|protected|public|static|final|transient|volatile)\s+)*"
    r"[\w<>\[\],.]+(?:\s*<[^>]*>)?\s+\w+"
    rf"(?:\s*=\s*{_LITERAL})?\s*;\s*\n?$"
)


def _fix_unused_decl(source: str, issue: dict) -> str | None:
    """S1481 (unused local variable) / S1068 (unused private field):
    deletes the flagged declaration line, but ONLY when its initializer
    (if any) is a plain literal or absent entirely — a method call or
    `new Foo()` initializer can have side effects that a blind delete
    would silently drop, so anything more complex is left for the LLM.
    Sonar already confirmed nothing reads this declaration (that's what
    triggered the finding, and for a private field it's a whole-class
    analysis Sonar itself already did) — the only remaining risk this
    guards against is the write side, not the read side. Modifiers
    (private/final/etc.) are optional, not required — ordinary unused
    locals (the common S1481 case) have none at all, e.g. `int x = 5;`.
    The type+name pair (two space-separated tokens before the optional
    `= literal`) is what rules out a bare reassignment like `x = 5;`
    matching by accident — that has only one token, not two."""
    lines = source.splitlines(keepends=True)
    lo, hi = max(0, issue["start_line"] - 1), min(len(lines), issue["end_line"])
    if hi - lo != 1 or not _UNUSED_DECL_RE.match(lines[lo]):
        return None
    del lines[lo:hi]
    return "".join(lines)


DETERMINISTIC_FIXERS = {
    "java:S6204": _fix_s6204,
    "java:S125": _fix_s125,
    "java:S6242": _fix_s6242,
    "java:S2629": _fix_s2629,
    "java:S5778": _fix_s5778,
    "java:S1128": _fix_s1128,
    "java:S2094": _fix_s2094,
    "java:S1481": _fix_unused_decl,
    "java:S1068": _fix_unused_decl,
}


_COLLECTORS_IMPORT_RE = re.compile(r"^import\s+java\.util\.stream\.Collectors;[ \t]*\n?", re.MULTILINE)
_COLLECTORS_USAGE_RE = re.compile(r"\bCollectors\.")


def _strip_unused_collectors_import(source: str) -> str:
    """S6204's fix (.collect(Collectors.toList()) -> .toList()) can strand
    `import java.util.stream.Collectors;` as unused if that was the only
    reference in the file — Sonar's own S1128 (unused imports) would then
    flag it as a brand-new issue, which the shared prompt skeleton's
    "do not introduce new findings" requirement explicitly forbids for
    the LLM path too. Only ever removes this one specific import, which
    is the only stranding this fixer's own transform can cause — not a
    general unused-import pass."""
    if _COLLECTORS_USAGE_RE.search(source):
        return source
    return _COLLECTORS_IMPORT_RE.sub("", source, count=1)


def apply_deterministic_fixes(
    source: str, issues: list[dict],
) -> tuple[str, list[dict], list[dict]]:
    """Applies every deterministic fixer that both (a) has a matching
    rule_key and (b) confidently applies (returns non-None) against
    `source`. `issues` is expected already bottom-to-top ordered (e.g.
    from patch_tools.issues_for_prompt()) — processing in that order
    means an edit near the end of the file never shifts the line numbers
    an earlier, not-yet-processed issue still needs to target, the same
    reasoning issues_for_prompt's own ordering exists for.

    Returns (patched_source, applied_issues, remaining_issues) —
    remaining_issues is every issue NOT resolved here (no fixer for that
    rule_key, or the fixer declined), unchanged and still meant for the
    LLM prompt."""
    applied, remaining = [], []
    for issue in issues:
        fixer = DETERMINISTIC_FIXERS.get(issue["rule_key"])
        if fixer is None:
            remaining.append(issue)
            continue
        patched = fixer(source, issue)
        if patched is None:
            remaining.append(issue)
            continue
        source = patched
        applied.append(issue)

    if any(i["rule_key"] == "java:S6204" for i in applied):
        source = _strip_unused_collectors_import(source)

    return source, applied, remaining
