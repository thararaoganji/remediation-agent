"""
Section 4 as ADK FunctionTools. These are plain deterministic functions —
no LLM involved — called by the custom orchestrator agents in agents.py.
ADK wraps any typed Python function into a tool automatically; the
docstring becomes the tool description the LlmAgent-calling-code sees,
but note NONE of these are called by an LlmAgent in this design — only by
BaseAgent orchestration code directly, per the "LLM only for fix
generation" principle from the review.
"""

CATEGORY_RANK = {"SECURITY": 0, "RELIABILITY": 1, "MAINTAINABILITY": 2, "HOTSPOT": 3}

# Per-category severity floor. SECURITY/RELIABILITY ratings are gated by the
# single WORST open bug/vulnerability (no partial credit), so hitting A
# there requires closing every tier down to (but not including) Info.
# MAINTAINABILITY is a technical-debt RATIO (<=5% for A), not a worst-issue
# threshold — it stays scoped to Critical/High/Medium here; see
# get_maintainability_debt_ratio() / debt_ratio_expansion_candidates() below
# for topping it up only if the ratio actually demands it.
CATEGORY_SEVERITY_FLOOR = {
    "SECURITY":        {"LEGACY": {"BLOCKER", "CRITICAL", "MAJOR", "MINOR"},
                         "CLEAN_CODE": {"BLOCKER", "HIGH", "MEDIUM", "LOW"}},
    "RELIABILITY":      {"LEGACY": {"BLOCKER", "CRITICAL", "MAJOR", "MINOR"},
                         "CLEAN_CODE": {"BLOCKER", "HIGH", "MEDIUM", "LOW"}},
    "MAINTAINABILITY":  {"LEGACY": {"BLOCKER", "CRITICAL", "MAJOR"},
                         "CLEAN_CODE": {"BLOCKER", "HIGH", "MEDIUM"}},
}
HOTSPOT_PROBABILITY_SCOPE = {"HIGH", "MEDIUM"}   # LOW dropped

# Within SECURITY/RELIABILITY's expanded scope, Minor/Low issues are cheap
# to get wrong and low value to auto-fix (frequently the trivial tail of a
# backlog). Route them to a human review queue as won't-fix/false-positive
# CANDIDATES instead of generating a code diff for every one automatically.
REVIEW_NOT_AUTOFIX = {"LEGACY": {"MINOR"}, "CLEAN_CODE": {"LOW"}}

# Rank order within a file's issue list — lower fixes first. Kept separate
# from the in/out-of-scope decision above so review-lane issues still sort
# sensibly if they're ever promoted into the autofix batch later.
SEVERITY_ORDER = {"BLOCKER": 0, "CRITICAL": 0, "HIGH": 0,
                   "MAJOR": 1, "MEDIUM": 1,
                   "MINOR": 2, "LOW": 2}

# Duplication and coverage are metrics, not issues, so they're already
# absent from fetch_issues_and_hotspots(). This constant exists so that any
# future quality-gate check explicitly excludes them rather than someone
# accidentally wiring the full default gate (which includes both) and
# silently pulling them back into scope.
OUT_OF_SCOPE_METRICS = [
    "duplicated_lines_density", "new_duplicated_lines_density",
    "coverage", "new_coverage",
]
IN_SCOPE_RATING_METRICS = ["security_rating", "reliability_rating", "sqale_rating"]
MAINTAINABILITY_DEBT_RATIO_TARGET = 5.0   # % — Sonar's A threshold for sqale_rating


def fetch_issues_and_hotspots(sonar_base_url: str, project_key: str, token: str) -> list[dict]:
    """GET /api/issues/search + /api/hotspots/search. Returns raw combined list.
    Caller must first confirm which severity taxonomy this Sonar instance
    returns (legacy vs Clean Code) before classify_issue() is applied —
    see Section 4.1 note in the workflow doc."""
    raise NotImplementedError("wire to requests.get against sonar_base_url")


def _taxonomy_and_severity(issue: dict) -> tuple[str, str]:
    if "impact_severities" in issue:
        return "CLEAN_CODE", issue["impact_severities"]
    return "LEGACY", issue["severity"]


def classify_issue(issue: dict) -> dict:
    """Single source of truth for the in/out-of-scope + autofix/review split.
    Returns {"in_scope": bool, "action": "autofix"|"review_wont_fix"|None,
    "rank": (cat_rank, sev_rank) | None}."""
    cat = issue["category"]
    cat_rank = CATEGORY_RANK.get(cat)
    if cat_rank is None:
        return {"in_scope": False, "action": None, "rank": None}

    if cat == "HOTSPOT":
        prob = issue.get("vulnerability_probability")
        if prob not in HOTSPOT_PROBABILITY_SCOPE:
            return {"in_scope": False, "action": None, "rank": None}
        sev_rank = SEVERITY_ORDER.get(prob, 1)
        return {"in_scope": True, "action": "autofix", "rank": (cat_rank, sev_rank)}

    taxonomy, severity = _taxonomy_and_severity(issue)
    floor = CATEGORY_SEVERITY_FLOOR.get(cat, {}).get(taxonomy, set())
    if severity not in floor:
        return {"in_scope": False, "action": None, "rank": None}

    action = "review_wont_fix" if severity in REVIEW_NOT_AUTOFIX[taxonomy] else "autofix"
    sev_rank = SEVERITY_ORDER.get(severity, 1)
    return {"in_scope": True, "action": action, "rank": (cat_rank, sev_rank)}


