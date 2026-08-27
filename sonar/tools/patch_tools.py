"""
Sonar-issue-shaped patch tooling: cluster classification (colliding/nested/
independent textRanges) and per-issue verification. Both are inherently
keyed to Sonar's issue shape (rule_key, textRange, severity) -- a Veracode/
Black Duck finding doesn't look like this, so this stays here rather than
in core. The tool-agnostic half (apply_diff, parse_junit_failures) lives in
core.tools.patch_tools instead.
"""

import os
import re
from dataclasses import dataclass


def _ranges_overlap(a: dict, b: dict) -> bool:
    return not (a["end_offset"] <= b["start_offset"] or b["end_offset"] <= a["start_offset"])


def _nested(a: dict, b: dict) -> bool:
    return (a["start_offset"] <= b["start_offset"] and a["end_offset"] >= b["end_offset"]) or \
           (b["start_offset"] <= a["start_offset"] and b["end_offset"] >= a["end_offset"])


@dataclass
class ClusterResult:
    independent: list[dict]        # flat list, safe to send to LLM as-is
    nested_pairs: list[tuple]      # (outer, inner), also sent to LLM (in one pass)
    colliding_flagged: list[dict]  # excluded from prompt, logged for manual review


def classify_and_prepare_batch(issues_in_file: list[dict]) -> ClusterResult:
    """Groups by exact/overlapping textRange on the same line(s), classifies
    each cluster, and returns the set of issues that are SAFE to include in
    a single batched fix prompt. Colliding issues never reach the LLM."""
    # group by line for overlap comparison
    by_line: dict[int, list[dict]] = {}
    for i in issues_in_file:
        by_line.setdefault(i["start_line"], []).append(i)

    independent, nested_pairs, colliding = [], [], []
    for _, cluster in by_line.items():
        if len(cluster) == 1:
            independent.append(cluster[0])
            continue
        a, b = cluster[0], cluster[1]  # simplifying to pairs; extend for 3+ if needed
        if _nested(a, b):
            nested_pairs.append((a, b))
        elif _ranges_overlap(a, b):
            colliding.append(a)
            colliding.append(b)
        else:
            independent.extend(cluster)

    return ClusterResult(independent, nested_pairs, colliding)


def issues_for_prompt(cluster_result: ClusterResult) -> list[dict]:
    """Flattens independent + nested (both members) into the bottom-to-top
    list that goes into the single batched prompt. Colliding issues are
    deliberately excluded here."""
    flat = list(cluster_result.independent)
    for outer, inner in cluster_result.nested_pairs:
        flat.extend([outer, inner])
    flat.sort(key=lambda i: (-i["start_line"], -i["start_offset"]))
    return flat


def _s3_credentials_provider_missing_count(source: str) -> int:
    """S6242: counts S3Client.builder()...build() chains with no explicit
    .credentialsProvider(...) call."""
    count = 0
    for m in re.finditer(r"S3Client\.builder\(\)(.*?)\.build\(\)", source, re.DOTALL):
        if ".credentialsProvider(" not in m.group(1):
            count += 1
    return count


_CLASS_DECL_RE = re.compile(r"^\s*(?:public\s+|final\s+|abstract\s+)*class\s+\w+")


def _has_class_level_suppression(lines: list[str]) -> bool:
    """A @SuppressWarnings on the class itself covers every member inside
    it -- including a .csrf().disable() call many lines below the class
    declaration, well outside _csrf_disabled_unsuppressed()'s local ±5/+2
    line window. Scans backward from the first class declaration through
    its stacked annotations (@Configuration, @EnableWebSecurity, etc. are
    common alongside it) rather than assuming the suppression sits
    immediately next to the call site."""
    for i, line in enumerate(lines):
        if _CLASS_DECL_RE.match(line):
            j = i - 1
            while j >= 0 and (lines[j].strip().startswith("@") or not lines[j].strip()):
                if "SuppressWarnings" in lines[j] or "NOSONAR" in lines[j]:
                    return True
                j -= 1
            break
    return False


def _csrf_disabled_unsuppressed_count(source: str) -> int:
    """S4502: counts `.csrf(...disable())` calls with no suppression marker
    in the surrounding few lines (the annotation/NOSONAR comment is rarely
    on the exact same line as the call itself) and no class-level
    suppression covering the whole class."""
    lines = source.splitlines()
    if _has_class_level_suppression(lines):
        return 0
    count = 0
    for i, line in enumerate(lines):
        if "csrf" in line and ".disable()" in line:
            window = lines[max(0, i - 5):i + 2]
            if not any("SuppressWarnings" in w or "NOSONAR" in w for w in window):
                count += 1
    return count


def _hardcoded_credential_literal_count(source: str) -> int:
    """S6437/S2068-style: counts string literals assigned directly to a
    password/secret/token/credential-named variable, not sourced from
    getenv/getProperty/@Value/config."""
    pattern = re.compile(
        r'\b\w*(?:password|secret|apikey|api_key|credential)\w*\s*=\s*"[^"]+"',
        re.IGNORECASE,
    )
    count = 0
    for m in pattern.finditer(source):
        line = source[max(0, m.start() - 80):m.end() + 80]
        if not re.search(r"getenv|getProperty|@Value|System\.console", line):
            count += 1
    return count


