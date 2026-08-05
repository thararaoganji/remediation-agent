"""
Section 4 as ADK FunctionTools. These are plain deterministic functions —
no LLM involved — called by the custom orchestrator agents in agents.py.
ADK wraps any typed Python function into a tool automatically; the
docstring becomes the tool description the LlmAgent-calling-code sees,
but note NONE of these are called by an LlmAgent in this design — only by
BaseAgent orchestration code directly, per the "LLM only for fix
generation" principle from the review.
"""

import requests

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


def _auth_headers(token: str) -> dict:
    # Bearer is the current documented auth scheme for the Sonar Web API;
    # user tokens no longer work reliably as an HTTP Basic username on
    # newer server versions.
    return {"Authorization": f"Bearer {token}"}


def _sonar_get(sonar_base_url: str, path: str, token: str, params: dict) -> dict:
    resp = requests.get(
        f"{sonar_base_url.rstrip('/')}{path}",
        headers=_auth_headers(token),
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _component_path(component_key: str, project_key: str) -> str:
    # issues/hotspots return component as "{projectKey}:{path}"
    prefix = f"{project_key}:"
    return component_key[len(prefix):] if component_key.startswith(prefix) else component_key


def _parse_effort_minutes(effort: str | None) -> int:
    """Sonar effort/debt strings look like '5min', '1h30min', '2d' (1d = 8h)."""
    if not effort:
        return 0
    minutes, num = 0, ""
    for ch in effort:
        if ch.isdigit():
            num += ch
            continue
        if not num:
            continue
        value = int(num)
        minutes += {"d": value * 8 * 60, "h": value * 60, "m": value}.get(ch, 0)
        num = ""
    return minutes


_TYPE_TO_CATEGORY = {"VULNERABILITY": "SECURITY", "BUG": "RELIABILITY", "CODE_SMELL": "MAINTAINABILITY"}


def _normalize_issue(raw: dict, project_key: str) -> dict:
    """Normalizes one /api/issues/search entry into the shape classify_issue(),
    partition_and_prioritize(), patch_tools, and prompts.build_issue_block()
    all expect. Handles both the legacy severity/type taxonomy and the
    newer per-softwareQuality `impacts` array (Clean Code taxonomy) — see
    _taxonomy_and_severity() above, which keys off whether
    'impact_severities' is present."""
    text_range = raw.get("textRange") or {}
    impacts = raw.get("impacts") or []

    category, impact_severity = None, None
    by_quality = {imp["softwareQuality"]: imp["severity"] for imp in impacts}
    for quality in ("SECURITY", "RELIABILITY", "MAINTAINABILITY"):
        if quality in by_quality:
            category, impact_severity = quality, by_quality[quality]
            break
    if category is None:
        category = _TYPE_TO_CATEGORY.get(raw.get("type"), raw.get("type"))

    issue = {
        "issue_key": raw["key"],
        "rule_key": raw["rule"],
        "component_path": _component_path(raw["component"], project_key),
        "category": category,
        "severity": impact_severity or raw.get("severity", ""),
        "message": raw.get("message", ""),
        "start_line": text_range.get("startLine", raw.get("line", 0)) or 0,
        "end_line": text_range.get("endLine", raw.get("line", 0)) or 0,
        "start_offset": text_range.get("startOffset", 0) or 0,
        "end_offset": text_range.get("endOffset", 0) or 0,
        "effort_minutes": _parse_effort_minutes(raw.get("effort") or raw.get("debt")),
    }
    if impact_severity is not None:
        issue["impact_severities"] = impact_severity
    return issue


def _normalize_hotspot(raw: dict, project_key: str) -> dict:
    return {
        "issue_key": raw["key"],
        "rule_key": raw.get("ruleKey", ""),
        "component_path": _component_path(raw["component"], project_key),
        "category": "HOTSPOT",
        "severity": raw.get("vulnerabilityProbability", ""),
        "vulnerability_probability": raw.get("vulnerabilityProbability"),
        "message": raw.get("message", ""),
        "start_line": raw.get("line", 0) or 0,
        "end_line": raw.get("line", 0) or 0,
        "start_offset": 0,
        "end_offset": 0,
        "effort_minutes": 0,
    }


def _paginate(sonar_base_url: str, path: str, token: str, params: dict, list_key: str) -> tuple[list[dict], dict]:
    page, page_size = 1, 500
    results: list[dict] = []
    rule_names: dict[str, str] = {}
    while True:
        data = _sonar_get(sonar_base_url, path, token, {**params, "p": page, "ps": page_size})
        results.extend(data.get(list_key, []))
        for rule in data.get("rules", []):
            rule_names[rule["key"]] = rule.get("name", rule["key"])
        paging = data.get("paging", {"total": len(results), "pageSize": page_size})
        if page * paging.get("pageSize", page_size) >= paging.get("total", len(results)):
            break
        page += 1
    return results, rule_names


def fetch_issues_and_hotspots(sonar_base_url: str, project_key: str, token: str) -> list[dict]:
    """GET /api/issues/search + /api/hotspots/search, normalized into one
    combined list. Only OPEN/CONFIRMED/REOPENED issues and TO_REVIEW
    hotspots — anything already resolved or reviewed is excluded at the
    source rather than relying on classify_issue() to filter it out.
    Caller must first confirm which severity taxonomy this Sonar instance
    returns (legacy vs Clean Code) before classify_issue() is applied —
    see Section 4.1 note in the workflow doc; handled per-issue here via
    the `impacts` array when present."""
    raw_issues, rule_names = _paginate(
        sonar_base_url, "/api/issues/search", token,
        {
            "componentKeys": project_key,
            "statuses": "OPEN,CONFIRMED,REOPENED",
            "additionalFields": "rules",
        },
        "issues",
    )
    issues = []
    for raw in raw_issues:
        issue = _normalize_issue(raw, project_key)
        issue["rule_name"] = rule_names.get(issue["rule_key"], issue["rule_key"])
        issues.append(issue)

    raw_hotspots, _ = _paginate(
        sonar_base_url, "/api/hotspots/search", token,
        {"projectKey": project_key, "status": "TO_REVIEW"},
        "hotspots",
    )
    for raw in raw_hotspots:
        hotspot = _normalize_hotspot(raw, project_key)
        hotspot["rule_name"] = hotspot["rule_key"]
        issues.append(hotspot)

    return issues


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
    {rule_description_from_sonar_rules_api} in the fix prompt.
    Newer Sonar versions split the description into `descriptionSections`
    rather than a single `htmlDesc`; falls back through both."""
    data = _sonar_get(sonar_base_url, "/api/rules/show", token, {"key": rule_key})
    rule = data.get("rule", {})
    if rule.get("htmlDesc"):
        return rule["htmlDesc"]
    sections = rule.get("descriptionSections") or []
    if sections:
        return "\n\n".join(s.get("content", "") for s in sections if s.get("content"))
    return rule.get("mdDesc", "")


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