def get_quality_ratings(sonar_base_url: str, project_key: str, token: str) -> dict:
    """GET /api/measures/component?metricKeys=security_rating,reliability_rating,sqale_rating
    Deliberately requests ONLY IN_SCOPE_RATING_METRICS — duplication and
    coverage are never included here, so a caller can't accidentally treat
    them as part of the success criterion. Returns e.g.
    {"security_rating": "1.0", "reliability_rating": "2.0", "sqale_rating": "1.0"}
    where Sonar encodes A=1.0 .. E=5.0."""
    raise NotImplementedError("wire to requests.get with metricKeys=" + ",".join(IN_SCOPE_RATING_METRICS))


def partition_and_prioritize(issues: list[dict]) -> tuple[list[dict], list[dict]]:
    """Replaces the old prioritize_and_group_by_file(). Splits into:
      - file_groups: same shape as before, autofix-lane issues only,
        grouped by file and ordered so security-critical files come first.
      - review_queue: flat list of review_wont_fix-lane issues (Minor
        Security/Reliability), NOT grouped or auto-resolved. A human
        confirms each Won't Fix/False Positive transition out of band —
        see resolve_issue_transition() below, which is intentionally not
        called anywhere in the autonomous loop.
    Out-of-scope issues (Info, or below each category's floor) are simply
    dropped from both lists.
    """
    autofix, review_queue = [], []
    for i in issues:
        c = classify_issue(i)
        if not c["in_scope"]:
            continue
        i = {**i, "_rank": c["rank"]}
        if c["action"] == "review_wont_fix":
            review_queue.append(i)
        else:
            autofix.append(i)

    groups: dict[str, list[dict]] = {}
    for i in autofix:
        groups.setdefault(i["component_path"], []).append(i)

    file_groups = []
    for path, group_issues in groups.items():
        file_priority = min(i["_rank"] for i in group_issues)
        file_groups.append({"file": path, "file_priority": file_priority, "issues": group_issues})
    file_groups.sort(key=lambda g: g["file_priority"])

    return file_groups, review_queue


def resolve_issue_transition(sonar_base_url: str, issue_key: str, transition: str, token: str) -> None:
    """POST /api/issues/do_transition (transition='wontfix'|'falsepositive').
    Deliberately NOT called anywhere in agents.py's autonomous loop — marking
    a Security/Reliability issue won't-fix is a judgment call, not a
    mechanical one, and a wrong call here silently reopens the exact gap
    this workflow exists to close. Call this only after a human has
    reviewed state[WONT_FIX_REVIEW_QUEUE] and approved specific issue keys."""
    raise NotImplementedError("wire to requests.post, human-confirmed issue_keys only")


def get_maintainability_debt_ratio(sonar_base_url: str, project_key: str, token: str) -> float:
    """GET /api/measures/component?metricKeys=sqale_debt_ratio. Returns the
    percentage Sonar itself uses for the sqale_rating threshold (A <= 5.0)."""
    raise NotImplementedError("wire to requests.get")


def debt_ratio_expansion_candidates(all_issues: list[dict]) -> list[dict]:
    """Only called if get_maintainability_debt_ratio() is still above
    MAINTAINABILITY_DEBT_RATIO_TARGET after the main Critical/High/Medium
    pass. Returns remaining Minor/Low MAINTAINABILITY code smells sorted by
    remediation effort (minutes) descending, so the highest-debt items are
    pulled in first — closing the ratio gap with the fewest extra fixes,
    rather than working through the tail alphabetically or by file."""
    candidates = [
        i for i in all_issues
        if i["category"] == "MAINTAINABILITY"
        and _taxonomy_and_severity(i)[1] in ("MINOR", "LOW")
    ]
    return sorted(candidates, key=lambda i: i.get("effort_minutes", 0), reverse=True)


def get_rule_description(sonar_base_url: str, rule_key: str, token: str) -> str:
    """GET /api/rules/show?key={rule_key} — used to fill
    {rule_description_from_sonar_rules_api} in the fix prompt."""
    raise NotImplementedError("wire to requests.get")


def trigger_sonar_analysis(working_dir: str, project_key: str, ce_edition: bool) -> str:
    """Section 7: if not ce_edition (i.e. Developer+), push branch and run
    branch-aware sonar-scanner. If ce_edition, run a LOCAL working-tree scan
    (no sonar.branch.name) per the recommended CE workaround — nothing is
    pushed to the shared project until merge. Returns a CE task id to poll."""
    raise NotImplementedError("wire to sonar-scanner subprocess")


def poll_ce_task_status(task_id: str, timeout_s: int = 600) -> bool:
    raise NotImplementedError("wire to GET /api/ce/task?id=")


def get_issues_created_after(sonar_base_url: str, project_key: str, since_iso: str, token: str) -> list[dict]:
    """Used at checkpoint time — creationDate filtering avoids false
    positives from issue-key instability across scans (per workflow doc)."""
    raise NotImplementedError("wire to requests.get with createdAfter param")