# Rule-specific "how many times is this violation still present" checks --
# return a match COUNT, not a bool. Deliberately covers only rules where a
# regex check is reliable without real AST parsing (SonarQube's Java rule
# set is 600+ rules; exhaustive regex coverage isn't feasible). A rule_key
# not in this table defaults to "resolved" rather than flagging it for
# manual review -- treating every uncovered rule as unresolved would flood
# FILES_FLAGGED with fixes that almost certainly succeeded, defeating the
# point of a targeted review queue.
#
# Counts, not booleans, because these checks are whole-file text search, not
# line/offset-anchored -- a pure "does the pattern exist anywhere in the
# file" boolean is a false-positive trap the moment a file has an
# unrelated, never-flagged occurrence of the same pattern. Comparing
# before/after occurrence counts and requiring the count to drop by at
# least as many issues as were flagged sidesteps that: it only demands that
# the flagged violations went away, not that every occurrence of the
# pattern vanished.
_VIOLATION_COUNT = {
    "java:S106": lambda src: len(re.findall(r"System\.(?:out|err)\.print", src)),
    "java:S112": lambda src: len(re.findall(r"\bnew\s+(?:RuntimeException|Exception)\s*\(", src)),
    "java:S2187": lambda src: len(re.findall(r"^\s*//\s*@Test\b", src, re.MULTILINE)),
    "java:S6204": lambda src: len(re.findall(r"\.collect\(\s*Collectors\.toList\(\)\s*\)", src)),
    "java:S6242": _s3_credentials_provider_missing_count,
    "java:S4502": _csrf_disabled_unsuppressed_count,
    "java:S6437": _hardcoded_credential_literal_count,
}


_STRUCTURAL_ONLY_RE = re.compile(r"[\s{}();,]")
_MIN_MEANINGFUL_FLAGGED_CHARS = 6


def _is_meaningful_flagged_text(flagged_text: str) -> bool:
    """Guards the per-issue 'is this exact flagged text still present'
    check below against short, structural-only lines that trivially
    recur throughout any Java file regardless of whether the actual fix
    landed. Stripping whitespace and common structural punctuation
    (braces, parens, semicolons, commas) before measuring length filters
    out exactly that shape of line while still catching real code
    (identifiers, literals, keywords) as meaningful."""
    return len(_STRUCTURAL_ONLY_RE.sub("", flagged_text)) >= _MIN_MEANINGFUL_FLAGGED_CHARS


def verify_issue_patterns_resolved(
    file_path: str, issues: list[dict], working_dir: str, original_content: str = "",
) -> dict[str, bool]:
    """The 'verification, not regeneration' step: cheap pattern/AST check
    per issue that its violation is actually gone from the now-patched file.
    Returns {issue_key: resolved_bool}. Anything False here is the ONLY
    trigger for a narrow single-issue follow-up LLM call.

    Issues are grouped by rule_key so multiple same-rule issues in one file
    are judged together against a before/after occurrence count rather than
    each independently re-running a whole-file existence check -- see
    _VIOLATION_COUNT's docstring for why that matters. `original_content`
    (the pre-patch file text) is what makes the "before" half of that
    comparison possible; if it isn't supplied, this falls back to the
    older, coarser "no violation anywhere left in the file" check.

    The aggregate count check alone has its own blind spot: it only checks
    that the TOTAL occurrence count dropped by enough, not that the
    specific flagged occurrences are the ones that changed. Closed by
    additionally requiring each individual issue's own original flagged
    line(s) to no longer appear verbatim in the patched file -- this per-
    line check runs for EVERY issue, not just ones with a _VIOLATION_COUNT
    entry, since quick_compile_check passing plus a targeted prompt isn't
    always enough signal on its own.

    That per-line check is occurrence-COUNT based, not a simple substring
    presence check, for the same reason _VIOLATION_COUNT's own before/after
    comparison is -- a bare "is this text still anywhere in the file" check
    can't tell "the flagged occurrence got fixed" from "an unrelated,
    never-flagged occurrence of byte-identical boilerplate elsewhere in the
    file is untouched". Counting occurrences per exact flagged text
    (grouped across issues that happen to share identical text, same as
    _VIOLATION_COUNT groups by rule) fixes this while still catching the
    original bug this check exists for: if a flagged line's exact text
    genuinely persists unchanged, its before/after count for that text
    doesn't drop, so it's still correctly reported unresolved."""
    with open(os.path.join(working_dir, file_path), encoding="utf-8") as f:
        after_source = f.read()
    original_lines = original_content.splitlines() if original_content else []

    by_rule: dict[str, list[dict]] = {}
    for issue in issues:
        by_rule.setdefault(issue["rule_key"], []).append(issue)

    result = {}
    for rule_key, rule_issues in by_rule.items():
        count_fn = _VIOLATION_COUNT.get(rule_key)
        if count_fn is None:
            resolved = True
        else:
            after_count = count_fn(after_source)
            if original_content:
                before_count = count_fn(original_content)
                resolved = (before_count - after_count) >= len(rule_issues)
            else:
                resolved = after_count == 0
        for issue in rule_issues:
            result[issue["issue_key"]] = resolved

        if not (resolved and original_lines):
            continue

        text_groups: dict[str, list[dict]] = {}
        for issue in rule_issues:
            lo = max(0, issue["start_line"] - 1)
            flagged_text = "\n".join(original_lines[lo:issue["end_line"]]).strip()
            if _is_meaningful_flagged_text(flagged_text):
                text_groups.setdefault(flagged_text, []).append(issue)

        for flagged_text, group_issues in text_groups.items():
            before_text_count = original_content.count(flagged_text)
            after_text_count = after_source.count(flagged_text)
            if (before_text_count - after_text_count) < len(group_issues):
                for issue in group_issues:
                    result[issue["issue_key"]] = False
    return result
