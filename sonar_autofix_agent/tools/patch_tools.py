"""
Section 5.2 cluster classification + the 5.2/6.1 resolution described above:
colliding clusters are filtered out BEFORE the prompt is built; nested
clusters stay in the single batched prompt; verification (not re-generation)
is the default path back after the diff is applied.
"""

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


def apply_diff(diff_text: str, working_dir: str, file_path: str) -> bool:
    raise NotImplementedError("wire to `git apply` or patch library against working_dir")


def verify_issue_patterns_resolved(
    file_path: str, issues: list[dict], working_dir: str
) -> dict[str, bool]:
    """The 'verification, not regeneration' step: cheap pattern/AST check
    per issue that its violation is actually gone from the now-patched file.
    Returns {issue_key: resolved_bool}. Anything False here is the ONLY
    trigger for a narrow single-issue follow-up LLM call."""
    raise NotImplementedError("wire to rule-specific regex/AST checks")
